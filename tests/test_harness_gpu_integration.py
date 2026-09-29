"""GPU wiring above the job runner: tools, backends, provisioning, the supervisor, the CLI, and the drill.

Everything runs against the FakeBox from tests/fake_gpu_box.py; no docker, no
aws, no ssh. The job runner itself is covered by test_harness_gpu.py.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from adv_loop.engine import LoopEngine

from harness import assembler, budget, cli, drill, pause, provision, routing, supervisor, tools, transcripts
from harness import gpu as gpu_module
from harness.backends import http_chat, make_backend
from harness.backends.base import ModelSpec, SessionBudget, SessionResult, WorkspaceHandle
from harness.backends.claude_code import ClaudeCodeBackend
from harness.backends.minimal import GPU_OUTPUT
from harness.gpu import jobs, lock, spend

try:
    from fake_gpu_box import FakeBox
    from test_harness_container import FakeDocker
    from test_harness_http_chat import AZURE_ANTHROPIC, AZURE_OPENAI, FakeTransport, anthropic_text, openai_text, valid_output
except ImportError:  # invoked as tests.<module> rather than via discovery
    from tests.fake_gpu_box import FakeBox
    from tests.test_harness_container import FakeDocker
    from tests.test_harness_http_chat import (AZURE_ANTHROPIC, AZURE_OPENAI, FakeTransport, anthropic_text, openai_text,
                                              valid_output)

REPO_ROOT = Path(__file__).resolve().parents[1]
MINIMAL = {"backend": "minimal", "model": "none", "provider": "local", "billing": "local"}
GPU_NAMES = set(gpu_module.TOOL_NAMES)


def local_router(cfg):
    return routing.Router(cfg, prices={}, env={}, token_exists=lambda p: True)


def directive(role="planner", mode="initial_plan"):
    return {"action": "attempt", "directive_id": f"d-{mode}", "role": role, "mode": mode, "task": "t",
            "open_criteria": [{"id": "C1", "text": "c"}], "instructions": [], "active_plan_id": "P0001"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StubService:
    """Stands in for a GpuService: records calls, answers with the tool tuple shape."""

    def __init__(self):
        self.calls = []

    def _answer(self, kind, args, tool_use_id):
        self.calls.append((kind, args, tool_use_id))
        payload = {"job_id": "g-20260912T000000Z-0000abcd", "status": "finished", "kind": kind}
        return payload, json.dumps(payload), "", {"exit_code": 0, "interrupted": False}

    def run(self, args, tool_use_id=None):
        return self._answer("run", args, tool_use_id)

    def collect(self, args, tool_use_id=None):
        return self._answer("collect", args, tool_use_id)

    def status(self, args, tool_use_id=None):
        return self._answer("status", args, tool_use_id)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gpu_tools_are_advertised_only_with_a_service(self):
        plain = {d["name"] for d in tools.tool_definitions()}
        granted = {d["name"] for d in tools.tool_definitions(gpu=True)}
        self.assertFalse(plain & GPU_NAMES)
        self.assertTrue(GPU_NAMES <= granted)
        self.assertEqual({d["name"] for d in tools.tool_definitions(gpu=True, allowed={"read_file", "gpu_run"})},
                         {"read_file", "gpu_run"})
        without = tools.ToolRunner(self.ws, self.ws / "l.jsonl")
        with_service = tools.ToolRunner(self.ws, self.ws / "l.jsonl", gpu=StubService())
        self.assertEqual({d["name"] for d in without.definitions}, plain)
        self.assertEqual({d["name"] for d in with_service.definitions}, granted)
        read_only = tools.ToolRunner(self.ws, self.ws / "l.jsonl", gpu=StubService(), allowed={"read_file"})
        self.assertEqual([d["name"] for d in read_only.definitions], ["read_file"])
        for convert, key in ((tools.openai_tools, lambda t: t["function"]["name"]), (tools.anthropic_tools, lambda t: t["name"])):
            self.assertEqual({key(t) for t in convert()}, plain)
            self.assertEqual({key(t) for t in convert(with_service.definitions)}, granted)

    def test_runner_refuses_without_a_grant_and_records_the_failure(self):
        runner = tools.ToolRunner(self.ws, self.ws / "ledger.jsonl")
        outcome = runner.run("gpu_run", {"command": ["true"], "outputs": ["payload/x"]}, tool_use_id="t1")
        self.assertEqual(outcome, {"error": "GPU compute is not granted for this workspace"})
        self.assertEqual(len(runner.entries), 1)
        self.assertEqual(runner.entries[0]["tool"], "gpu_run")
        self.assertIn("not granted", runner.entries[0]["error"])
        self.assertEqual(len((self.ws / "ledger.jsonl").read_text().splitlines()), 1)

    def test_runner_dispatches_to_the_service_with_the_tool_use_id(self):
        service = StubService()
        runner = tools.ToolRunner(self.ws, self.ws / "ledger.jsonl", gpu=service)
        outcome = runner.run("gpu_status", {}, tool_use_id="t9")
        self.assertEqual(outcome["kind"], "status")
        self.assertEqual(service.calls, [("status", {}, "t9")])
        self.assertEqual(runner.entries[-1]["exit_code"], 0)
        runner.run("gpu_collect", {"job_id": "g-x"})
        self.assertEqual(service.calls[-1][0], "collect")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "payload").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_make_backend_threads_the_service_factory_into_the_runner(self):
        service = StubService()
        seen = []

        def factory(handle, ledger_path):
            seen.append((handle.session_id, ledger_path))
            return service
        handle = WorkspaceHandle(ws_id="w", path=self.ws, session_id="sid")
        for backend_name in ("minimal", "http_chat"):
            spec = ModelSpec(backend=backend_name, model="m", provider="local", billing="local")
            backend = make_backend(spec, handle, gpu_service_factory=factory)
            runner = backend.tool_runner_factory(handle, self.ws / "ledger.jsonl")
            self.assertIs(runner.gpu, service)
            plain = make_backend(spec, handle).tool_runner_factory(handle, self.ws / "ledger.jsonl")
            self.assertIsNone(plain.gpu)
        self.assertEqual(len(seen), 2)

    def _run(self, spec, responses, service=None, budget_=None):
        d = directive()
        prompt = assembler.assemble(d)
        transport = FakeTransport(responses)
        factory = (lambda h, lp: tools.ToolRunner(h.path, lp, gpu=service)) if service else None
        backend = http_chat.HttpChatBackend(transport=transport, key_reader=lambda s: "KEY", tool_runner_factory=factory)
        handle = WorkspaceHandle(ws_id="w", path=self.ws, session_id=uuid.uuid4().hex)
        return backend.run(d, handle, spec, budget_ or SessionBudget(), prompt), transport

    def test_http_chat_wires_advertise_gpu_tools_only_with_a_service(self):
        output = json.dumps(valid_output("planner", "initial_plan"))
        _, transport = self._run(AZURE_OPENAI, [openai_text(output)])
        names = {t["function"]["name"] for t in transport.calls[0][2]["tools"]}
        self.assertFalse(names & GPU_NAMES)
        _, transport = self._run(AZURE_OPENAI, [openai_text(output)], service=StubService())
        names = {t["function"]["name"] for t in transport.calls[0][2]["tools"]}
        self.assertTrue(GPU_NAMES <= names)
        _, transport = self._run(AZURE_ANTHROPIC, [anthropic_text(output)], service=StubService())
        self.assertTrue(GPU_NAMES <= {t["name"] for t in transport.calls[0][2]["tools"]})
        _, transport = self._run(AZURE_ANTHROPIC, [anthropic_text(output)])
        self.assertFalse(GPU_NAMES & {t["name"] for t in transport.calls[0][2]["tools"]})

    def test_http_chat_preflight_guard_ends_the_session_before_any_request(self):
        small = ModelSpec(**{**AZURE_OPENAI.__dict__, "context_window": 1000})

        def transport(url, headers, body):
            raise AssertionError("no request may be sent when the prompt cannot fit")
        d = directive()
        backend = http_chat.HttpChatBackend(transport=transport, key_reader=lambda s: "KEY")
        handle = WorkspaceHandle(ws_id="w", path=self.ws, session_id=uuid.uuid4().hex)
        result = backend.run(d, handle, small, SessionBudget(), assembler.assemble(d))
        self.assertEqual(result.ended, "error_prompt_too_large")
        self.assertFalse(result.ok)
        self.assertEqual(result.usage["requests"], 0)
        self.assertGreater(result.usage["estimated_prompt_tokens"], 1000)
        self.assertEqual(result.cost_usd, 0.0)
        self.assertIn("context window", result.errors[0])
        self.assertEqual(result.tool_ledger, [])
        roomy = ModelSpec(**{**AZURE_OPENAI.__dict__, "context_window": 400000})
        result, transport_ = self._run(roomy, [openai_text(json.dumps(valid_output("planner", "initial_plan")))])
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.usage["requests"], 1)

    def test_claude_code_session_env_names_the_gpu_dir_and_stretches_bash_only_for_gpu_workspaces(self):
        handle = WorkspaceHandle(ws_id="w", path=self.ws, session_id="sid1")
        plain = ClaudeCodeBackend(repo_mount=None).session_env(handle, self.ws / "l.jsonl", self.ws / ".harness" / "validate")
        self.assertEqual(plain["ADV_LOOP_GPU_DIR"], str(self.ws / ".harness" / "gpu"))
        self.assertEqual(plain["ADV_LOOP_SESSION_ID"], "sid1")
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", plain)
        granted = ClaudeCodeBackend(repo_mount=None, gpu_max_job_seconds=14400).session_env(
            handle, self.ws / "l.jsonl", self.ws / ".harness" / "validate")
        self.assertEqual(granted["BASH_MAX_TIMEOUT_MS"], str((14400 + 900) * 1000))
        self.assertEqual(granted["BASH_DEFAULT_TIMEOUT_MS"], granted["BASH_MAX_TIMEOUT_MS"])
        self.assertEqual(granted["ADV_LOOP_GPU_MAX_JOB_SECONDS"], "14400")


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.engine = LoopEngine.create(base / "fleet", "Solve it", ["Result works"], {}, task_id="ws-gpu")
        self.ws = self.engine.workspace
        self.box = FakeBox(base / "remote")
        self.lock = lock.observe(self.box, image=gpu_module.DEFAULTS["image"])

    def tearDown(self):
        self.tmp.cleanup()

    def grant(self, **extra):
        config = provision.read_config(self.ws)
        config.setdefault("harness", {})["gpu"] = {"enabled": True, "max_hours": 3.0, **extra}
        provision.write_config(self.ws, config)

    def assert_gpu_block(self, config, report, toolchain_hash):
        block = config["harness"]["gpu"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["toolchain_hash"], toolchain_hash)
        self.assertEqual(block["image_digest"], self.lock.image_digest)
        self.assertEqual(block["driver"], self.lock.driver)
        self.assertEqual(block["max_hours"], 3.0)
        self.assertEqual(block["max_job_seconds"], gpu_module.DEFAULTS["max_job_seconds"])
        self.assertTrue(block["provisioned_at"].endswith("Z"))
        self.assertEqual(config["sandbox"]["lockfile_hashes"]["sandbox/gpu.lock"], lock.lock_sha256(self.ws))
        self.assertEqual(lock.read_workspace_lock(self.ws), self.lock)
        for name, (script, rank, timeout) in provision.GPU_CHECKERS.items():
            hook = config["validators"][name]
            self.assertFalse(hook["in_sandbox"])
            self.assertEqual(hook["rank"], rank)
            self.assertEqual(hook["timeout_seconds"], timeout)
            self.assertTrue(hook["command"][-1].endswith(script))
        for key in provision.GPU_BUDGET_KEYS:
            self.assertEqual(config["harness"]["budgets"][key]["wall_clock_seconds"], 14400 + 3600)
            self.assertEqual(config["harness"]["budgets"][key]["max_tool_turns"], SessionBudget().max_tool_turns)
        pins = json.loads((self.ws / ".harness" / "overlay-templates" / "pin-toolchain.json").read_text())["ops"]
        gpu_pins = {op["toolchain_id"]: op["artifact_hash"] for op in pins if op["toolchain_id"] in provision.GPU_CHECKERS}
        self.assertEqual(gpu_pins, {name: toolchain_hash for name in provision.GPU_CHECKERS})
        sandbox = json.loads((self.ws / ".harness" / "overlay-templates" / "register-gpu-sandbox.json").read_text())["ops"]
        self.assertEqual(sandbox, [{"op": "register_sandbox", "sandbox_id": "gpu-box", "description": sandbox[0]["description"],
                                    "mechanism_locator": "harness/gpu/jobs.py"}])
        self.assertEqual(report["gpu"]["enabled"], True)
        self.assertEqual(report["gpu"]["toolchain_hash"], toolchain_hash)
        self.assertTrue((self.ws / ".harness" / "architecture" / "manifest.json").is_file())

    def test_provision_local_with_the_grant_writes_the_block_hooks_lock_budgets_and_templates(self):
        self.grant()
        report = provision.provision_local(self.ws, repo=REPO_ROOT, gpu_lock=self.lock)
        config = provision.read_config(self.ws)
        self.assert_gpu_block(config, report, self.lock.toolchain_hash)
        self.assertEqual(config["validators"]["gpu-replay"]["command"], ["python3", str(REPO_ROOT / "checkers" / "gpu_replay.py")])
        self.assertIn("gpu-replay", report["hooks"])
        plain = provision.provision_local(LoopEngine.create(self.ws.parent, "x", ["y"], {}, task_id="plain").workspace, repo=REPO_ROOT)
        self.assertEqual(plain["gpu"], {"enabled": False})

    def test_provision_local_gpu_flag_uses_a_placeholder_lock_for_drills(self):
        report = provision.provision_local(self.ws, repo=REPO_ROOT, gpu=True)
        config = provision.read_config(self.ws)
        self.assertTrue(config["harness"]["gpu"]["enabled"])
        self.assertEqual(config["harness"]["gpu"]["toolchain_hash"], provision.FAKE_GPU_LOCK.toolchain_hash)
        self.assertEqual(lock.read_workspace_lock(self.ws), provision.FAKE_GPU_LOCK)
        self.assertEqual(report["gpu"]["toolchain_hash"], provision.FAKE_GPU_LOCK.toolchain_hash)

    def test_provision_fails_loudly_without_a_fleet_lock_and_writes_the_block_with_one(self):
        self.grant()
        with self.assertRaises(RuntimeError) as caught:
            provision.provision(self.ws, FakeDocker(), image="img:1", repo=Path("/srv/adv-loop/repo"))
        self.assertIn("no fleet GPU lock", str(caught.exception))
        self.assertIn("provision-box", str(caught.exception))
        report = provision.provision(self.ws, FakeDocker(), image="img:1", repo=Path("/srv/adv-loop/repo"), gpu_lock=self.lock)
        config = provision.read_config(self.ws)
        self.assert_gpu_block(config, report, self.lock.toolchain_hash)
        self.assertEqual(config["validators"]["gpu-replay"]["command"], ["python3", "/srv/adv-loop/repo/checkers/gpu_replay.py"])
        self.assertIn("sandbox/container.lock", config["sandbox"]["lockfile_hashes"])
        container_pins = [op for op in json.loads(Path(report["overlay_template"]).read_text())["ops"]
                          if op["toolchain_id"] in provision.CHECKERS]
        self.assertEqual({op["artifact_hash"] for op in container_pins}, {report["toolchain_hash"]})
        self.assertNotEqual(report["toolchain_hash"], self.lock.toolchain_hash)

    def test_provision_without_the_grant_is_unchanged_and_gpu_config_can_grant(self):
        report = provision.provision(self.ws, FakeDocker(), image="img:1")
        config = provision.read_config(self.ws)
        self.assertNotIn("gpu", config["harness"])
        self.assertNotIn("gpu-replay", config["validators"])
        self.assertEqual(report["gpu"], {"enabled": False})
        report = provision.provision(self.ws, FakeDocker(), image="img:1", gpu_lock=self.lock, gpu_config={"enabled": True})
        self.assertTrue(provision.read_config(self.ws)["harness"]["gpu"]["enabled"])
        self.assertTrue(report["gpu"]["enabled"])

    def test_operator_budgets_are_kept(self):
        self.grant()
        config = provision.read_config(self.ws)
        config["harness"]["budgets"] = {"researcher/experiment": {"max_budget_usd": 1.0, "wall_clock_seconds": 60}}
        provision.write_config(self.ws, config)
        provision.provision_local(self.ws, repo=REPO_ROOT, gpu_lock=self.lock)
        budgets = provision.read_config(self.ws)["harness"]["budgets"]
        self.assertEqual(budgets["researcher/experiment"], {"max_budget_usd": 1.0, "wall_clock_seconds": 60})
        self.assertEqual(budgets["verifier/independent_verification"]["wall_clock_seconds"], 14400 + 3600)


class SupervisorFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "fleet"
        self.root.mkdir()
        self.box = FakeBox(base / "remote")
        self.runtime = supervisor.Runtime(root=self.root, state_dir=base / "state", repo=REPO_ROOT,
                                          router_factory=local_router, pass_interval_seconds=0, gpu=self.box)
        self.lock = lock.observe(self.box, image=gpu_module.DEFAULTS["image"])
        lock.write_fleet_lock(self.runtime.state_dir, self.lock)
        self.box.calls.clear()

    def tearDown(self):
        self.box.wait_all()
        self.tmp.cleanup()

    def workspace(self, name="ws-gpu", gpu=True, roles=None, max_hours=2.0):
        engine = LoopEngine.create(self.root, "Solve it", [{"text": "it works", "min_formalization_rank": "executable_spec"}],
                                   {}, task_id=name)
        ws = engine.workspace
        config = provision.read_config(ws)
        config.setdefault("harness", {})["roles"] = roles or {
            "default": MINIMAL, "critic": {**MINIMAL, "model": "critic-model"}, "verifier": {**MINIMAL, "model": "verifier-model"}}
        if gpu:
            config["harness"]["gpu"] = {"enabled": True, "max_hours": max_hours, "queue_wait_seconds": 0}
        provision.write_config(ws, config)
        supervisor.provision_workspace(self.runtime, ws)
        return engine, ws

    def wait_ready(self, *workspaces):
        for _ in range(100):
            if all(supervisor.workspace_ready(ws) for ws in workspaces):
                return
            time.sleep(0.05)
        raise AssertionError("sandbox init did not finish")


class RunSessionTests(SupervisorFixture):
    def test_researcher_runs_a_gpu_job_and_the_kernel_accepts_the_attested_attempt(self):
        engine, ws = self.workspace()
        self.runtime.backend_overrides["minimal"] = {"gpu": True}
        first = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(first["accepted"], 1)
        second = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(second["accepted"], 1, second)
        rows = transcripts.load_index(ws)
        self.assertEqual([r["mode"] for r in rows if r["mode"] != "budget"], ["initial_plan", "experiment"])
        entries = transcripts._read_ledger(transcripts.session_dir(ws, rows[-1]["session_id"]))
        names = [e["tool"] for e in entries]
        self.assertEqual(names, ["run_in_sandbox", "write_file", "gpu_run", "validate"])
        job_row = entries[2]
        self.assertEqual(job_row["exit_code"], 0)
        self.assertEqual(job_row["job"]["status"], "collected")
        self.assertEqual(job_row["job"]["toolchain_hash"], self.lock.toolchain_hash)
        job_id = job_row["job"]["job_id"]
        self.assertEqual((ws / GPU_OUTPUT).read_text(), "gpu-drill\n")
        self.assertEqual(job_row["job"]["outputs"][0]["sha256"], sha(ws / GPU_OUTPUT))
        # the planner session had the grant too but ran no job; nothing crossed for it
        self.assertEqual(sorted(set(self.box.pushed)), sorted({f".harness/gpu/jobs/{job_id}/job.json"}))
        self.assertEqual(sorted(set(self.box.pulled)), sorted({GPU_OUTPUT, f".harness/gpu/jobs/{job_id}"}))
        state = engine.load()
        stored = state.get("evidence") or []
        evidence = list(stored.values()) if isinstance(stored, dict) else list(stored)
        item = evidence[-1]
        self.assertEqual(item["checker"]["checker_id"], "gpu-replay")
        self.assertTrue(item["checker"]["accepted"])
        self.assertEqual(item["checker"]["toolchain_hash"], self.lock.toolchain_hash)
        self.assertEqual(item["formalization_rank"], "executable_spec")
        self.assertEqual(item["fingerprint"], sha(ws / GPU_OUTPUT))
        self.assertTrue(item["locator"].startswith("validate:gpu-replay"))
        ws_rows = budget.gpu_spend_rows(ws / gpu_module.GPU_SPEND_FILE, "job")
        fleet_rows = budget.gpu_spend_rows(self.runtime.state_dir / gpu_module.STATE_SPEND_FILE, "job")
        self.assertEqual([r["job_id"] for r in ws_rows], [job_id])
        self.assertEqual([r["job_id"] for r in fleet_rows], [job_id])
        api_rows = [json.loads(l) for l in (ws / ".spend.jsonl").read_text().splitlines()]
        # Two loop sessions plus the pre-flight budget session; GPU hours never enter this ledger.
        self.assertEqual(len(api_rows), 3)
        self.assertTrue(all(r["estimated_usd"] == 0.0 and "job_id" not in r for r in api_rows))
        self.assertEqual(budget.ledger_total(ws / ".spend.jsonl"), 0.0)
        self.assertEqual(self.box.registered, {})
        self.assertEqual(self.box.released, [job_id])
        self.assertFalse((ws / pause.PAUSE_FILE).exists())

    def test_session_without_the_grant_gets_no_gpu_tools_and_no_gpu_chapter(self):
        _, ws = self.workspace(name="ws-cpu", gpu=False)
        self.runtime.backend_overrides["minimal"] = {"gpu": True}
        supervisor.run_directive(self.runtime, ws)
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["driver_status"], "fault")
        # gpu-replay is not a registered hook here, so the verdict view carries no id and the evidence is rejected
        self.assertIn("verdict_id", outcome["feedback"][0]["message"])
        # A refinement session follows the directive, so read this directive's own last session.
        last = transcripts.session_dir(ws, outcome["sessions"][-1])
        entries = transcripts._read_ledger(last)
        gpu_rows = [e for e in entries if e["tool"] == "gpu_run"]
        self.assertIn("not granted", gpu_rows[0]["error"])
        self.assertEqual(self.box.names(), [])
        manifest = json.loads((last / "prompt.manifest.json").read_text())
        self.assertFalse(manifest["gpu"])

    def test_run_session_passes_the_architecture_tier_and_the_gpu_flag_to_the_assembler(self):
        _, ws = self.workspace(roles={"default": {**MINIMAL, "architecture_tier": "core"},
                                      "critic": {**MINIMAL, "model": "c"}, "verifier": {**MINIMAL, "model": "v"}})
        supervisor.run_directive(self.runtime, ws)
        rows = transcripts.load_index(ws)
        manifest = json.loads((transcripts.session_dir(ws, rows[-1]["session_id"]) / "prompt.manifest.json").read_text())
        self.assertEqual(manifest["architecture_tier"], "core")
        self.assertTrue(manifest["gpu"])
        self.assertTrue((ws / ".harness" / "architecture" / "ARCHITECTURE.md").is_file())

    def test_claude_code_sessions_get_the_gpu_broker_and_the_bash_timeout_override(self):
        sub = {"backend": "claude_code", "model": "claude-opus-5", "provider": "anthropic_subscription", "wire": "anthropic",
               "billing": "subscription", "key_file": "/nonexistent/token"}
        _, ws = self.workspace(roles={"default": sub, "critic": {**MINIMAL, "model": "c"}, "verifier": {**MINIMAL, "model": "v"}})
        captured = {}

        class StubBackend:
            name = "claude_code"

            def run(self, directive_, handle, spec, budget_, prompt):
                request = {"request_id": "r1", "session_id": handle.session_id, "kind": "status", "args": {}}
                gpu_dir = handle.path / ".harness" / "gpu"
                gpu_dir.mkdir(parents=True, exist_ok=True)
                (gpu_dir / "r1.request.json").write_text(json.dumps(request))
                for _ in range(100):
                    if (gpu_dir / "r1.result.json").is_file():
                        break
                    time.sleep(0.05)
                captured["answer"] = json.loads((gpu_dir / "r1.result.json").read_text())
                return SessionResult(session_id=handle.session_id, ended="no_output", session_dir=handle.session_dir)

        def fake_make_backend(spec, handle, **kwargs):
            captured["overrides"] = kwargs.get("overrides")
            captured["factory"] = kwargs.get("gpu_service_factory")
            return StubBackend()
        engine = LoopEngine(ws)
        d = engine.next_instruction()
        assignment = self.runtime.router_factory(provision.read_config(ws)).resolve(d["role"], d["mode"])
        with mock.patch.object(supervisor, "make_backend", fake_make_backend):
            outcome = supervisor.run_session(self.runtime, ws, d, [], assignment)
        self.assertEqual(captured["overrides"]["gpu_max_job_seconds"], gpu_module.DEFAULTS["max_job_seconds"])
        self.assertIsNotNone(captured["factory"])
        self.assertEqual(captured["answer"]["enabled"], True)
        self.assertEqual(captured["answer"]["hours"]["max"], 2.0)
        self.assertEqual(outcome["problems"][0]["error"], "session_no_output")
        ledger_rows = [json.loads(l) for l in (ws / ".harness" / "sessions" / outcome["session_id"] / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual([r["tool"] for r in ledger_rows], ["gpu_status"])


class RunDirectiveTests(SupervisorFixture):
    def test_gpu_hour_cap_pauses_the_workspace_before_any_session(self):
        engine, ws = self.workspace(max_hours=1.0)
        spend.record_job_spend(ws, self.runtime.state_dir, {"job_id": "g-20260101T000000Z-00000000", "session_id": "old",
                                                              "seconds_billed": 3600, "hours_billed": 1.0, "status": "collected"})
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome, {"driver_status": "paused", "reason": "gpu_budget_stop", "accepted": 0,
                                   "rejections": 0, "sessions": []})
        record = pause.read(ws / pause.PAUSE_FILE)
        self.assertEqual(record["reason"], pause.GPU_BUDGET_STOP)
        self.assertTrue(record["indefinite"])
        self.assertEqual((record["hours_used"], record["max_hours"]), (1.0, 1.0))
        self.assertTrue(pause.workspace_paused(ws))
        self.assertFalse(engine.load().get("process_faults"))
        self.assertEqual(supervisor.pass_once(self.runtime)["skipped"][ws.name], "paused")

    def test_prompt_too_large_faults_once_without_a_retry_burst(self):
        api = {"backend": "http_chat", "model": "gpt-6-astra", "provider": "azure", "billing": "api", "base_url": "https://f",
               "wire": "responses", "price_in_usd": 1.0, "price_out_usd": 1.0, "context_window": 1000}
        engine, ws = self.workspace(gpu=False, roles={"default": api, "critic": {**MINIMAL, "model": "c"},
                                                      "verifier": {**MINIMAL, "model": "v"}})
        calls = []

        def transport(url, headers, body):
            calls.append(url)
            raise AssertionError("never sent")
        self.runtime.backend_overrides["http_chat"] = {"transport": transport, "key_reader": lambda s: "k"}
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["driver_status"], "fault")
        self.assertEqual(len(outcome["sessions"]), 1)
        self.assertEqual(calls, [])
        self.assertEqual(outcome["feedback"][0]["error"], "session_error_prompt_too_large")
        self.assertEqual(len(engine.load().get("process_faults") or []), 1)
        self.assertFalse(pause.workspace_paused(ws))


class PassAndServeTests(SupervisorFixture):
    def register(self, job_id="g-20260912T000000Z-0000abcd"):
        self.box.register_job(job_id, workspace=str(self.root / "ws-gpu"), session_id="other-process", timeout_seconds=60)
        return job_id

    def test_pass_sweeps_and_reaps_and_an_active_job_keeps_the_fleet_awake(self):
        outcome = supervisor.pass_once(self.runtime)
        self.assertTrue(outcome["idle"])
        self.assertEqual(outcome["gpu"]["sweep"], {"settled": [], "still_running": [], "errors": []})
        self.assertEqual(outcome["gpu"]["reap"]["stop"]["stopped"], True)
        self.assertEqual(outcome["gpu"]["active_jobs"], [])
        self.assertIn("reap", self.box.names())
        job_id = self.register()
        outcome = supervisor.pass_once(self.runtime)
        self.assertFalse(outcome["idle"])
        self.assertEqual(outcome["gpu"]["active_jobs"], [job_id])
        self.assertEqual(outcome["gpu"]["reap"]["stop"], {"stopped": False, "reason": "jobs_active"})

    def test_pass_survives_a_broken_box(self):
        class Broken(FakeBox):
            def reap(self):
                raise RuntimeError("aws is down")
        self.runtime.gpu = Broken(self.box.remote_root)
        outcome = supervisor.pass_once(self.runtime)
        self.assertIn("aws is down", outcome["gpu"]["reap_error"])
        self.assertTrue(outcome["idle"])

    def test_pass_without_a_box_reports_gpu_disabled(self):
        self.runtime.gpu = None
        outcome = supervisor.pass_once(self.runtime)
        self.assertEqual(outcome["gpu"], {"enabled": False, "active_jobs": []})

    def test_serve_stops_the_box_before_the_idle_shutdown_and_defers_while_a_job_runs(self):
        self.runtime.idle_stop_minutes = 0
        self.runtime.shutdown_command = ["true"]
        job_id = self.register()
        outcome = supervisor.serve(self.runtime, max_passes=2, log=lambda s: None)
        self.assertEqual(outcome["reason"], "max_passes")
        self.assertNotIn("harness_idle_stop", self.box.stops)
        self.assertFalse((self.runtime.state_dir / supervisor.WAKE_FILE).exists())
        self.box.release_job(job_id)
        logs = []
        outcome = supervisor.serve(self.runtime, log=logs.append)
        self.assertEqual(outcome["reason"], "idle_stop")
        self.assertEqual(self.box.stops[-1], "harness_idle_stop")
        self.assertTrue((self.runtime.state_dir / supervisor.WAKE_FILE).is_file())
        self.assertTrue(any("harness_idle_stop" in line for line in logs))

    def test_serve_skips_the_shutdown_when_a_job_appears_between_the_pass_and_the_stop(self):
        answers = [[], [{"job_id": "g-late"}], [], []]

        class Late(FakeBox):
            def active_jobs(self):
                return answers.pop(0) if answers else []
        self.runtime.gpu = Late(self.box.remote_root)
        self.runtime.idle_stop_minutes = 0
        self.runtime.shutdown_command = ["true"]
        logs = []
        outcome = supervisor.serve(self.runtime, log=logs.append)
        self.assertEqual(outcome["reason"], "idle_stop")
        self.assertTrue(any("idle_stop_deferred" in line for line in logs))
        self.assertEqual(self.runtime.gpu.stops[-1], "harness_idle_stop")
        self.assertEqual(answers, [])

    def test_fleet_stop_stops_the_box(self):
        (self.root / supervisor.FLEET_STOP).write_text("")
        outcome = supervisor.serve(self.runtime, log=lambda s: None)
        self.assertEqual(outcome["reason"], "fleet_stop")
        self.assertEqual(self.box.stops, ["fleet_stop"])
        self.assertEqual(self.box.state, "stopped")

    def test_supplemental_text_lists_jobs_awaiting_collection(self):
        _, ws = self.workspace()
        self.assertIsNone(supervisor.supplemental_text(ws, "researcher", "experiment"))
        store = jobs.JobStore(ws)
        job = jobs.Job(job_id="g-20260912T000000Z-0000abcd", workspace=str(ws), ws_id=ws.name, session_id="gone",
                       label="train", outputs_declared=["payload/derived/model.pt"], status="finished", outcome="finished",
                       outputs_ready=True)
        store.write(job)
        text = supervisor.supplemental_text(ws, "researcher", "experiment")
        self.assertIn("gpu jobs awaiting collection", text)
        self.assertIn("g-20260912T000000Z-0000abcd 'train': status finished", text)
        self.assertIn("payload/derived/model.pt", text)

    def test_runtime_loads_no_box_without_the_environment_and_logs_a_broken_one(self):
        with mock.patch.dict(os.environ, {"ADV_LOOP_GPU_BOX": "", "ADV_LOOP_GPU_ENABLED": ""}):
            runtime = supervisor.Runtime(root=self.root, state_dir=self.runtime.state_dir / "plain")
        self.assertIsNone(runtime.gpu)
        with mock.patch.dict(os.environ, {"ADV_LOOP_GPU_ENABLED": "1"}), \
                mock.patch.object(supervisor.box_api, "load_box", side_effect=RuntimeError("no aws here")):
            runtime = supervisor.Runtime(root=self.root, state_dir=self.runtime.state_dir / "broken")
        self.assertIsNone(runtime.gpu)
        self.assertIn("no aws here", (runtime.state_dir / supervisor.GPU_BOX_ERROR_FILE).read_text())
        with mock.patch.dict(os.environ, {"ADV_LOOP_GPU_ENABLED": "1"}), \
                mock.patch.object(supervisor.box_api, "load_box", return_value=self.box) as loader:
            runtime = supervisor.Runtime(root=self.root, state_dir=self.runtime.state_dir / "loaded", repo=REPO_ROOT)
        self.assertIs(runtime.gpu, self.box)
        self.assertEqual(loader.call_args.kwargs["repo"], REPO_ROOT)
        self.assertIs(loader.call_args.kwargs["guard"], runtime.aws_guard)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "fleet").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_gpu_status_and_spend_print_without_a_box(self):
        code, text = self.run_cli(["gpu", "status", "--state", str(self.base / "state"), "--root", str(self.base / "fleet")])
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["fleet_lock"], None)
        self.assertEqual(report["running"], [])
        self.assertEqual(report["spend_usd"], 0.0)
        self.assertEqual(report["workspaces"], {})
        self.assertIn("active_jobs", report)
        code, text = self.run_cli(["gpu", "spend", "--state", str(self.base / "state")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["fleet_usd"], 0.0)
        code, text = self.run_cli(["gpu", "jobs", "--state", str(self.base / "state"), "--status", "running"])
        self.assertEqual((code, json.loads(text)), (0, []))

    def test_status_reports_gpu_state_and_spend(self):
        budget.record_gpu_spend(self.base / "state" / gpu_module.STATE_SPEND_FILE,
                                {"kind": "box_run", "usd": 2.5, "instance_id": "i-x"})
        code, text = self.run_cli(["status", "--root", str(self.base / "fleet"), "--state", str(self.base / "state")])
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["gpu_spend_usd"], 2.5)
        self.assertEqual(report["gpu"], {"box": None, "fleet_lock": False, "running": []})

    def test_route_prints_context_window_and_architecture_tier(self):
        env = {"AZURE_OPENAI_ENDPOINT": "https://o.example", "AZURE_AI_ENDPOINT": "https://a.example"}
        with mock.patch.dict(os.environ, env):
            code, text = self.run_cli(["route"])
        rows = json.loads(text)
        self.assertEqual(code, 0, rows)
        self.assertEqual(rows["planner"]["context_window"], 200000)
        self.assertEqual(rows["planner"]["architecture_tier"], "role")
        self.assertEqual(rows["explorer"]["architecture_tier"], "lean")
        self.assertEqual(rows["researcher"]["model"], "grok-4-6")

    def test_drill_gpu_from_the_cli_uses_the_fake_box(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["drill", "--gpu", "--root", str(self.base / "fleet"), "--state", str(self.base / "state"),
                             "--repo", str(REPO_ROOT)])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, [c for c in report["checks"] if not c["ok"]])
        self.assertTrue((self.base / "state" / "fake-gpu-remote").is_dir())


class DrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "fleet"
        self.root.mkdir()
        self.box = FakeBox(base / "remote")
        self.runtime = supervisor.Runtime(root=self.root, state_dir=base / "state", repo=REPO_ROOT,
                                          router_factory=local_router, pass_interval_seconds=0, gpu=self.box)

    def tearDown(self):
        self.box.wait_all()
        self.tmp.cleanup()

    def test_gpu_drill_passes_every_named_check(self):
        report = drill.run(self.runtime, backend="minimal", gpu=True)
        failed = [c for c in report["checks"] if not c["ok"]]
        self.assertEqual(failed, [], failed)
        names = [c["check"] for c in report["checks"]]
        for expected in ("gpu_provisioned_with_lock_and_hooks", "gpu_tools_advertised_only_when_granted",
                         "gpu_job_ran_between_turns", "declared_outputs_pulled_and_hashed", "undeclared_files_not_pulled",
                         "evidence_from_gpu_output_carries_no_unobserved_warning",
                         "gpu_replay_verdict_signed_with_toolchain_hash", "tampered_output_rejected_by_gpu_replay",
                         "kernel_accepted_attempt_with_gpu_verdict", "gpu_spend_in_gpu_ledgers_not_in_api_ledger",
                         "pin_template_includes_gpu_checkers", "audit_clean"):
            self.assertIn(expected, names)
        self.assertIsNotNone(lock.read_fleet_lock(self.runtime.state_dir))
        ws = Path(report["workspace"])
        self.assertEqual((ws / GPU_OUTPUT).read_text(), "gpu-drill\n")
        self.assertFalse((ws / drill.UNDECLARED).exists())

    def test_gpu_drill_needs_a_box_and_the_minimal_backend(self):
        self.runtime.gpu = None
        with self.assertRaises(ValueError):
            drill.run(self.runtime, backend="minimal", gpu=True)
        with self.assertRaises(ValueError):
            drill.run(self.runtime, backend="claude_code", gpu=True)


if __name__ == "__main__":
    unittest.main()
