"""Container argv, toolchain pinning, the checker stamp, the validate broker, and provisioning; no docker needed."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import broker as brk
from harness import container as ct
from harness import ledger, provision

try:
    from test_engine import Harness
except ImportError:
    from tests.test_engine import Harness

REPO_ROOT = Path(__file__).resolve().parents[1]
STAMP = REPO_ROOT / "harness" / "container" / "toolchain_stamp.py"
CLIENT = REPO_ROOT / "harness" / "container" / "adv-validate"
FAKE_CHECKER = """
import json, sys
env = json.load(sys.stdin)
print(json.dumps({"accepted": True, "checker_id": "fake", "checker_version": "1.0",
                  "artifact_hash": "a" * 64, "log_hash": "b" * 64, "details": {"hook": env["hook"]}}))
"""


class ArgvTests(unittest.TestCase):
    def test_run_args_mount_the_workspace_at_its_own_path_and_mask_host_material(self):
        ws = Path("/srv/adv-loop/workspaces/demo")
        argv = ct.run_args(ws, "adv-ws-demo", "img:1", env={"ADV_LOOP_TOOLCHAIN_HASH": "x"},
                           repo=Path("/srv/adv-loop/repo"), masks=ct.default_masks(ws))
        self.assertIn(f"{ws}:{ws}:rw", argv)
        self.assertIn("/srv/adv-loop/repo:/srv/adv-loop/repo:ro", argv)
        self.assertIn(f"/dev/null:{ws}/.checker-key:ro", argv)
        self.assertTrue(any(a.startswith(f"{ws}/.harness/host:") for a in argv))
        self.assertIn("ADV_LOOP_TOOLCHAIN_HASH=x", argv)
        self.assertIn("--cap-drop", argv)
        self.assertEqual(argv[-3:], ["img:1", "sleep", "infinity"])
        self.assertEqual(argv[argv.index("--user") + 1], ct.SANDBOX_USER)

    def test_exec_prefix_is_the_sandbox_command(self):
        self.assertEqual(ct.exec_prefix("adv-ws-demo", Path("/w")),
                         ["docker", "exec", "-i", "-u", ct.SANDBOX_USER, "-w", "/w", "adv-ws-demo"])
        self.assertNotIn("-i", ct.exec_prefix("c", Path("/w"), interactive=False))

    def test_container_name_is_safe(self):
        self.assertEqual(ct.container_name("prove a/thing"), "adv-ws-prove-a-thing")


class LockTests(unittest.TestCase):
    def test_toolchain_hash_depends_on_image_lean_and_mathlib_only(self):
        a = ct.ContainerLock("img", "sha256:1", "leanprover/lean4:v4.29.0", "abc", z3_version="4.13")
        b = ct.ContainerLock("other-tag", "sha256:1", "leanprover/lean4:v4.29.0", "abc", z3_version="4.14")
        c = ct.ContainerLock("img", "sha256:2", "leanprover/lean4:v4.29.0", "abc")
        self.assertEqual(a.toolchain_hash, b.toolchain_hash)
        self.assertNotEqual(a.toolchain_hash, c.toolchain_hash)
        self.assertEqual(len(a.toolchain_hash), 64)

    def test_lock_round_trip_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            lock = ct.ContainerLock("img", "sha256:1", "lean", "rev", extra={"note": "x"})
            digest = ct.write_lock(ws, lock)
            self.assertEqual(ct.read_lock(ws), lock)
            from adv_loop.pathsafe import hash_file
            self.assertEqual(digest, hash_file(ct.lock_path(ws))[0])

    def test_parse_pins(self):
        done = subprocess.CompletedProcess(["x"], 0, stdout="lean=v1\nmathlib=abc\nbad\n", stderr="")
        self.assertEqual(ct.parse_pins(done), {"lean": "v1", "mathlib": "abc"})
        self.assertEqual(ct.parse_pins(subprocess.CompletedProcess(["x"], 1, stdout="lean=v1", stderr="")), {})


class StampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.checker = Path(self.tmp.name) / "fake_checker.py"
        self.checker.write_text(FAKE_CHECKER, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_stamp(self, env_hash, extra_argv=None):
        env = {**os.environ, "ADV_LOOP_TOOLCHAIN_HASH": env_hash} if env_hash is not None else \
            {k: v for k, v in os.environ.items() if k != "ADV_LOOP_TOOLCHAIN_HASH"}
        argv = [sys.executable, str(STAMP)] + (extra_argv or [sys.executable, str(self.checker)])
        return subprocess.run(argv, input=json.dumps({"hook": "h", "input": None}), text=True,
                              capture_output=True, env=env)

    def test_stamp_adds_the_toolchain_hash(self):
        done = self.run_stamp("C" * 64)
        self.assertEqual(done.returncode, 0, done.stderr)
        verdict = json.loads(done.stdout)
        self.assertEqual(verdict["toolchain_hash"], "c" * 64)
        self.assertEqual(verdict["details"], {"hook": "h"})

    def test_without_env_the_verdict_is_untouched(self):
        verdict = json.loads(self.run_stamp(None).stdout)
        self.assertNotIn("toolchain_hash", verdict)

    def test_failure_passes_through(self):
        done = self.run_stamp("d" * 64, [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])
        self.assertEqual(done.returncode, 2)
        self.assertIn("boom", done.stderr)


def register_fake(workspace: Path, script: Path) -> None:
    (workspace / "loop-config.json").write_text(json.dumps({
        "validators": {"suite": {"command": [sys.executable, str(script)], "rank": "executable_spec"}},
    }), encoding="utf-8")
    (workspace / ".checker-key").write_text("ab" * 32 + "\n", encoding="utf-8")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(self)
        self.ws = self.harness.engine.workspace
        self.script = self.ws / "fake_checker.py"
        self.script.write_text(FAKE_CHECKER, encoding="utf-8")
        register_fake(self.ws, self.script)
        self.broker = brk.Broker(self.ws, "sess-1")
        self.broker.request_dir.mkdir(parents=True)

    def drop(self, request_id="r1", session="sess-1", hook="suite"):
        path = self.broker.request_dir / f"{request_id}.request.json"
        path.write_text(json.dumps({"request_id": request_id, "session_id": session, "hook": hook, "input": {}}))
        return path

    def test_request_becomes_a_signed_host_record_and_an_unsigned_model_view(self):
        self.drop()
        self.assertEqual(self.broker.poll(), 1)
        answer = json.loads((self.broker.request_dir / "r1.verdict.json").read_text())
        self.assertEqual(answer["verdict_id"], "V0001")
        self.assertTrue(answer["ok"] and answer["accepted"])
        self.assertNotIn("attestation", answer["result"])
        self.assertNotIn("checker", answer["evidence_hint"])
        host = self.broker.verdicts["V0001"]["report"]
        self.assertEqual(len(host["result"]["attestation"]), 64)
        self.assertTrue((self.broker.host_dir / "verdicts.jsonl").is_file())
        self.assertFalse((self.broker.request_dir / "r1.request.json").exists())
        self.assertEqual(self.broker.poll(), 0)

    def test_evidence_verification_copies_the_host_record(self):
        self.drop()
        self.broker.poll()
        evidence, problems, _ = ledger.verify_evidence(self.ws, [{
            "ref": "e1", "kind": "artifact", "quality": "direct", "claim": "c", "method": "m",
            "independence_key": "k", "supports": ["C1"], "verdict_id": "V0001"}],
            verdicts=self.broker.session_verdicts())
        self.assertEqual(problems, [])
        self.assertEqual(evidence[0]["checker"]["attestation"], self.broker.verdicts["V0001"]["report"]["result"]["attestation"])
        self.assertEqual(evidence[0]["fingerprint"], "a" * 64)

    def test_foreign_session_and_unknown_hook_are_refused(self):
        self.drop("r2", session="other")
        self.drop("r3", hook="nope")
        self.broker.poll()
        for rid in ("r2", "r3"):
            answer = json.loads((self.broker.request_dir / f"{rid}.verdict.json").read_text())
            self.assertFalse(answer["ok"])
        self.assertEqual(self.broker.session_verdicts(), {})

    def test_kernel_contract_error_is_reported_not_raised(self):
        (self.ws / "loop-config.json").write_text(json.dumps({"validators": {"suite": {"command": [], "rank": "x"}}}))
        self.drop()
        self.broker.poll()
        answer = json.loads((self.broker.request_dir / "r1.verdict.json").read_text())
        self.assertFalse(answer["ok"])
        self.assertIn("failure", answer)

    def test_in_container_client_round_trip(self):
        thread = brk.BrokerThread(self.broker, interval=0.1)
        thread.start()
        try:
            done = subprocess.run(
                [sys.executable, str(CLIENT), "suite", "--json", "{}", "--timeout", "20"],
                env={**os.environ, "ADV_LOOP_VALIDATE_DIR": str(self.broker.request_dir), "ADV_LOOP_SESSION_ID": "sess-1"},
                capture_output=True, text=True, timeout=30)
        finally:
            thread.stop()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        printed = json.loads(done.stdout)
        self.assertEqual(printed["verdict_id"], "V0001")
        self.assertEqual(list(self.broker.session_verdicts()), ["V0001"])


class FakeDocker(ct.Docker):
    def __init__(self):
        super().__init__(runner=self._run)
        self.calls = []
        self.exists = False

    def _run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            if self.exists:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([{"State": {"Running": True}}]), stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such container")
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="sha256:feed\n", stderr="")
        if argv[:3] == ["docker", "run", "--rm"]:
            return subprocess.CompletedProcess(argv, 0, stdout="lean=leanprover/lean4:v4.29.0\nmathlib=abc123\nz3=4.13.4\nclaude-code=2.1.261\n", stderr="")
        if argv[:2] == ["docker", "run"]:
            self.exists = True
            return subprocess.CompletedProcess(argv, 0, stdout="cid\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(self)
        self.ws = self.harness.engine.workspace
        (self.ws / "loop-config.json").write_text(json.dumps({
            "profile": "formal-mathematics", "on_record": ["true"], "spend_ceiling_usd": 40,
            "validators": {"custom": {"command": ["python3", "x.py"], "rank": "sourced_claim"}},
        }), encoding="utf-8")
        self.docker = FakeDocker()

    def test_provision_writes_docker_blocks_and_preserves_foreign_keys(self):
        report = provision.provision(self.ws, self.docker, image="img:1", repo=Path("/srv/adv-loop/repo"))
        config = json.loads((self.ws / "loop-config.json").read_text())
        ws_abs = self.ws.resolve()
        name = ct.container_name(ws_abs.name)
        self.assertEqual(config["profile"], "formal-mathematics")
        self.assertEqual(config["spend_ceiling_usd"], 40)
        self.assertEqual(config["sandbox"]["kind"], "docker")
        self.assertEqual(config["sandbox"]["command"], ct.exec_prefix(name, ws_abs))
        self.assertEqual(config["sandbox"]["setup"][-2:], ["bash", "setup.sh"])
        self.assertIn("sandbox/container.lock", config["sandbox"]["lockfile_hashes"])
        self.assertIn("sandbox/setup.sh", config["sandbox"]["lockfile_hashes"])
        self.assertIn("custom", config["validators"])
        lean = config["validators"]["lean-kernel"]
        self.assertTrue(lean["in_sandbox"])
        self.assertEqual(lean["rank"], "kernel_proof")
        self.assertEqual(lean["command"][:2], ["python3", "/srv/adv-loop/repo/harness/container/toolchain_stamp.py"])
        self.assertEqual(lean["command"][-1], "/srv/adv-loop/repo/checkers/lean_kernel.py")
        self.assertEqual(config["checker_attestation"], {"require": True})
        self.assertEqual(config["harness"]["container"], name)
        self.assertEqual(config["harness"]["toolchain_hash"], report["toolchain_hash"])
        self.assertEqual(report["container_state"], "created")
        self.assertTrue(report["checker_key_created"])
        mode = stat.S_IMODE((self.ws / ".checker-key").stat().st_mode)
        self.assertEqual(mode, 0o600)
        lock = ct.read_lock(self.ws)
        self.assertEqual(lock.mathlib_rev, "abc123")
        self.assertEqual(lock.toolchain_hash, report["toolchain_hash"])
        template = json.loads(Path(report["overlay_template"]).read_text())
        self.assertEqual({op["op"] for op in template["ops"]}, {"pin_toolchain_hash"})
        self.assertEqual({op["toolchain_id"] for op in template["ops"]}, set(provision.CHECKERS))
        self.assertTrue(all(op["artifact_hash"] == report["toolchain_hash"] for op in template["ops"]))
        run = next(c for c in self.docker.calls if c[:2] == ["docker", "run"] and "-d" in c)
        self.assertIn(f"ADV_LOOP_TOOLCHAIN_HASH={report['toolchain_hash']}", run)
        self.assertIn(f"/dev/null:{ws_abs}/.checker-key:ro", run)
        # the kernel accepts the block: its sandbox reader must parse what we wrote
        from adv_loop.sandbox import sandbox_declaration
        declared = sandbox_declaration(self.ws)
        self.assertEqual(declared["kind"], "docker")
        self.assertEqual(declared["command"][0], "docker")

    def test_provision_is_idempotent(self):
        first = provision.provision(self.ws, self.docker, image="img:1")
        key = (self.ws / ".checker-key").read_text()
        second = provision.provision(self.ws, self.docker, image="img:1")
        self.assertEqual((self.ws / ".checker-key").read_text(), key)
        self.assertFalse(second["checker_key_created"])
        self.assertEqual(second["container_state"], "running")
        self.assertEqual(first["toolchain_hash"], second["toolchain_hash"])

    def test_missing_image_is_an_error(self):
        docker = FakeDocker()
        docker.runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no image")
        with self.assertRaises(RuntimeError):
            provision.provision(self.ws, docker, image="img:missing")


if __name__ == "__main__":
    unittest.main()
