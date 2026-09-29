"""GPU dispatch against a fake box: only declared files cross, evidence is what the harness pulled.

The FakeBox runs the job's argv locally under the job's own timeout, so every
path the runner takes (refusals, drift, timeout, interruption, orphan recovery,
the shim round trip, and the two checkers) is exercised offline.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adv_loop.engine import SAFE_ID, checker_attestation_mac
from adv_loop.validators import validate_report

from harness import budget, gpu, ledger, pause, transcripts
from harness.gpu import broker as gpu_broker
from harness.gpu import index, jobs, lock, service, spend
from harness.gpu.box_api import GpuBoxError

try:
    from fake_gpu_box import FakeBox
    from test_engine import Harness
except ImportError:  # invoked as tests.test_harness_gpu rather than via discovery
    from tests.fake_gpu_box import FakeBox
    from tests.test_engine import Harness

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM = REPO_ROOT / "harness" / "container" / "adv-gpu-run"
OUT = "payload/derived/out.txt"
WRITE_OUT = ["python3", "-c",
             "import os, pathlib; ws = pathlib.Path(os.environ['ADV_LOOP_WORKSPACE']);"
             " d = ws / 'payload/derived'; d.mkdir(parents=True, exist_ok=True);"
             " (d / 'out.txt').write_text('gpu says hi\\n'); (d / 'extra.txt').write_text('undeclared\\n');"
             " print('done')"]
WRITE_JOB_ID = ["python3", "-c",
                "import os, pathlib; ws = pathlib.Path(os.environ['ADV_LOOP_WORKSPACE']);"
                " d = ws / 'payload/derived'; d.mkdir(parents=True, exist_ok=True);"
                " (d / 'out.txt').write_text(os.environ['ADV_GPU_JOB_ID'] + '\\n')"]
SLEEP = ["python3", "-c", "import time; time.sleep(30)"]
EVIDENCE = {"ref": "e1", "kind": "artifact", "quality": "direct", "claim": "c", "method": "m",
            "independence_key": "k", "supports": ["C1"]}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gpu_config(**overrides):
    config = {**gpu.DEFAULTS, "enabled": True, "min_job_seconds": 1, "max_job_seconds": 60,
              "queue_wait_seconds": 0, "collect_grace_seconds": 2, "max_hours": 2.0}
    config.update(overrides)
    return config


class GpuFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "workspaces"
        self.ws = self.root / "ws-a"
        (self.ws / "payload" / "scratch").mkdir(parents=True)
        (self.ws / "payload" / "scratch" / "data.txt").write_text("1 2 3\n", encoding="utf-8")
        (self.ws / "payload" / "scratch" / "secret.txt").write_text("never pushed\n", encoding="utf-8")
        self.state = base / "state"
        self.state.mkdir()
        self.box = FakeBox(base / "remote")
        self.config = gpu_config()
        self.observed = lock.observe(self.box, image=self.config["image"])
        lock.write_fleet_lock(self.state, self.observed)
        lock.write_workspace_lock(self.ws, self.observed)
        self.box.calls.clear()

    def tearDown(self):
        self.box.wait_all()
        self.tmp.cleanup()

    def runner(self, session_id="sess-1", config=None, **kwargs):
        return jobs.JobRunner(self.box, self.ws, self.state, config or self.config, session_id=session_id,
                              directive_id="d1", role="researcher", mode="experiment", **kwargs)

    def args(self, **overrides):
        value = {"command": WRITE_OUT, "inputs": ["payload/scratch/data.txt"], "outputs": [OUT],
                 "timeout_seconds": 30, "label": "unit"}
        value.update(overrides)
        return value

    def host_record(self, job_id):
        return json.loads((self.ws / ".harness" / "host" / "gpu" / "jobs" / job_id / "job.json").read_text())

    def spend_rows(self):
        path = self.ws / gpu.GPU_SPEND_FILE
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


class RunTests(GpuFixture):
    def test_run_pushes_only_declared_inputs_and_pulls_only_declared_outputs(self):
        result, stdout, stderr, extra = self.runner().run(self.args(), tool_use_id="t1")
        job_id = result["job_id"]
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["collected"])
        self.assertEqual(stdout, "done\n")
        self.assertEqual(set(self.box.pushed), {"payload/scratch/data.txt", f".harness/gpu/jobs/{job_id}/job.json"})
        self.assertNotIn("payload/scratch/secret.txt", self.box.pushed)
        self.assertEqual((self.ws / OUT).read_text(), "gpu says hi\n")
        self.assertFalse((self.ws / "payload" / "derived" / "extra.txt").exists())
        self.assertEqual(result["outputs"], [{"path": OUT, "sha256": sha(self.ws / OUT), "bytes": 12}])
        self.assertEqual(result["missing_outputs"], [])
        copy = self.ws / ".harness" / "gpu" / "jobs" / job_id / "outputs" / OUT
        self.assertEqual(sha(copy), sha(self.ws / OUT))
        self.assertEqual(result["environment"]["toolchain_hash"], self.observed.toolchain_hash)
        self.assertEqual(result["environment"]["instance_type"], "g5.xlarge")
        self.assertEqual(result["validate_hint"], {"hook": "gpu-replay", "input": {"job_id": job_id, "artifact": OUT}})
        self.assertEqual(result["logs"]["stdout"], f".harness/gpu/jobs/{job_id}/stdout.log")
        self.assertEqual(extra["exit_code"], 0)
        self.assertFalse(extra["interrupted"])
        record = self.host_record(job_id)
        self.assertEqual(record["status"], "collected")
        self.assertEqual(record["outcome"], "finished")
        self.assertEqual(record["inputs"][0]["sha256"], sha(self.ws / "payload/scratch/data.txt"))
        self.assertEqual(record["log_hash"], hashlib.sha256(b"done\n\n---\n").hexdigest())
        self.assertEqual([row["status"] for row in index.rows(self.state)],
                         ["queued", "starting", "running", "finished", "collected"])
        self.assertEqual(index.running_jobs(self.state), [])

    def test_ledger_row_carries_exit_code_job_and_hashes_and_the_output_counts_as_observed(self):
        args = self.args()
        result, stdout, stderr, extra = self.runner().run(args, tool_use_id="t1")
        record = ledger.entry("gpu_run", args, stdout=stdout, stderr=stderr, tool_use_id="t1", **extra)
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["job"]["job_id"], result["job_id"])
        self.assertEqual(record["job"]["outputs"][0]["sha256"], sha(self.ws / OUT))
        self.assertEqual(record["job"]["toolchain_hash"], self.observed.toolchain_hash)
        self.assertEqual(record["job"]["status"], "collected")
        evidence, problems, warnings = ledger.verify_evidence(self.ws, [{**EVIDENCE, "artifact_path": OUT}],
                                                              ledger=[record])
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])
        self.assertEqual(evidence[0]["fingerprint"], sha(self.ws / OUT))
        digest = ledger.observation_digest([record])
        self.assertIn(f"job {result['job_id']} status=collected outputs=1 toolchain={self.observed.toolchain_hash[:12]}",
                      digest)
        self.assertIn("exit=0", digest)
        explicit = ledger.entry("Bash", {"command": "x; echo EXIT=$?"}, stdout="EXIT=3\n", exit_code=0)
        self.assertEqual(explicit["exit_code"], 0)

    def test_spend_lands_in_the_gpu_ledgers_and_never_in_the_api_ledger(self):
        result, _, _, _ = self.runner().run(self.args())
        rows = self.spend_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "job")
        self.assertEqual(rows[0]["job_id"], result["job_id"])
        self.assertEqual(rows[0]["session_id"], "sess-1")
        self.assertEqual(rows[0]["hourly_usd"], gpu.HOURLY_USD)
        self.assertEqual(rows[0]["box_instance_id"], "i-0fakegpu")
        fleet = [json.loads(line) for line in (self.state / gpu.STATE_SPEND_FILE).read_text().splitlines()]
        self.assertEqual(fleet[0]["job_id"], result["job_id"])
        self.assertFalse((self.ws / budget.SPEND_FILE).exists())
        self.assertEqual(spend.hours_billed(61), round(2 / 60, 6))
        self.assertEqual(spend.hours_billed(3600), 1.0)
        self.assertEqual(spend.hours_used(self.ws), rows[0]["hours_billed"])

    def test_hours_cap_refuses_and_writes_the_gpu_budget_stop_pause(self):
        spend.record_job_spend(self.ws, self.state, {"job_id": "g-20260101T000000Z-00000000", "session_id": "old",
                                                     "seconds_billed": 6840, "hours_billed": 1.9, "status": "collected"})
        config = gpu_config(max_job_seconds=14400)
        result, _, stderr, extra = self.runner(config=config).run(self.args(timeout_seconds=3600))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "gpu_hours_cap")
        self.assertIn("paused", result["message"])
        self.assertIsNone(extra["exit_code"])
        self.assertNotIn("ensure_running", self.box.names())
        self.assertEqual(len(self.spend_rows()), 1)
        record = pause.read(self.ws / pause.PAUSE_FILE)
        self.assertEqual(record["reason"], "gpu_budget_stop")
        self.assertTrue(record["indefinite"])
        self.assertEqual(record["max_hours"], 2.0)
        self.assertTrue(pause.workspace_paused(self.ws))
        self.assertEqual(self.host_record(result["job_id"])["status"], "refused")
        self.assertEqual(index.latest(self.state, result["job_id"])["status"], "refused")

    def test_aws_gpu_stop_refuses_without_a_pause(self):
        guard = budget.AwsSpendGuard(self.state / "aws-spend.json")
        guard.update(1700.0)
        self.assertFalse(guard.allows_gpu())
        result, _, _, extra = self.runner(guard=guard).run(self.args())
        self.assertEqual((result["status"], result["reason"]), ("refused", "aws_gpu_stop"))
        self.assertFalse((self.ws / pause.PAUSE_FILE).exists())
        self.assertNotIn("ensure_running", self.box.names())
        self.assertEqual(self.spend_rows(), [])

        class Guard:
            calls = []

            def allows_gpu(self, projected_usd=0.0, unreported_usd=0.0):
                self.calls.append((projected_usd, unreported_usd))
                return False

        result, _, _, _ = self.runner(guard=Guard()).run(self.args(timeout_seconds=36))
        self.assertEqual(result["reason"], "aws_gpu_stop")
        self.assertAlmostEqual(Guard.calls[0][0], 36 / 3600 * gpu.HOURLY_USD)

    def test_environment_drift_is_refused_before_any_push(self):
        self.box.drift = True
        result, _, _, _ = self.runner().run(self.args())
        self.assertEqual((result["status"], result["reason"]), ("refused", "environment_drift"))
        self.assertEqual(self.box.pushed, [])
        names = self.box.names()
        self.assertIn("ensure_running", names)
        self.assertNotIn("push", names)
        self.assertNotIn("submit", names)
        self.assertEqual(self.host_record(result["job_id"])["status"], "refused")
        self.assertEqual(self.spend_rows(), [])

    def test_missing_fleet_lock_and_box_start_failures_are_refusals(self):
        (self.state / gpu.STATE_LOCK_FILE).unlink()
        result, _, _, _ = self.runner().run(self.args())
        self.assertEqual(result["reason"], "box_unavailable")
        lock.write_fleet_lock(self.state, self.observed)
        self.box.fail_start = "capacity"
        result, _, _, _ = self.runner().run(self.args())
        self.assertEqual(result["reason"], "capacity")
        self.box.fail_start = None
        self.box.fail_push = "rsync exited 12"
        result, _, _, _ = self.runner().run(self.args())
        self.assertEqual(result["reason"], "push_failed")
        self.assertEqual(self.box.released, [result["job_id"]])

    def test_box_timeout_marks_timeout_and_interrupted(self):
        result, _, _, extra = self.runner().run(self.args(command=SLEEP, timeout_seconds=1))
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(extra["interrupted"])
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["missing_outputs"], [OUT])
        self.assertEqual(result["reason"], "output_missing")
        record = self.host_record(result["job_id"])
        self.assertEqual(record["outcome"], "timeout")
        self.assertEqual(record["status"], "collected")
        self.assertEqual(len(self.spend_rows()), 1)

    def test_harness_deadline_kills_a_job_the_box_did_not_stop(self):
        self.box.ignore_timeout = True
        config = gpu_config(collect_grace_seconds=0)
        result, _, _, extra = self.runner(config=config).run(self.args(command=SLEEP, timeout_seconds=1))
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(extra["interrupted"])
        self.assertIn("kill", self.box.names())
        self.assertEqual(self.box.released, [result["job_id"]])

    def test_box_interruption_marks_interrupted(self):
        self.box.interrupt_after_wait = True
        result, _, _, extra = self.runner().run(self.args(command=SLEEP, timeout_seconds=5))
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(extra["interrupted"])
        self.assertEqual(self.host_record(result["job_id"])["outcome"], "interrupted")

    def test_second_launch_is_refused_busy(self):
        held = index.LaunchLock(self.state)
        held.acquire()
        try:
            result, _, _, _ = self.runner().run(self.args())
        finally:
            held.release()
        self.assertEqual((result["status"], result["reason"]), ("refused", "busy"))
        self.assertNotIn("ensure_running", self.box.names())
        self.box.busy = True
        result, _, _, _ = self.runner().run(self.args())
        self.assertEqual(result["reason"], "busy")
        self.assertEqual(self.box.released[-1], result["job_id"])
        self.assertEqual(self.spend_rows(), [])

    def test_launch_lock_waits_with_the_injected_clock_then_gives_up(self):
        held = index.LaunchLock(self.state)
        held.acquire()
        ticks = iter(range(0, 100, 5))
        sleeps = []
        try:
            waiting = index.LaunchLock(self.state, wait_seconds=12, sleep=sleeps.append, clock=lambda: next(ticks))
            with self.assertRaises(GpuBoxError) as caught:
                waiting.acquire()
        finally:
            held.release()
        self.assertEqual(caught.exception.kind, "busy")
        self.assertTrue(sleeps)
        with index.launch_lock(self.state):
            pass

    def test_release_job_is_called_after_the_last_job(self):
        first, _, _, _ = self.runner().run(self.args())
        second, _, _, _ = self.runner().run(self.args())
        self.assertEqual(self.box.released, [first["job_id"], second["job_id"]])
        self.assertEqual(self.box.registered, {})
        self.assertTrue(self.box.stop_if_idle()["stopped"])
        names = self.box.names()
        self.assertLess(names.index("register_job"), names.index("push"))
        self.assertEqual(names[-2:], ["release_job", "stop_if_idle"])

    def test_output_too_large_leaves_the_output_on_the_box_until_collected(self):
        config = gpu_config(max_output_bytes=4)
        result, _, _, _ = self.runner(config=config).run(self.args())
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["reason"], "output_too_large")
        self.assertEqual(result["outputs"], [])
        self.assertFalse(result["collected"])
        self.assertFalse((self.ws / OUT).exists())
        record = self.host_record(result["job_id"])
        self.assertEqual(record["status"], "finished")
        self.assertTrue(record["outputs_ready"])
        self.assertEqual(len(self.spend_rows()), 1)
        collected, _, _, _ = self.runner(session_id="sess-2").collect(result["job_id"])
        self.assertTrue(collected["collected"])
        self.assertEqual(collected["outputs"][0]["path"], OUT)
        self.assertEqual(len(self.spend_rows()), 1)
        self.assertEqual(self.host_record(result["job_id"])["collected_by_session"], "sess-2")

    def test_public_view_has_no_host_only_fields_and_the_host_record_lives_under_host(self):
        result, _, _, _ = self.runner().run(self.args(), tool_use_id="toolu_1")
        job_id = result["job_id"]
        host = self.ws / ".harness" / "host" / "gpu" / "jobs" / job_id / "job.json"
        public = self.ws / ".harness" / "gpu" / "jobs" / job_id / "job.json"
        self.assertTrue(host.is_file() and public.is_file())
        host_record = json.loads(host.read_text())
        public_record = json.loads(public.read_text())
        for key in jobs.HOST_ONLY_FIELDS:
            self.assertIn(key, host_record)
            self.assertNotIn(key, public_record)
        self.assertEqual(host_record["session_id"], "sess-1")
        self.assertEqual(host_record["tool_use_id"], "toolu_1")
        self.assertEqual(host_record["box"]["instance_id"], "i-0fakegpu")
        self.assertEqual(public_record["environment"], host_record["environment"])
        store = jobs.JobStore(self.ws)
        self.assertEqual(store.read(job_id).session_id, "sess-1")
        self.assertEqual(store.public(job_id), public_record)
        self.assertIsNone(store.read("../etc"))
        self.assertEqual([job.job_id for job in store.list()], [job_id])

    def test_env_and_path_validation_refusals(self):
        bad = [
            ({"outputs": [".harness/x"]}, "harness-owned"),
            ({"outputs": ["sandbox/x"]}, "harness-owned"),
            ({"outputs": ["events.jsonl"]}, "harness-owned"),
            ({"outputs": ["loop-config.json"]}, "harness-owned"),
            ({"outputs": [".checker-key"]}, "harness-owned"),
            ({"outputs": ["../outside"]}, "escapes"),
            ({"outputs": []}, "at least one"),
            ({"cwd": "../x"}, "escapes"),
            ({"inputs": [".checker-key"]}, "host-only"),
            ({"inputs": [".harness/host/x"]}, "host-only"),
            ({"inputs": ["payload/scratch/missing.txt"]}, "does not exist"),
            ({"env": {"aws_key": "x"}}, "not allowed"),
            ({"env": {"AWS_SECRET_ACCESS_KEY": "x"}}, "credentials"),
            ({"env": {"ANTHROPIC_API_KEY": "x"}}, "credentials"),
            ({"env": {"LD_PRELOAD": "x"}}, "credentials"),
            ({"env": {"CLAUDECODE": "1"}}, "credentials"),
            ({"env": {"OK": "x" * 4097}}, "at most 4096"),
            ({"env": {"OK": 5}}, "at most 4096"),
            ({"network": "host"}, "network"),
            ({"label": "x" * 81}, "label"),
            ({"command": []}, "argv"),
            ({"command": ["python3", ""]}, "argv"),
            ({"timeout_seconds": "soon"}, "timeout_seconds"),
            ({"surprise": 1}, "unknown"),
        ]
        runner = self.runner()
        for overrides, fragment in bad:
            result, _, stderr, extra = runner.run(self.args(**overrides))
            self.assertEqual((result["status"], result["reason"]), ("refused", "invalid_input"), overrides)
            self.assertIn(fragment, result["message"], overrides)
            self.assertIsNone(extra["exit_code"])
        self.assertEqual(self.box.calls, [])
        self.assertFalse((self.ws / ".harness" / "host" / "gpu").exists())
        spec = jobs.validate_args(self.ws, self.args(timeout_seconds=0.5, cwd="./payload/scratch/"), self.config)
        self.assertEqual(spec.timeout_seconds, 1)
        self.assertEqual(spec.cwd, "payload/scratch")
        self.assertEqual(jobs.validate_args(self.ws, self.args(timeout_seconds=99999), self.config).timeout_seconds, 60)
        self.assertEqual(jobs.validate_args(self.ws, {"command": ["true"], "outputs": [OUT]}, self.config).timeout_seconds, 60)
        self.assertEqual(jobs.validate_args(self.ws, self.args(env={"HF_HOME": "/tmp"}), self.config).env, {"HF_HOME": "/tmp"})

    def test_job_dataclass_covers_every_documented_field_and_ids_are_safe(self):
        names = {f.name for f in dataclasses.fields(jobs.Job)}
        self.assertTrue(set(gpu.JOB_FIELDS) <= names, set(gpu.JOB_FIELDS) - names)
        job_id = jobs.new_job_id()
        self.assertRegex(job_id, jobs.JOB_ID_PATTERN)
        self.assertTrue(SAFE_ID.fullmatch(job_id))

    def test_status_report_never_starts_the_box(self):
        svc = service.GpuService(self.box, self.ws, self.state, self.config, session_id="sess-1")
        report, text, _, extra = svc.status({})
        self.assertEqual(report["hours"], {"used": 0.0, "max": 2.0, "remaining": 2.0})
        self.assertTrue(report["fleet_lock"] and report["workspace_lock"])
        self.assertEqual(report["box"]["state"], "stopped")
        self.assertEqual(report["jobs"], [])
        self.assertNotIn("ensure_running", self.box.names())
        self.assertEqual(json.loads(text)["hours"]["max"], 2.0)
        self.assertIsNone(extra["exit_code"])

    def test_service_turns_faults_into_refusals(self):
        class Broken(FakeBox):
            def ensure_running(self, *, need_seconds=0):
                raise RuntimeError("ssh exploded")

        broken = Broken(self.box.remote_root)
        svc = service.GpuService(broken, self.ws, self.state, self.config, session_id="sess-1")
        result, _, stderr, extra = svc.run(self.args())
        self.assertEqual((result["status"], result["reason"]), ("refused", "internal_error"))
        self.assertIn("ssh exploded", extra["error"])
        result, _, _, _ = svc.collect({"job_id": "g-20260101T000000Z-deadbeef"})
        self.assertEqual(result["reason"], "invalid_input")
        result, _, _, _ = svc.collect({})
        self.assertEqual(result["status"], "refused")


class OrphanTests(GpuFixture):
    def orphan(self, mark_dead=True):
        self.box.crash_on_wait = True
        with self.assertRaises(RuntimeError):
            self.runner().run(self.args())
        self.box.crash_on_wait = False
        self.box.wait_all()
        job = jobs.JobStore(self.ws).list()[0]
        self.assertEqual(job.status, "running")
        if mark_dead:
            target = transcripts.session_dir(self.ws, "sess-1")
            target.mkdir(parents=True)
            (target / "result.json").write_text("{}", encoding="utf-8")
        return job.job_id

    def test_sweep_and_collect_recover_an_orphan_and_record_spend_once(self):
        job_id = self.orphan()
        self.assertEqual(self.spend_rows(), [])
        self.assertEqual(index.running_jobs(self.state)[0]["job_id"], job_id)
        report = jobs.sweep(self.box, self.root, self.state, alive=lambda pid: False)
        self.assertEqual(report["settled"], [job_id])
        record = self.host_record(job_id)
        self.assertEqual(record["status"], "finished")
        self.assertTrue(record["outputs_ready"])
        self.assertEqual(record["returncode"], 0)
        self.assertTrue((self.ws / ".harness" / "gpu" / "jobs" / job_id / "stdout.log").is_file())
        self.assertFalse((self.ws / OUT).exists())
        self.assertEqual(self.spend_rows(), [])
        self.assertEqual(jobs.sweep(self.box, self.root, self.state, alive=lambda pid: False)["settled"], [])
        result, stdout, _, extra = jobs.collect(self.ws, self.state, job_id, session_id="sess-2", box=self.box,
                                                config=self.config)
        self.assertEqual(result["status"], "finished")
        self.assertTrue(result["collected"])
        self.assertEqual(result["outputs"][0]["sha256"], sha(self.ws / OUT))
        self.assertEqual(stdout, "done\n")
        self.assertEqual(extra["exit_code"], 0)
        self.assertEqual(extra["job"]["status"], "collected")
        self.assertEqual(self.host_record(job_id)["collected_by_session"], "sess-2")
        self.assertEqual(len(self.spend_rows()), 1)
        again, _, _, _ = jobs.collect(self.ws, self.state, job_id, session_id="sess-3", box=self.box, config=self.config)
        self.assertTrue(again["collected"])
        self.assertEqual(len(self.spend_rows()), 1)
        self.assertEqual(self.host_record(job_id)["collected_by_session"], "sess-2")
        self.assertIsNone(jobs.pending_collection_note(self.ws, alive=lambda pid: False))

    def test_collect_of_a_running_orphan_waits_for_the_box_and_needs_a_box(self):
        job_id = self.orphan()
        refused, _, _, _ = jobs.collect(self.ws, self.state, job_id, session_id="sess-2", config=self.config)
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "box_unavailable"))
        self.assertEqual(self.host_record(job_id)["status"], "running")
        result, _, _, _ = jobs.collect(self.ws, self.state, job_id, session_id="sess-2", box=self.box, config=self.config)
        self.assertTrue(result["collected"])
        self.assertEqual(result["status"], "finished")
        self.assertEqual(len(self.spend_rows()), 1)

    def test_pending_collection_note_lists_jobs_awaiting_collection(self):
        job_id = self.orphan(mark_dead=False)
        self.assertIsNone(jobs.pending_collection_note(self.ws, alive=lambda pid: True))
        note = jobs.pending_collection_note(self.ws, alive=lambda pid: False)
        self.assertTrue(note.startswith("## gpu jobs awaiting collection"))
        self.assertIn(job_id, note)
        self.assertIn("'unit'", note)
        self.assertIn(OUT, note)
        self.assertIn("gpu_collect", note)
        target = transcripts.session_dir(self.ws, "sess-1")
        target.mkdir(parents=True)
        (target / "result.json").write_text("{}", encoding="utf-8")
        self.assertIn(job_id, jobs.pending_collection_note(self.ws, alive=lambda pid: True))
        report = jobs.status_report(self.ws, self.state, self.config, None, self.box)
        self.assertEqual(report["jobs"][0]["status"], "running")

    def test_cancel_kills_and_releases(self):
        self.box.crash_on_wait = True
        with self.assertRaises(RuntimeError):
            self.runner().run(self.args(command=SLEEP, timeout_seconds=30))
        job_id = jobs.JobStore(self.ws).list()[0].job_id
        record = jobs.cancel(self.box, self.ws, self.state, job_id)
        self.assertEqual(record["status"], "cancelled")
        self.assertIn("kill", self.box.names())
        self.assertEqual(index.latest(self.state, job_id)["status"], "cancelled")
        self.box.wait_all()


class LockTests(unittest.TestCase):
    def test_toolchain_hash_covers_only_image_digest_cuda_driver_and_instance_type(self):
        base = lock.GpuLock("adv-loop-gpu:v1", "sha256:1", cuda="13.0", torch="2.13", python="3.12",
                            driver="580.65.06", gpu="NVIDIA A10G", instance_type="g5.xlarge", ami_id="ami-1")
        same = lock.GpuLock("adv-loop-gpu:v2", "sha256:1", cuda="13.0", torch="2.14", python="3.13",
                            driver="580.65.06", gpu="NVIDIA A10G 24GB", instance_type="g5.xlarge", ami_id="ami-2")
        self.assertEqual(base.toolchain_hash, same.toolchain_hash)
        self.assertEqual(len(base.toolchain_hash), 64)
        for changed in (dict(image_digest="sha256:2"), dict(cuda="12.8"), dict(driver="581.0"),
                        dict(instance_type="g6.xlarge")):
            other = dataclasses.replace(base, **changed)
            self.assertNotEqual(base.toolchain_hash, other.toolchain_hash, changed)
        self.assertEqual(lock.GpuLock.from_dict(base.as_dict()), base)
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            digest = lock.write_workspace_lock(ws, base)
            self.assertEqual(lock.read_workspace_lock(ws), base)
            self.assertEqual(lock.lock_sha256(ws), digest)
            self.assertIsNone(lock.read_fleet_lock(ws))
            lock.write_fleet_lock(ws, base)
            self.assertEqual(lock.read_fleet_lock(ws), base)

    def test_observe_reads_the_box_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = FakeBox(Path(tmp))
            observed = lock.observe(box, image="adv-loop-gpu:v1")
        self.assertEqual((observed.gpu, observed.driver, observed.cuda, observed.torch, observed.python),
                         ("NVIDIA A10G", "580.65.06", "13.0", "2.13.0+cu130", "3.12.3"))
        self.assertEqual(observed.instance_type, "g5.xlarge")
        self.assertEqual(observed.ami_id, "ami-0fake")
        self.assertTrue(observed.image_digest.startswith("sha256:"))
        self.assertEqual(len(box.names()), 4)


class CheckerFixture(unittest.TestCase):
    """A kernel workspace with a signing key, GPU locks, and the host-side checker hooks."""

    def setUp(self):
        self.harness = Harness(self)
        self.ws = self.harness.engine.workspace
        base = Path(tempfile.mkdtemp(dir=self.harness.temp.name))
        self.state = base / "state"
        self.state.mkdir()
        self.box = FakeBox(base / "remote")
        self.config = gpu_config()
        observed = lock.observe(self.box, image=self.config["image"])
        lock.write_fleet_lock(self.state, observed)
        lock.write_workspace_lock(self.ws, observed)
        (self.ws / "payload" / "scratch").mkdir(parents=True, exist_ok=True)
        (self.ws / ".checker-key").write_text("ab" * 32 + "\n", encoding="utf-8")
        (self.ws / "loop-config.json").write_text(json.dumps({
            "sandbox": {"kind": "local", "root": "sandbox", "command": ["env", "ADV_REPLICATE_PREFIX=1"]},
            "validators": {
                "gpu-replay": {"command": ["python3", str(REPO_ROOT / "checkers" / "gpu_replay.py")],
                               "rank": "executable_spec", "in_sandbox": False, "timeout_seconds": 600},
                "gpu-replicate": {"command": ["python3", str(REPO_ROOT / "checkers" / "gpu_replicate.py")],
                                  "rank": "replicated_experiment", "in_sandbox": False, "timeout_seconds": 900},
            },
        }), encoding="utf-8")

    def tearDown(self):
        self.box.wait_all()

    def run_job(self, command=WRITE_OUT, session_id="sess-1"):
        runner = jobs.JobRunner(self.box, self.ws, self.state, self.config, session_id=session_id,
                                directive_id="d1", role="researcher", mode="experiment")
        result, _, _, _ = runner.run({"command": command, "outputs": [OUT], "timeout_seconds": 30})
        self.assertEqual(result["status"], "finished", result)
        return result["job_id"]


class GpuReplayTests(CheckerFixture):
    def test_accepts_a_collected_job_with_a_signed_verdict_carrying_the_box_toolchain_hash(self):
        job_id = self.run_job()
        record = json.loads((self.ws / ".harness" / "host" / "gpu" / "jobs" / job_id / "job.json").read_text())
        report = validate_report(self.ws, "gpu-replay", input_value={"job_id": job_id, "artifact": OUT})
        self.assertEqual(report["exit_code"], 0, report)
        result = report["result"]
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["checker_id"], "gpu-replay")
        self.assertEqual(result["artifact_hash"], sha(self.ws / OUT))
        self.assertEqual(result["log_hash"], record["log_hash"])
        self.assertEqual(result["toolchain_hash"], record["environment"]["toolchain_hash"])
        self.assertEqual(result["attestation"], checker_attestation_mac("ab" * 32, result))
        self.assertEqual(result["details"]["instance_type"], "g5.xlarge")
        self.assertEqual(report["evidence_hint"]["formalization_rank"], "executable_spec")
        evidence, problems, _ = ledger.verify_evidence(self.ws, [{**EVIDENCE, "verdict_id": "V1"}],
                                                       verdicts={"V1": report})
        self.assertEqual(problems, [])
        self.assertEqual(evidence[0]["checker"]["toolchain_hash"], record["environment"]["toolchain_hash"])

    def test_rejects_a_tampered_output_and_ignores_the_public_copy(self):
        job_id = self.run_job()
        public = self.ws / ".harness" / "gpu" / "jobs" / job_id / "job.json"
        public.write_text(json.dumps({"status": "collected", "returncode": 0, "outputs": []}), encoding="utf-8")
        report = validate_report(self.ws, "gpu-replay", input_value={"job_id": job_id, "artifact": OUT})
        self.assertTrue(report["result"]["accepted"])
        with open(self.ws / OUT, "a", encoding="utf-8") as handle:
            handle.write("tampered\n")
        report = validate_report(self.ws, "gpu-replay", input_value={"job_id": job_id, "artifact": OUT})
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["result"]["accepted"])
        self.assertIn("changed since collection", " ".join(report["result"]["details"]["problems"]))
        self.assertEqual(report["result"]["artifact_hash"], sha(self.ws / OUT))
        self.assertNotIn("formalization_rank", report["evidence_hint"])

    def test_rejects_an_undeclared_artifact_and_a_failed_job(self):
        job_id = self.run_job()
        (self.ws / "payload" / "derived" / "other.txt").write_text("x\n", encoding="utf-8")
        report = validate_report(self.ws, "gpu-replay", input_value={"job_id": job_id, "artifact": "payload/derived/other.txt"})
        self.assertFalse(report["result"]["accepted"])
        self.assertIn("not a recorded output", " ".join(report["result"]["details"]["problems"]))
        failing = ["python3", "-c", "import os, pathlib; ws = pathlib.Path(os.environ['ADV_LOOP_WORKSPACE']);"
                   " (ws / 'payload/derived').mkdir(parents=True, exist_ok=True);"
                   " (ws / 'payload/derived/out.txt').write_text('partial\\n'); raise SystemExit(3)"]
        failed = self.run_job(command=failing)
        report = validate_report(self.ws, "gpu-replay", input_value={"job_id": failed, "artifact": OUT})
        self.assertFalse(report["result"]["accepted"])
        self.assertIn("return code is 3", " ".join(report["result"]["details"]["problems"]))

    def test_contract_problems_are_hook_failures_not_verdicts(self):
        job_id = self.run_job()
        for bad in ({"job_id": "g-20260101T000000Z-deadbeef", "artifact": OUT},
                    {"job_id": "../../etc/passwd", "artifact": OUT},
                    {"job_id": job_id, "artifact": "../outside"},
                    {"job_id": job_id, "artifact": "payload/derived/missing.txt"},
                    {"job_id": job_id}):
            report = validate_report(self.ws, "gpu-replay", input_value=bad)
            self.assertEqual(report["exit_code"], 1, bad)
            self.assertIn("failure", report)


class GpuReplicateTests(CheckerFixture):
    def test_needs_two_matching_collected_jobs(self):
        first = self.run_job()
        second = self.run_job(session_id="sess-2")
        report = validate_report(self.ws, "gpu-replicate", input_value={"job_ids": [first, second], "artifact": OUT})
        self.assertEqual(report["exit_code"], 0, report)
        result = report["result"]
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["checker_id"], "gpu-replicate")
        self.assertEqual(result["artifact_hash"], sha(self.ws / OUT))
        self.assertEqual(result["toolchain_hash"], lock.read_workspace_lock(self.ws).toolchain_hash)
        self.assertEqual(report["evidence_hint"]["formalization_rank"], "replicated_experiment")
        self.assertIsNone(result["details"]["compare_returncode"])
        same = validate_report(self.ws, "gpu-replicate", input_value={"job_ids": [first, first], "artifact": OUT})
        self.assertFalse(same["result"]["accepted"])
        self.assertIn("same job", " ".join(same["result"]["details"]["problems"]))
        other = self.run_job(command=WRITE_OUT + ["--flag"], session_id="sess-3")
        mixed = validate_report(self.ws, "gpu-replicate", input_value={"job_ids": [first, other], "artifact": OUT})
        self.assertFalse(mixed["result"]["accepted"])
        self.assertIn("differ in command", " ".join(mixed["result"]["details"]["problems"]))
        copy = self.ws / ".harness" / "gpu" / "jobs" / second / "outputs" / OUT
        copy.write_text("edited\n", encoding="utf-8")
        edited = validate_report(self.ws, "gpu-replicate", input_value={"job_ids": [first, second], "artifact": OUT})
        self.assertFalse(edited["result"]["accepted"])
        self.assertIn("per-job copy changed", " ".join(edited["result"]["details"]["problems"]))
        broken = validate_report(self.ws, "gpu-replicate", input_value={"job_ids": [first], "artifact": OUT})
        self.assertEqual(broken["exit_code"], 1)

    def test_differing_outputs_need_a_compare_that_runs_through_the_sandbox_prefix(self):
        first = self.run_job(command=WRITE_JOB_ID)
        second = self.run_job(command=WRITE_JOB_ID, session_id="sess-2")
        without = validate_report(self.ws, "gpu-replicate", input_value={"job_ids": [first, second], "artifact": OUT})
        self.assertFalse(without["result"]["accepted"])
        self.assertIn("no compare command", " ".join(without["result"]["details"]["problems"]))
        compare = ["python3", "-c",
                   "import os, sys; a, b = open(sys.argv[1]).read(), open(sys.argv[2]).read();"
                   " ok = os.environ.get('ADV_REPLICATE_PREFIX') == '1' and a.startswith('g-') and b.startswith('g-')"
                   " and a != b; print('prefix', os.environ.get('ADV_REPLICATE_PREFIX')); sys.exit(0 if ok else 1)",
                   "{a}", "{b}"]
        report = validate_report(self.ws, "gpu-replicate",
                                 input_value={"job_ids": [first, second], "artifact": OUT, "compare": compare})
        self.assertEqual(report["exit_code"], 0, report)
        self.assertTrue(report["result"]["accepted"], report["result"])
        self.assertEqual(report["result"]["details"]["compare_returncode"], 0)
        self.assertIn("prefix 1", report["result"]["details"]["compare_stdout_tail"])
        self.assertEqual(report["result"]["details"]["hashes"][0], report["result"]["artifact_hash"])
        failing = ["python3", "-c", "import sys; sys.exit(1)"]
        report = validate_report(self.ws, "gpu-replicate",
                                 input_value={"job_ids": [first, second], "artifact": OUT, "compare": failing})
        self.assertFalse(report["result"]["accepted"])
        self.assertIn("compare exited 1", " ".join(report["result"]["details"]["problems"]))


class BrokerTests(GpuFixture):
    def setUp(self):
        super().setUp()
        self.rows = []
        self.service = service.GpuService(self.box, self.ws, self.state, self.config, session_id="sess-1")
        self.gpu_dir = self.ws / ".harness" / "gpu"
        self.broker = gpu_broker.GpuBroker(self.service, self.gpu_dir, "sess-1", ledger_append=self.rows.append)

    def shim(self, *argv, session="sess-1"):
        env = {**os.environ, "ADV_LOOP_GPU_DIR": str(self.gpu_dir), "ADV_LOOP_SESSION_ID": session}
        return subprocess.run([sys.executable, str(SHIM), *argv], env=env, capture_output=True, text=True, timeout=60)

    def test_shim_round_trip_through_the_broker_and_foreign_session_refusal(self):
        thread = gpu_broker.GpuBrokerThread(self.broker, interval=0.1)
        thread.start()
        try:
            status = self.shim("--status")
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            printed = json.loads(status.stdout)
            self.assertEqual(printed["hours"]["max"], 2.0)
            self.assertEqual(self.rows[0]["tool"], "gpu_status")
            run = self.shim("--inputs", "payload/scratch/data.txt", "--outputs", OUT, "--timeout", "30",
                            "--label", "shim", "--env", "HF_HOME=/tmp/hf", "--", *WRITE_OUT)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["outputs"][0]["path"], OUT)
            self.assertEqual((self.ws / OUT).read_text(), "gpu says hi\n")
            self.assertEqual(self.rows[1]["tool"], "gpu_run")
            self.assertEqual(self.rows[1]["exit_code"], 0)
            self.assertEqual(self.rows[1]["job"]["job_id"], result["job_id"])
            self.assertEqual(self.rows[1]["input"]["env"], {"HF_HOME": "/tmp/hf"})
            self.assertEqual(self.rows[1]["input"]["label"], "shim")
            self.assertEqual(self.host_record(result["job_id"])["tool_use_id"], self.rows[1]["tool_use_id"])
            foreign = self.shim("--status", session="other")
            self.assertEqual(foreign.returncode, 2)
            self.assertEqual(json.loads(foreign.stdout)["error"], "foreign_session")
            self.assertEqual(len(self.rows), 2)
            failing = self.shim("--outputs", OUT, "--timeout", "5", "--", "python3", "-c", "raise SystemExit(4)")
            self.assertEqual(failing.returncode, 1, failing.stdout)
            self.assertEqual(json.loads(failing.stdout)["returncode"], 4)
        finally:
            thread.stop()
        self.assertEqual(len(list(self.gpu_dir.glob("*.request.json"))), 0)
        self.assertEqual(len(list(self.gpu_dir.glob("*.request.done"))), 4)

    def test_run_requests_are_served_on_their_own_thread(self):
        request = self.gpu_dir / "r1.request.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(json.dumps({"request_id": "r1", "session_id": "sess-1", "kind": "run",
                                       "args": self.args(command=SLEEP, timeout_seconds=2)}), encoding="utf-8")
        self.assertEqual(self.broker.poll(), 1)
        self.assertEqual(len(self.broker.threads), 1)
        self.assertFalse((self.gpu_dir / "r1.result.json").exists())
        self.broker.join(30)
        result = json.loads((self.gpu_dir / "r1.result.json").read_text())
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(self.rows[0]["interrupted"])
        unknown = self.gpu_dir / "r2.request.json"
        unknown.write_text(json.dumps({"request_id": "r2", "session_id": "sess-1", "kind": "stop"}), encoding="utf-8")
        self.broker.poll()
        self.assertEqual(json.loads((self.gpu_dir / "r2.result.json").read_text())["error"], "unknown_kind")
        self.assertEqual(len(self.rows), 1)


if __name__ == "__main__":
    unittest.main()
