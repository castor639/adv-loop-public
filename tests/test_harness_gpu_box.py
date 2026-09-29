"""GpuBox against a scripted runner: AWS lifecycle, ssh transport, rsync containment, state, stops, reaping."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import budget
from harness.gpu import box as gpubox
from harness.gpu.box import GpuBox, GpuBoxConfig
from harness.gpu.box_api import GpuBoxError

ENV_ASSIGNMENT = re.compile(r"^[A-Z_][A-Z0-9_]*=")


def cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), rc, stdout=out, stderr=err)


def aws_error(code):
    return f"\nAn error occurred ({code}) when calling the operation: no\n"


class FakeRunner:
    """Scripted subprocess.run: a key derived from argv selects a queue of responses; the last one repeats."""

    def __init__(self):
        self.calls = []
        self.scripts = {}

    @staticmethod
    def key_of(argv):
        if argv[0] == "aws":
            return ("aws", argv[2])
        if argv[0] == "ssh":
            words = [w for w in shlex.split(argv[-1].split("exec env ", 1)[-1]) if not ENV_ASSIGNMENT.match(w)]
            return ("ssh", words[0], words[1]) if words[0] == "adv-gpu-agent" else ("ssh", words[0])
        return (argv[0],)

    def script(self, key, *responses):
        self.scripts[key] = list(responses)

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        queue = self.scripts.get(self.key_of(argv))
        if not queue:
            return cp(argv)
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(response):
            return response(argv, kwargs)
        rc, out, err = response
        return cp(argv, rc, out, err)

    def argvs(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == list(prefix)]

    def keys(self):
        return [self.key_of(a) for a, _ in self.calls]

    def remote_commands(self):
        return [a[-1] for a, _ in self.calls if a[0] == "ssh"]


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_repo(root: Path) -> None:
    files = {
        "harness/aws/gpu-box-bootstrap.sh": "#!/usr/bin/env bash\necho bootstrap\n",
        "harness/gpu/agent/adv-gpu-agent": "#!/usr/bin/env python3\nprint('agent')\n",
        "harness/gpu/agent/adv-gpu-watchdog.timer": "[Timer]\n",
        "harness/gpu/push.exclude": ".checker-key\n.harness/host/\n",
        "harness/container/Dockerfile.gpu": "FROM scratch\n",
        "harness/container/__pycache__/x.pyc": "ignored",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class BoxFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.state.mkdir()
        self.repo = (base / "repo").resolve()
        make_repo(self.repo)
        self.ws = base / "workspaces" / "ws-a"
        (self.ws / "payload" / "data").mkdir(parents=True)
        (self.ws / "payload" / "data" / "in.txt").write_text("xy", encoding="utf-8")
        (self.ws / ".checker-key").write_text("k", encoding="utf-8")
        (self.ws / "events.jsonl").write_text("", encoding="utf-8")
        (self.ws / ".harness" / "host").mkdir(parents=True)
        self.runner = FakeRunner()
        self.clock = Clock()
        self.launch = gpubox.utc(self.clock.now - 60)
        self.config = GpuBoxConfig(instance_id="i-gpu", image="adv-loop-gpu:v1")
        (self.state / "gpu-box.key").write_text("private", encoding="utf-8")
        (self.state / "gpu-box.known_hosts").write_text("172.31.5.6 ssh-ed25519 AAAA\n", encoding="utf-8")
        self.mirror = "/srv/adv-loop/workspaces/ws-a"

    def tearDown(self):
        self.tmp.cleanup()

    def box(self, **kw) -> GpuBox:
        kw.setdefault("config", self.config)
        kw.setdefault("runner", self.runner)
        kw.setdefault("sleep", self.clock.sleep)
        kw.setdefault("clock", self.clock)
        kw.setdefault("repo", self.repo)
        return GpuBox(self.state, **kw)

    def describe(self, state, ip="172.31.5.6", launch=None, az="us-east-2a"):
        return (0, json.dumps({"state": state, "private_ip": ip, "public_ip": None, "launch_time": launch or self.launch,
                               "az": az, "type": "g5.xlarge", "ami": "ami-07639bdb1dc95014c"}), "")

    def ready_json(self, **over):
        data = {"docker": True, "nvidia_smi": True, "image_present": True, "disk_free_gb": 120.5,
                "bootstrap_hash": self.box().bootstrap_hash(), "uptime_s": 30.0, "job_running": False}
        data.update(over)
        return json.dumps(data)

    def script_running_ready(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.runner.script(("ssh", "adv-gpu-agent", "ready"), (0, self.ready_json(), ""))

    def state_json(self):
        return json.loads((self.state / "gpu-box.json").read_text(encoding="utf-8"))

    def spend_rows(self):
        path = self.state / "gpu-spend.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class DescribeTests(BoxFixture):
    def test_describe_parses_the_query_map_and_records_state(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        info = self.box().describe()
        argv = self.runner.argvs("aws", "ec2", "describe-instances")[0]
        self.assertEqual(argv[:5], ["aws", "ec2", "describe-instances", "--instance-ids", "i-gpu"])
        query = argv[argv.index("--query") + 1]
        self.assertTrue(query.startswith("Reservations[0].Instances[0].{state:State.Name,private_ip:PrivateIpAddress"))
        self.assertIn("ami:ImageId", query)
        self.assertEqual(argv[-4:], ["--region", "us-east-2", "--output", "json"])
        self.assertEqual((info.state, info.private_ip, info.az, info.instance_type, info.ami_id, info.launch_time),
                         ("running", "172.31.5.6", "us-east-2a", "g5.xlarge", "ami-07639bdb1dc95014c", self.launch))
        self.assertEqual(info.uptime_seconds, 60)
        self.assertFalse(info.ready)
        state = self.state_json()
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["started_at"], self.launch, "a box found running is billed from its launch time")
        self.box().describe(cached_ok=True)
        self.assertEqual(len(self.runner.calls), 1, "a fresh describe is reused for a minute")
        self.clock.now += 61
        self.box().describe(cached_ok=True)
        self.assertEqual(len(self.runner.calls), 2)

    def test_not_found_is_reported_by_kind(self):
        self.runner.script(("aws", "describe-instances"), (254, "", aws_error("InvalidInstanceID.NotFound")))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().describe()
        self.assertEqual(ctx.exception.kind, "not_found")
        self.assertEqual(ctx.exception.as_dict()["error"], "gpu_not_found")

    def test_tag_discovery_runs_once_and_is_cached_in_state(self):
        config = GpuBoxConfig(instance_id=None)
        self.runner.script(("aws", "describe-instances"), (0, json.dumps(["i-found"]), ""), self.describe("stopped"))
        info = self.box(config=config).describe()
        self.assertEqual(info.instance_id, "i-found")
        discovery = [a for a in self.runner.argvs("aws", "ec2", "describe-instances") if "--filters" in a]
        self.assertEqual(len(discovery), 1)
        self.assertIn("Name=tag:Name,Values=adv-gpu-box", discovery[0])
        self.assertIn("Name=tag:adv-loop,Values=gpu", discovery[0])
        self.assertEqual(self.state_json()["instance_id"], "i-found")
        self.box(config=config).describe()
        discovery = [a for a in self.runner.argvs("aws", "ec2", "describe-instances") if "--filters" in a]
        self.assertEqual(len(discovery), 1, "a second controller reuses the cached id")

    def test_no_instance_anywhere_is_not_configured(self):
        self.runner.script(("aws", "describe-instances"), (0, "[]", ""))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box(config=GpuBoxConfig(instance_id=None)).ensure_running()
        self.assertEqual(ctx.exception.kind, "not_configured")


class StartTests(BoxFixture):
    def test_capacity_errors_retry_then_give_up_without_touching_ssh(self):
        self.runner.script(("aws", "describe-instances"), self.describe("stopped"))
        self.runner.script(("aws", "start-instances"), (254, "", aws_error("InsufficientInstanceCapacity")))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().ensure_running(need_seconds=600)
        self.assertEqual(ctx.exception.kind, "capacity")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.as_dict()["error"], "gpu_capacity")
        self.assertGreater(len(self.runner.argvs("aws", "ec2", "start-instances")), 5)
        self.assertEqual(self.clock.sleeps[:3], [30, 60, 60])
        self.assertLessEqual(sum(self.clock.sleeps), 900)
        self.assertFalse([k for k in self.runner.keys() if k[0] in ("ssh", "ssh-keyscan", "rsync")])
        self.assertIsNone(self.state_json().get("started_at"))

    def test_quota_errors_are_not_retried(self):
        self.runner.script(("aws", "describe-instances"), self.describe("stopped"))
        self.runner.script(("aws", "start-instances"), (254, "", aws_error("VcpuLimitExceeded")))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().ensure_running(need_seconds=60)
        self.assertEqual(ctx.exception.kind, "quota")
        self.assertEqual(len(self.runner.argvs("aws", "ec2", "start-instances")), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_budget_refusals_make_no_aws_call(self):
        guard = budget.AwsSpendGuard(self.state / "aws-spend.json")
        guard.update(1599.5)
        with self.assertRaises(GpuBoxError) as ctx:
            self.box(guard=guard).ensure_running(need_seconds=3600)
        self.assertEqual(ctx.exception.kind, "budget_stop")
        self.assertAlmostEqual(ctx.exception.details["projected_usd"], 1.006)
        self.assertEqual(self.runner.calls, [])
        stale = {"aws_spend_usd": 10.0, "stopped": False, "gpu_disabled": False, "at": "2026-01-01T00:00:00Z"}
        (self.state / "aws-spend.json").write_text(json.dumps(stale), encoding="utf-8")

        def failing():
            raise RuntimeError("cost explorer down")

        with self.assertRaises(GpuBoxError) as ctx:
            self.box(guard=guard, cost_refresh=failing).ensure_running(need_seconds=60)
        self.assertEqual(ctx.exception.kind, "budget_unknown")
        with self.assertRaises(GpuBoxError) as ctx:
            self.box(guard=guard).ensure_running(need_seconds=60)
        self.assertEqual(ctx.exception.kind, "budget_unknown")
        self.assertEqual(self.runner.calls, [])
        self.script_running_ready()
        info = self.box(guard=guard, cost_refresh=lambda: guard.update(10.0)).ensure_running(need_seconds=60)
        self.assertTrue(info.ready)
        self.assertEqual(self.runner.keys()[0], ("aws", "describe-instances"))

    def test_unreported_usd_counts_recent_runs_and_the_live_box(self):
        guard = budget.AwsSpendGuard(self.state / "aws-spend.json")
        guard.update(100.0)
        old = gpubox.utc(budget.parse_utc(guard.state()["at"]) - 3 * 86400)
        budget.record_gpu_spend(self.state / "gpu-spend.jsonl", {"kind": "box_run", "usd": 5.0, "stopped_at": old})
        budget.record_gpu_spend(self.state / "gpu-spend.jsonl", {"kind": "box_run", "usd": 2.5, "stopped_at": gpubox.utc(self.clock.now)})
        budget.record_gpu_spend(self.state / "gpu-spend.jsonl", {"kind": "job", "usd": 99.0})
        box = self.box(guard=guard)
        self.assertAlmostEqual(box.unreported_usd(), 2.5)
        self.runner.script(("aws", "describe-instances"), self.describe("running", launch=gpubox.utc(self.clock.now - 3600)))
        box.describe()
        self.assertAlmostEqual(box.unreported_usd(), 2.5 + 1.006, places=3)

    def test_stopping_box_waits_for_stopped_then_starts_and_readies(self):
        self.runner.script(("aws", "describe-instances"), self.describe("stopping"), self.describe("stopping"),
                           self.describe("stopped"), self.describe("pending"), self.describe("running"))
        self.runner.script(("aws", "start-instances"), (0, "{}", ""))
        self.runner.script(("ssh", "adv-gpu-agent", "ready"), (0, self.ready_json(), ""))
        info = self.box().ensure_running(need_seconds=60)
        keys = self.runner.keys()
        self.assertEqual(keys[:4], [("aws", "describe-instances")] * 3 + [("aws", "start-instances")])
        self.assertEqual(keys[-1], ("ssh", "adv-gpu-agent", "ready"))
        self.assertEqual(self.runner.argvs("aws", "ec2", "start-instances")[0][:5],
                         ["aws", "ec2", "start-instances", "--instance-ids", "i-gpu"])
        self.assertTrue(info.ready and info.bootstrapped)
        self.assertEqual(info.state, "running")
        state = self.state_json()
        self.assertTrue(state["started_at"])
        self.assertEqual(state["launch_time"], self.launch)
        self.assertTrue(state["ready"])
        self.assertIn(10, self.clock.sleeps)

    def test_running_box_near_the_uptime_cap_is_recycled_first(self):
        old_launch = gpubox.utc(self.clock.now - 11.8 * 3600)
        self.runner.script(("aws", "describe-instances"), self.describe("running", launch=old_launch),
                           self.describe("running", launch=old_launch), self.describe("stopped", launch=old_launch),
                           self.describe("running", launch=gpubox.utc(self.clock.now)))
        self.runner.script(("ssh", "adv-gpu-agent", "ready"), (0, self.ready_json(), ""))
        info = self.box().ensure_running(need_seconds=3600)
        keys = [k for k in self.runner.keys() if k[0] == "aws"]
        self.assertLess(keys.index(("aws", "stop-instances")), keys.index(("aws", "start-instances")))
        rows = self.spend_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "recycle")
        self.assertAlmostEqual(rows[0]["seconds"], 11.8 * 3600, delta=5)
        self.assertTrue(info.ready)

    def test_far_from_the_cap_a_running_box_is_not_recycled(self):
        self.script_running_ready()
        self.box().ensure_running(need_seconds=14400)
        self.assertFalse(self.runner.argvs("aws", "ec2", "stop-instances"))
        self.assertFalse(self.runner.argvs("aws", "ec2", "start-instances"))


class ReadinessTests(BoxFixture):
    def test_first_contact_keyscans_and_pins_the_host_key(self):
        (self.state / "gpu-box.known_hosts").unlink()
        self.script_running_ready()
        self.runner.script(("ssh-keyscan",), (0, "172.31.5.6 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc\n", "# 172.31.5.6:22 SSH-2.0\n"))
        self.box().ensure_running(need_seconds=60)
        keys = self.runner.keys()
        self.assertIn(("ssh-keyscan",), keys)
        self.assertLess(keys.index(("ssh-keyscan",)), keys.index(("ssh", "adv-gpu-agent", "ready")))
        self.assertEqual(self.runner.argvs("ssh-keyscan")[0], ["ssh-keyscan", "-T", "10", "-t", "ed25519", "172.31.5.6"])
        known = (self.state / "gpu-box.known_hosts").read_text(encoding="utf-8")
        self.assertEqual(known, "172.31.5.6 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc\n")
        self.assertEqual(stat.S_IMODE((self.state / "gpu-box.known_hosts").stat().st_mode), 0o600)
        self.runner.calls.clear()
        self.box().ensure_running(need_seconds=60)
        self.assertNotIn(("ssh-keyscan",), self.runner.keys(), "a pinned host is never rescanned")

    def test_host_key_mismatch_is_fatal_and_never_auto_accepted(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.runner.script(("ssh", "adv-gpu-agent", "ready"),
                           (255, "", "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.\n"))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().ensure_running(need_seconds=60)
        self.assertEqual(ctx.exception.kind, "hostkey_mismatch")
        self.assertNotIn(("ssh-keyscan",), self.runner.keys())
        self.assertFalse(self.runner.argvs("aws", "ec2", "stop-instances"))
        self.assertEqual((self.state / "gpu-box.known_hosts").read_text(encoding="utf-8"), "172.31.5.6 ssh-ed25519 AAAA\n")

    def test_never_reachable_box_is_stopped(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.runner.script(("ssh", "adv-gpu-agent", "ready"), (255, "", "ssh: connect to host 172.31.5.6 port 22: Connection timed out\n"))
        config = GpuBoxConfig(instance_id="i-gpu", ssh_ready_seconds=30)
        with self.assertRaises(GpuBoxError) as ctx:
            self.box(config=config).ensure_running(need_seconds=60)
        self.assertEqual(ctx.exception.kind, "ssh_unreachable")
        self.assertEqual(len(self.runner.argvs("aws", "ec2", "stop-instances")), 1)
        self.assertEqual(self.spend_rows()[-1]["reason"], "ssh_unreachable")
        self.assertEqual(self.state_json()["state"], "stopped")

    def test_hash_mismatch_triggers_bootstrap_and_a_full_disk_refuses(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.runner.script(("ssh", "adv-gpu-agent", "ready"), (0, self.ready_json(bootstrap_hash="stale"), ""),
                           (0, self.ready_json(), ""))
        self.runner.script(("ssh", "sudo"), (0, json.dumps({"image_digest": "sha256:abc"}), ""))
        info = self.box().ensure_running(need_seconds=60)
        self.assertTrue(info.ready)
        self.assertEqual(len(self.runner.argvs("rsync")), 3)
        self.assertIn(("ssh", "sudo"), self.runner.keys())
        self.assertEqual(self.state_json()["image_digest"], "sha256:abc")
        self.runner.calls.clear()
        self.runner.script(("ssh", "adv-gpu-agent", "ready"), (0, self.ready_json(disk_free_gb=3.2), ""))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().ensure_running(need_seconds=60)
        self.assertEqual(ctx.exception.kind, "disk_full")
        self.assertNotIn(("ssh", "sudo"), self.runner.keys())


class TransportTests(BoxFixture):
    def setUp(self):
        super().setUp()
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.box().describe()
        self.runner.calls.clear()
        self.opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                     "-o", "ServerAliveCountMax=6", "-o", "StrictHostKeyChecking=yes",
                     "-o", f"UserKnownHostsFile={self.state / 'gpu-box.known_hosts'}", "-o", "IdentitiesOnly=yes",
                     "-i", str(self.state / "gpu-box.key"), "-o", "LogLevel=ERROR"]

    def test_exec_remote_argv_shape(self):
        self.box().exec_remote(["nvidia-smi", "-L"], cwd="/srv/adv-loop/workspaces/ws a", env={"B": "2", "A": "x y"},
                               timeout=42, stdin="in")
        argv, kwargs = self.runner.calls[-1]
        self.assertEqual(argv, ["ssh", *self.opts, "ubuntu@172.31.5.6", "--",
                                "cd '/srv/adv-loop/workspaces/ws a' && exec env A='x y' B=2 nvidia-smi -L"])
        self.assertEqual(kwargs, {"capture_output": True, "text": True, "timeout": 42, "input": "in", "check": False})
        self.box().exec_remote(["adv-gpu-agent", "ready"])
        argv, kwargs = self.runner.calls[-1]
        self.assertEqual(argv[-1], "exec env adv-gpu-agent ready")
        self.assertEqual(kwargs["timeout"], 600.0)
        self.assertEqual(len(self.runner.calls), 2, "exec_remote uses the cached address without an aws call")

    def test_push_uses_rsync_with_excludes_and_creates_the_remote_parent(self):
        result = self.box().push(["payload/data", "payload/data/in.txt"], self.mirror, workspace=self.ws)
        ws = self.ws.resolve()
        ssh_cmd = "ssh " + shlex.join(self.opts)
        rsyncs = self.runner.argvs("rsync")
        self.assertEqual(rsyncs[0], ["rsync", "-a", "--partial", "--timeout=120",
                                     f"--exclude-from={self.repo / 'harness' / 'gpu' / 'push.exclude'}", "-e", ssh_cmd,
                                     f"{ws}/payload/data/", f"ubuntu@172.31.5.6:{self.mirror}/payload/data/"])
        self.assertEqual(rsyncs[1][-2:], [f"{ws}/payload/data/in.txt", f"ubuntu@172.31.5.6:{self.mirror}/payload/data/in.txt"])
        commands = self.runner.remote_commands()
        self.assertEqual(commands[0], f"exec env mkdir -p {self.mirror}/payload")
        self.assertEqual(commands[-1], "exec env adv-gpu-agent heartbeat")
        keys = self.runner.keys()
        self.assertLess(keys.index(("ssh", "mkdir")), keys.index(("rsync",)))
        self.assertEqual(result, {"pushed": ["payload/data", "payload/data/in.txt"], "bytes": 4})
        self.box().push(["payload/data"], self.mirror, workspace=self.ws, delete=True)
        self.assertIn("--delete", self.runner.argvs("rsync")[-1])
        self.assertEqual(self.runner.argvs("rsync")[-1][4], "--delete")

    def test_push_failure_and_missing_input_are_reported(self):
        self.runner.script(("rsync",), (12, "", "rsync: connection unexpectedly closed"))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().push(["payload/data"], self.mirror, workspace=self.ws)
        self.assertEqual(ctx.exception.kind, "push_failed")
        self.assertTrue(ctx.exception.retryable)
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().push(["payload/nope"], self.mirror, workspace=self.ws)
        self.assertEqual(ctx.exception.kind, "push_failed")

    def test_push_and_pull_refuse_host_only_paths_and_escapes(self):
        (self.ws / "payload" / "link").symlink_to("/etc")
        box = self.box()
        for bad in (".checker-key", ".harness/host/x", ".harness/sessions", ".harness/validate/r.json", ".harness",
                    "../etc/passwd", "/etc/passwd", "payload/link", "payload/link/hosts", "", "."):
            with self.assertRaises(GpuBoxError, msg=bad) as ctx:
                box.push([bad], self.mirror, workspace=self.ws)
            self.assertEqual(ctx.exception.kind, "path_refused", bad)
        for bad in ("events.jsonl", ".pending-event.json", "state.json", "attempts.jsonl", "evidence.jsonl", "task.md",
                    "decision-log.md", "report.md", "loop-config.json", ".checker-key", ".harness/host/gpu/jobs/x",
                    "../x", "payload/link/out"):
            with self.assertRaises(GpuBoxError, msg=bad) as ctx:
                box.pull([bad], self.mirror, workspace=self.ws)
            self.assertEqual(ctx.exception.kind, "path_refused", bad)
        self.assertFalse(self.runner.argvs("rsync"))
        self.assertFalse(self.runner.argvs("ssh"))

    def test_pull_creates_local_parents_and_rsyncs_into_the_parent(self):
        def deliver(argv, kwargs):
            dest = Path(argv[-1])
            (dest / "model.pt").write_bytes(b"12345")
            return cp(argv)

        self.runner.script(("rsync",), deliver)
        result = self.box().pull(["payload/out/model.pt", ".harness/gpu/jobs/g-1"], self.mirror, workspace=self.ws)
        ws = self.ws.resolve()
        rsyncs = self.runner.argvs("rsync")
        self.assertEqual(rsyncs[0][-2:], [f"ubuntu@172.31.5.6:{self.mirror}/payload/out/model.pt", f"{ws}/payload/out/"])
        self.assertEqual(rsyncs[1][-2:], [f"ubuntu@172.31.5.6:{self.mirror}/.harness/gpu/jobs/g-1", f"{ws}/.harness/gpu/jobs/"])
        self.assertNotIn("--delete", rsyncs[0])
        self.assertTrue((self.ws / ".harness" / "gpu" / "jobs").is_dir())
        self.assertEqual(result["pulled"], ["payload/out/model.pt", ".harness/gpu/jobs/g-1"])
        self.assertEqual(result["bytes"], 5)
        self.assertEqual(self.runner.remote_commands()[-1], "exec env adv-gpu-agent heartbeat")


class JobTests(BoxFixture):
    def setUp(self):
        super().setUp()
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.box().describe()
        self.runner.calls.clear()
        self.job_dir = f"{self.mirror}/.harness/gpu/jobs/g-1"

    def test_submit_returns_the_status_and_busy_on_exit_75(self):
        self.runner.script(("ssh", "adv-gpu-agent", "submit"), (0, '{"state": "running", "pid": 5}', ""))
        self.assertEqual(self.box().submit(self.job_dir), {"state": "running", "pid": 5})
        self.assertEqual(self.runner.remote_commands()[-1], f"exec env adv-gpu-agent submit {self.job_dir}")
        self.runner.script(("ssh", "adv-gpu-agent", "submit"), (75, '{"state": "rejected_busy"}', ""))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().submit(self.job_dir)
        self.assertEqual(ctx.exception.kind, "busy")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.details, {"state": "rejected_busy"})
        self.runner.script(("ssh", "adv-gpu-agent", "submit"), (2, '{"state": "rejected", "error": "env key refused"}', ""))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().submit(self.job_dir)
        self.assertEqual(ctx.exception.kind, "submit_failed")

    def test_wait_passes_max_and_returns_interrupted_when_the_box_stopped(self):
        self.runner.script(("ssh", "adv-gpu-agent", "wait"), (255, "", "Connection closed by remote host"))
        self.runner.script(("aws", "describe-instances"), self.describe("stopped"))
        result = self.box().wait(self.job_dir, max_seconds=300)
        self.assertEqual(result, {"state": "interrupted", "box_state": "stopped", "job_dir": self.job_dir})
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(argv[-1], f"exec env adv-gpu-agent wait {self.job_dir} --max 300")
        self.assertEqual(kwargs["timeout"], 330.0)
        self.assertEqual(len(self.runner.argvs("ssh")), 1)

    def test_wait_reconnects_after_one_ssh_failure(self):
        box = self.box()
        box.register_job("g-1", workspace=self.mirror, session_id="s1", timeout_seconds=600)
        self.runner.script(("ssh", "adv-gpu-agent", "wait"), (255, "", "Connection reset"), (0, '{"state": "done", "exit_code": 0}', ""))
        self.assertEqual(box.wait(self.job_dir, max_seconds=60), {"state": "done", "exit_code": 0})
        self.assertEqual(len(self.runner.argvs("ssh")), 2)
        self.assertEqual(self.clock.sleeps, [5])
        self.assertEqual(self.state_json()["active_jobs"]["g-1"]["reconnects"], 1)
        self.runner.script(("ssh", "adv-gpu-agent", "wait"), (3, '{"state": "running"}', ""))
        self.assertEqual(box.wait(self.job_dir, max_seconds=60), {"state": "running"})
        self.runner.script(("ssh", "adv-gpu-agent", "wait"), (255, "", "down"))
        with self.assertRaises(GpuBoxError) as ctx:
            box.wait(self.job_dir, max_seconds=60)
        self.assertEqual(ctx.exception.kind, "box_unreachable")

    def test_status_and_kill_parse_json(self):
        self.runner.script(("ssh", "adv-gpu-agent", "status"), (0, '{"state": "running"}', ""))
        self.runner.script(("ssh", "adv-gpu-agent", "kill"), (0, '{"state": "killed"}', ""))
        box = self.box()
        self.assertEqual(box.status(self.job_dir), {"state": "running"})
        self.assertEqual(box.kill(self.job_dir), {"state": "killed"})
        self.assertEqual(self.runner.remote_commands(), [f"exec env adv-gpu-agent status {self.job_dir}",
                                                         f"exec env adv-gpu-agent kill {self.job_dir}"])

    def test_register_and_release_update_the_state(self):
        box = self.box()
        box.register_job("g-1", workspace=self.mirror, session_id="s1", timeout_seconds=1200)
        state = self.state_json()
        record = state["active_jobs"]["g-1"]
        self.assertEqual((record["workspace"], record["session_id"], record["pid"], record["timeout_seconds"], record["reconnects"]),
                         (self.mirror, "s1", os.getpid(), 1200, 0))
        self.assertEqual(record["started_at"], gpubox.utc(self.clock.now))
        self.assertEqual(state["jobs_running"], 1)
        self.assertEqual(state["last_job_at"], gpubox.utc(self.clock.now))
        self.assertEqual(box.active_jobs(), [{"job_id": "g-1", **record}])
        self.clock.now += 30
        box.release_job("g-1")
        state = self.state_json()
        self.assertEqual(state["active_jobs"], {})
        self.assertEqual(state["jobs_running"], 0)
        self.assertEqual(state["last_job_at"], gpubox.utc(self.clock.now))
        self.assertEqual(box.active_jobs(), [])
        self.assertFalse(self.runner.calls, "job bookkeeping never touches the network")


class StopTests(BoxFixture):
    def test_stop_if_idle_honours_the_window_and_active_jobs(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running", launch=gpubox.utc(self.clock.now - 3600)))
        box = self.box()
        box.describe()
        box.register_job("g-1", workspace=self.mirror, session_id="s1", timeout_seconds=600)
        self.assertEqual(box.stop_if_idle(), {"action": "none", "reason": "jobs_running", "jobs": ["g-1"]})
        box.release_job("g-1")
        result = box.stop_if_idle()
        self.assertEqual((result["action"], result["reason"]), ("none", "recent"))
        describes = len(self.runner.argvs("aws", "ec2", "describe-instances"))
        self.clock.now += 30
        box.stop_if_idle()
        self.assertEqual(len(self.runner.argvs("aws", "ec2", "describe-instances")), describes, "cached within a minute")
        self.clock.now += 9 * 60
        self.assertEqual(box.stop_if_idle()["reason"], "recent")
        self.clock.now += 61
        result = box.stop_if_idle()
        self.assertEqual(result["action"], "stopped")
        self.assertEqual(result["reason"], "idle_stop")
        self.assertEqual(len(self.runner.argvs("aws", "ec2", "stop-instances")), 1)
        row = self.spend_rows()[-1]
        self.assertEqual(row["reason"], "idle_stop")
        self.assertEqual(self.state_json()["state"], "stopped")
        self.assertEqual(box.stop_if_idle(idle_minutes=1)["action"], "none")

    def test_stop_if_idle_uses_the_last_job_time_not_the_launch(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running", launch=gpubox.utc(self.clock.now - 7200)))
        box = self.box()
        box.describe()
        box.register_job("g-1", workspace=self.mirror, session_id="s1", timeout_seconds=600)
        box.release_job("g-1")
        self.clock.now += 5 * 60
        self.assertEqual(box.stop_if_idle(idle_minutes=10)["reason"], "recent")
        self.clock.now += 5 * 60
        self.assertEqual(box.stop_if_idle(idle_minutes=10)["action"], "stopped")

    def test_stop_now_records_the_run_and_forces_after_two_failures(self):
        launch = gpubox.utc(self.clock.now - 7200)
        self.runner.script(("aws", "describe-instances"), self.describe("running", launch=launch))
        self.runner.script(("aws", "stop-instances"), (254, "", aws_error("IncorrectInstanceState")),
                           (254, "", aws_error("IncorrectInstanceState")), (0, "{}", ""))
        box = self.box()
        box.register_job("g-7", workspace=self.mirror, session_id="s1", timeout_seconds=600)
        self.runner.script(("ssh", "adv-gpu-agent", "kill"), (0, '{"state": "killed"}', ""))
        result = box.stop_now("operator")
        stops = self.runner.argvs("aws", "ec2", "stop-instances")
        self.assertEqual([a[3:5] + ["--force"] * ("--force" in a) for a in stops],
                         [["--instance-ids", "i-gpu"], ["--instance-ids", "i-gpu"], ["--instance-ids", "i-gpu", "--force"]])
        self.assertEqual(self.clock.sleeps, [5])
        self.assertEqual(result["action"], "stopped")
        self.assertEqual(result["killed"], ["g-7"])
        self.assertEqual(result["stop"], {"forced": True, "attempts": 3})
        self.assertIn(f"adv-gpu-agent kill {self.mirror}/.harness/gpu/jobs/g-7", self.runner.remote_commands()[0])
        row = self.spend_rows()[-1]
        self.assertEqual(row["kind"], "box_run")
        self.assertEqual(row["instance_id"], "i-gpu")
        self.assertEqual(row["reason"], "operator")
        self.assertEqual(row["started_at"], launch)
        self.assertEqual(row["launch_time"], launch)
        self.assertEqual(row["stopped_at"], gpubox.utc(self.clock.now))
        self.assertEqual(row["seconds"], 7200 + 5)
        self.assertAlmostEqual(row["usd"], round((7200 + 5) / 3600 * 1.006, 4))
        self.assertEqual(row["rate_usd_per_hour"], 1.006)
        self.assertEqual(row["jobs"], ["g-7"])
        self.assertEqual(row["source"], "harness_timer")
        self.assertEqual(row, result["run"])
        state = self.state_json()
        self.assertEqual(state["state"], "stopped")
        self.assertIsNone(state["started_at"])
        self.assertIn("g-7", state["active_jobs"], "the owning session still releases its job")
        self.assertEqual(budget.gpu_spend_total(self.state), row["usd"])

    def test_stop_now_on_a_stopped_box_records_nothing(self):
        self.runner.script(("aws", "describe-instances"), self.describe("stopped"))
        result = self.box().stop_now("service_stop")
        self.assertEqual(result["action"], "already_stopped")
        self.assertIsNone(result["run"])
        self.assertFalse(self.runner.argvs("aws", "ec2", "stop-instances"))
        self.assertEqual(self.spend_rows(), [])
        fresh = GpuBox(self.state / "fresh", config=GpuBoxConfig(instance_id=None), runner=self.runner, clock=self.clock)
        self.assertEqual(fresh.stop_now("service_stop"),
                         {"action": "none", "reason": "not_configured", "stop_reason": "service_stop"})

    def test_external_stop_is_billed_on_the_next_describe(self):
        launch = gpubox.utc(self.clock.now - 1800)
        self.runner.script(("aws", "describe-instances"), self.describe("running", launch=launch), self.describe("stopped", launch=launch))
        box = self.box()
        box.describe()
        self.clock.now += 600
        box.describe()
        rows = self.spend_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "external_stop")
        self.assertEqual(rows[0]["seconds"], 2400)
        box.describe()
        self.assertEqual(len(self.spend_rows()), 1, "billed once")


class ReapTests(BoxFixture):
    def test_reap_kills_orphans_whose_owner_died_and_writes_the_orphaned_row(self):
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.runner.script(("ssh", "adv-gpu-agent", "kill"), (0, '{"state": "killed"}', ""))
        box = self.box()
        box.describe()
        box.register_job("g-dead", workspace=str(self.ws), session_id="s-dead", timeout_seconds=100)
        box.register_job("g-alive", workspace=str(self.ws), session_id="s-alive", timeout_seconds=100)
        state = self.state_json()
        state["active_jobs"]["g-dead"]["pid"] = 424242
        (self.state / "gpu-box.json").write_text(json.dumps(state), encoding="utf-8")
        self.clock.now += 400
        with patch.object(gpubox, "pid_alive", lambda pid: pid != 424242):
            result = box.reap()
        self.assertIsNone(result["error"])
        self.assertEqual([o["job_id"] for o in result["orphans"]], ["g-dead"])
        self.assertEqual(result["orphans"][0]["kill"], {"state": "killed"})
        self.assertIn(f"adv-gpu-agent kill {self.ws}/.harness/gpu/jobs/g-dead", self.runner.remote_commands()[0])
        rows = [json.loads(l) for l in (self.ws / ".gpu-spend.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["job_id"], rows[0]["state"], rows[0]["session_id"], rows[0]["seconds"]),
                         ("job", "g-dead", "orphaned", "s-dead", 100))
        self.assertAlmostEqual(rows[0]["usd"], round(100 / 3600 * 1.006, 4))
        self.assertEqual(list(self.state_json()["active_jobs"]), ["g-alive"])
        self.assertEqual(result["idle"]["reason"], "jobs_running")
        self.assertFalse(self.runner.argvs("aws", "ec2", "stop-instances"))

    def test_reap_never_raises(self):
        def boom(argv, kwargs):
            raise RuntimeError("aws exploded")

        self.runner.script(("aws", "describe-instances"), boom)
        result = self.box().reap()
        self.assertEqual(result["orphans"], [])
        self.assertIn("aws exploded", result["error"])


class ProvisionTests(BoxFixture):
    def setUp(self):
        super().setUp()
        self.runner.script(("aws", "describe-instances"), self.describe("running"))
        self.box().describe()
        self.runner.calls.clear()

    def test_bootstrap_hash_is_stable_and_ignores_caches(self):
        digest = self.box().bootstrap_hash()
        self.assertEqual(len(digest), 64)
        (self.repo / "harness" / "container" / "__pycache__" / "y.pyc").write_text("more", encoding="utf-8")
        self.assertEqual(self.box().bootstrap_hash(), digest)
        (self.repo / "harness" / "container" / "Dockerfile.gpu").write_text("FROM other\n", encoding="utf-8")
        self.assertNotEqual(self.box().bootstrap_hash(), digest)

    def test_bootstrap_pushes_the_repo_paths_and_runs_the_script_with_env(self):
        self.runner.script(("ssh", "sudo"), (0, "building\n" + json.dumps({"image_digest": "sha256:abc"}) + "\n", ""))
        box = self.box()
        result = box.bootstrap()
        digest = box.bootstrap_hash()
        rsyncs = self.runner.argvs("rsync")
        self.assertEqual([r[-2:] for r in rsyncs], [
            [f"{self.repo}/harness/aws/gpu-box-bootstrap.sh", "ubuntu@172.31.5.6:/srv/adv-loop/repo/harness/aws/gpu-box-bootstrap.sh"],
            [f"{self.repo}/harness/gpu/agent/", "ubuntu@172.31.5.6:/srv/adv-loop/repo/harness/gpu/agent/"],
            [f"{self.repo}/harness/container/", "ubuntu@172.31.5.6:/srv/adv-loop/repo/harness/container/"],
        ])
        sudo = [(a, k) for a, k in self.runner.calls if a[0] == "ssh" and "sudo" in a[-1]][0]
        self.assertEqual(sudo[0][-1], f"exec env sudo env ADV_GPU_BOOTSTRAP_HASH={digest} ADV_GPU_IMAGE=adv-loop-gpu:v1 "
                                      "bash /srv/adv-loop/repo/harness/aws/gpu-box-bootstrap.sh")
        self.assertEqual(sudo[1]["timeout"], 1800)
        self.assertEqual(result["bootstrap_hash"], digest)
        state = self.state_json()
        self.assertEqual((state["bootstrap_hash"], state["image"], state["image_digest"]), (digest, "adv-loop-gpu:v1", "sha256:abc"))
        self.assertFalse(self.runner.argvs("aws", "ec2", "stop-instances"))

    def test_bootstrap_failure_stops_the_box(self):
        self.runner.script(("ssh", "sudo"), (4, "", "Dockerfile.gpu missing"))
        with self.assertRaises(GpuBoxError) as ctx:
            self.box().bootstrap()
        self.assertEqual(ctx.exception.kind, "bootstrap_failed")
        self.assertEqual(len(self.runner.argvs("aws", "ec2", "stop-instances")), 1)
        self.assertEqual(self.spend_rows()[-1]["reason"], "bootstrap_failed")
        self.assertIn("Dockerfile.gpu missing", self.state_json()["last_error"])

    def test_keygen_and_trust(self):
        (self.state / "gpu-box.key").unlink()

        def make_key(argv, kwargs):
            Path(argv[-1]).write_text("private", encoding="utf-8")
            Path(argv[-1] + ".pub").write_text("ssh-ed25519 AAAA adv-harness-box\n", encoding="utf-8")
            return cp(argv)

        self.runner.script(("ssh-keygen",), make_key)
        pub = self.box().keygen()
        self.assertEqual(self.runner.argvs("ssh-keygen")[0], ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                                                              "adv-harness-box", "-f", str(self.state / "gpu-box.key")])
        self.assertEqual(pub, self.state / "gpu-box.key.pub")
        self.assertEqual(stat.S_IMODE((self.state / "gpu-box.key").stat().st_mode), 0o600)
        self.box().keygen()
        self.assertEqual(len(self.runner.argvs("ssh-keygen")), 1, "an existing key is kept")
        self.runner.script(("ssh-keyscan",), (0, "172.31.5.6 ssh-ed25519 NEWKEY\n", ""))
        result = self.box().trust(reset=True)
        self.assertEqual(result["entries"], 1)
        self.assertEqual((self.state / "gpu-box.known_hosts").read_text(encoding="utf-8"), "172.31.5.6 ssh-ed25519 NEWKEY\n")


class ModuleTests(BoxFixture):
    def test_module_wrappers_read_adv_loop_state(self):
        self.runner.script(("aws", "describe-instances"), self.describe("stopped"))
        with patch.dict(os.environ, {"ADV_LOOP_STATE": str(self.state), "ADV_LOOP_REPO": str(self.repo)}):
            info = gpubox.describe(config=self.config, runner=self.runner, clock=self.clock, sleep=self.clock.sleep)
            self.assertEqual(info.state, "stopped")
            self.assertEqual(self.state_json()["instance_id"], "i-gpu")
            result = gpubox.stop_now("service_stop", config=self.config, runner=self.runner, clock=self.clock, sleep=self.clock.sleep)
            self.assertEqual(result["action"], "already_stopped")
            self.assertEqual(gpubox.stop_if_idle(config=self.config, runner=self.runner, clock=self.clock)["action"], "none")
        with patch.dict(os.environ, {"ADV_LOOP_STATE": ""}):
            with self.assertRaises(GpuBoxError) as ctx:
                gpubox.describe(config=self.config, runner=self.runner)
            self.assertEqual(ctx.exception.kind, "not_configured")

    def test_config_from_env(self):
        env = {"ADV_LOOP_GPU_BOX": "i-env", "AWS_DEFAULT_REGION": "us-west-2", "ADV_LOOP_GPU_IDLE_MINUTES": "4",
               "ADV_LOOP_GPU_RATE_USD": "2.5", "ADV_LOOP_GPU_IMAGE": "img:9", "ADV_LOOP_GPU_START_MAX_WAIT_SECONDS": "77",
               "ADV_LOOP_GPU_SPEND_MAX_AGE_HOURS": "1.5", "ADV_LOOP_GPU_MAX_UPTIME_MINUTES": "60",
               "ADV_LOOP_GPU_MIRROR_ROOT": "/mnt/ws", "ADV_LOOP_GPU_VPC_CIDR": "10.0.0.0/8", "ADV_LOOP_GPU_SSH_USER": "ec2-user"}
        config = GpuBoxConfig.from_env(env)
        self.assertEqual((config.instance_id, config.region, config.idle_minutes, config.rate_usd_per_hour, config.image,
                          config.start_max_wait_seconds, config.spend_max_age_hours, config.max_uptime_minutes,
                          config.mirror_root, config.vpc_cidr, config.ssh_user),
                         ("i-env", "us-west-2", 4, 2.5, "img:9", 77, 1.5, 60, "/mnt/ws", "10.0.0.0/8", "ec2-user"))
        defaults = GpuBoxConfig.from_env({"ADV_LOOP_GPU_IDLE_MINUTES": "junk"})
        self.assertEqual((defaults.instance_id, defaults.region, defaults.idle_minutes, defaults.rate_usd_per_hour,
                          defaults.image, defaults.start_max_wait_seconds, defaults.spend_max_age_hours,
                          defaults.max_uptime_minutes, defaults.mirror_root, defaults.vpc_cidr),
                         (None, "us-east-2", 10, 1.006, "adv-loop-gpu:v1", 900, 12.0, 720, "/srv/adv-loop/workspaces", "172.31.0.0/16"))

    def test_flock_excludes_a_second_instance(self):
        first, second = self.box(), self.box()
        second.short_lock_timeout_seconds = 0.0
        with first._locked():
            with self.assertRaises(GpuBoxError) as ctx:
                with second._locked(timeout=0):
                    pass
            self.assertEqual(ctx.exception.kind, "locked")
            self.assertEqual(second.stop_if_idle(), {"action": "skipped", "reason": "locked"})
            self.assertEqual(second.reap(), {"orphans": [], "idle": {"action": "skipped", "reason": "locked"}, "error": None})
            with first._locked():
                pass
        with second._locked(timeout=0):
            pass
        self.assertTrue((self.state / "gpu-box.lock").exists())


if __name__ == "__main__":
    unittest.main()
