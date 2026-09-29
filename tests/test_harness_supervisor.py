"""The supervisor drives real kernel workspaces: retries, faults, pauses, passes, idle stop, and the drill."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from adv_loop.engine import LoopEngine

from harness import drill, pause, routing, supervisor, transcripts

REPO_ROOT = Path(__file__).resolve().parents[1]
MINIMAL = {"backend": "minimal", "model": "none", "provider": "local", "billing": "local"}


def local_router(cfg):
    return routing.Router(cfg, prices={}, env={}, token_exists=lambda p: True)


class SupervisorFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "fleet"
        self.root.mkdir()
        self.inbox = base / "inbox"
        self.inbox.mkdir()
        self.runtime = supervisor.Runtime(root=self.root, state_dir=base / "state", inbox=self.inbox,
                                          repo=REPO_ROOT, router_factory=local_router, pass_interval_seconds=0)

    def tearDown(self):
        self.tmp.cleanup()

    def workspace(self, name="ws-a", roles=None):
        engine = LoopEngine.create(self.root, "Solve it", [{"text": "it works", "min_formalization_rank": "executable_spec"}],
                                   {}, task_id=name)
        ws = engine.workspace
        supervisor.provision_workspace(self.runtime, ws)
        config = json.loads((ws / "loop-config.json").read_text())
        config["harness"]["roles"] = roles or {"default": MINIMAL, "critic": {**MINIMAL, "model": "critic-model"},
                                               "verifier": {**MINIMAL, "model": "verifier-model"}}
        (ws / "loop-config.json").write_text(json.dumps(config, indent=2, sort_keys=True))
        return engine, ws


class RunDirectiveTests(SupervisorFixture):
    def test_two_directives_land_and_transcripts_are_marked(self):
        engine, ws = self.workspace()
        first = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(first["driver_status"], "paused")
        self.assertEqual(first["accepted"], 1)
        second = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(second["accepted"], 1)
        rows = transcripts.load_index(ws)
        # The first drive also sizes the task's spend cap; that session is pre-flight, not loop work.
        self.assertEqual([r["mode"] for r in rows if r["mode"] != "budget"], ["initial_plan", "experiment"])
        self.assertEqual([r["mode"] for r in rows][0], "budget")
        self.assertTrue(all(r["accepted"] for r in rows if r["mode"] != "budget"))
        self.assertTrue(all(r["event_head_after"] for r in rows if r["mode"] != "budget"))
        state = engine.load()
        self.assertEqual(state["status"], "active")
        spend = [json.loads(l) for l in (ws / ".spend.jsonl").read_text().splitlines()]
        self.assertEqual([r["mode"] for r in spend if r["mode"] != "budget"], ["initial_plan", "experiment"])
        self.assertTrue(all(r["estimated_usd"] == 0.0 for r in spend))

    def test_lying_backend_is_rejected_retried_and_faulted_without_a_chain_attempt(self):
        engine, ws = self.workspace()
        supervisor.run_directive(self.runtime, ws)
        self.runtime.backend_overrides["minimal"] = {"lie": True}
        self.runtime.adapter_retries = 1
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["driver_status"], "fault")
        self.assertEqual(outcome["accepted"], 0)
        self.assertEqual(len(outcome["sessions"]), 2)
        self.assertTrue(all(f["error"] == "harness_contract" for f in outcome["feedback"]))
        rejections = [json.loads(l) for l in (ws / ".harness" / "rejections.jsonl").read_text().splitlines()]
        self.assertEqual(sorted({r["retry"] for r in rejections}), [0, 1])
        state = engine.load()
        self.assertEqual(len(state.get("process_faults") or []), 1)
        rows = transcripts.load_index(ws)
        # A refinement session now follows the directive, so select this directive's own sessions.
        by_id = {r["session_id"]: r for r in rows}
        self.assertEqual([by_id[sid]["accepted"] for sid in outcome["sessions"]], [False, False])
        self.assertEqual(sum(1 for r in rows if r["accepted"]), 1)

    def test_retry_feedback_reaches_the_next_session_prompt(self):
        engine, ws = self.workspace()
        supervisor.run_directive(self.runtime, ws)
        self.runtime.backend_overrides["minimal"] = {"lie": True}
        self.runtime.adapter_retries = 1
        outcome = supervisor.run_directive(self.runtime, ws)
        second_try = transcripts.session_dir(ws, outcome["sessions"][-1]) / "prompt.md"
        self.assertIn("rejected by the contract", second_try.read_text())
        self.assertIn("does not match", second_try.read_text())

    def test_subscription_rate_limit_pauses_fleet_wide_without_a_fault(self):
        sub = {"backend": "claude_code", "model": "claude-opus-5", "provider": "anthropic_subscription", "wire": "anthropic",
               "billing": "subscription", "key_file": "/nonexistent/token"}
        engine, ws = self.workspace(roles={"default": sub, "critic": {**MINIMAL, "model": "c"}, "verifier": {**MINIMAL, "model": "v"}})
        self.runtime.backend_overrides["claude_code"] = {
            "claude_binary": [sys.executable, str(REPO_ROOT / "harness" / "container" / "fake_claude.py")],
            "repo_mount": None, "hook_path": str(REPO_ROOT / "harness" / "container" / "ledger_hook.py"),
            "token_reader": lambda s: "tok",
            "env": {"PATH": __import__("os").environ.get("PATH", ""), "HOME": self.tmp.name, "FAKE_CLAUDE_SCENARIO": "rate_limit"}}
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["driver_status"], "paused")
        self.assertEqual(outcome["reason"], "rate_limited")
        self.assertTrue(pause.workspace_paused(ws))
        self.assertTrue(pause.subscription_paused(self.runtime.state_dir))
        self.assertFalse(engine.load().get("process_faults"))
        again = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(again["reason"], "subscription_paused")
        self.assertEqual(len(again["sessions"]), 0)

    def test_api_budget_stop_pauses_the_workspace(self):
        api = {"backend": "minimal", "model": "gpt-6-astra", "provider": "azure", "billing": "api", "base_url": "https://f",
               "price_in_usd": 1.0, "price_out_usd": 1.0, "context_window": 200000}
        engine, ws = self.workspace(roles={"default": api, "critic": {**MINIMAL, "model": "c"}, "verifier": {**MINIMAL, "model": "v"}})
        self.runtime.api_guard.state_file.write_text(json.dumps({"api_spend_usd": 1899.5, "degraded": True, "stopped": False}))
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["reason"], "api_budget_stop")
        self.assertTrue(pause.workspace_paused(ws))

    def test_routing_failure_blocks_instead_of_crashing(self):
        engine, ws = self.workspace(roles={"default": MINIMAL})
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["accepted"], 1)
        self.runtime.router_factory = lambda cfg: routing.Router(cfg, prices={}, env={}, token_exists=lambda p: False)
        config = json.loads((ws / "loop-config.json").read_text())
        config["harness"]["roles"] = {"default": {**MINIMAL, "billing": "subscription"}}
        (ws / "loop-config.json").write_text(json.dumps(config))
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["driver_status"], "blocked")
        self.assertIn("routing", outcome["reason"])


    def test_auth_errors_block_without_a_retry_burst(self):
        from harness.backends import http_chat
        api = {"backend": "http_chat", "model": "gpt-6-astra", "provider": "azure", "billing": "api", "base_url": "https://f",
               "wire": "responses", "price_in_usd": 1.0, "price_out_usd": 1.0, "context_window": 200000}
        engine, ws = self.workspace(roles={"default": api, "critic": {**MINIMAL, "model": "c"}, "verifier": {**MINIMAL, "model": "v"}})

        def transport(url, headers, body):
            raise http_chat.TransportError(401, "Unauthorized")
        self.runtime.backend_overrides["http_chat"] = {"transport": transport, "key_reader": lambda s: "k"}
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["driver_status"], "blocked")
        self.assertEqual(outcome["reason"], "auth_error_401")
        self.assertEqual(len(outcome["sessions"]), 1)
        self.assertTrue(pause.workspace_paused(ws))
        self.assertFalse(engine.load().get("process_faults"))


class PassTests(SupervisorFixture):
    def test_pass_drives_ready_workspaces_and_skips_paused_ones(self):
        _, a = self.workspace("ws-a")
        _, b = self.workspace("ws-b")
        pause.human_hold(b, "hold")
        for ws in (a, b):
            supervisor.provision_workspace(self.runtime, ws)
        import time
        for _ in range(50):
            if supervisor.workspace_ready(a) and supervisor.workspace_ready(b):
                break
            time.sleep(0.1)
        outcome = supervisor.pass_once(self.runtime)
        self.assertEqual(set(outcome["driven"]), {"ws-a"})
        self.assertEqual(outcome["driven"]["ws-a"]["accepted"], 1)
        self.assertEqual(outcome["skipped"]["ws-b"], "paused")
        self.assertFalse(outcome["idle"])
        pause.human_hold(a, "hold")
        outcome = supervisor.pass_once(self.runtime)
        self.assertEqual(outcome["driven"], {})
        self.assertTrue(outcome["idle"])

    def test_serve_stops_on_fleet_stop_and_on_idle_window(self):
        (self.root / supervisor.FLEET_STOP).write_text("")
        self.assertEqual(supervisor.serve(self.runtime, log=lambda s: None)["reason"], "fleet_stop")
        (self.root / supervisor.FLEET_STOP).unlink()
        self.runtime.idle_stop_minutes = 0
        self.runtime.shutdown_command = ["true"]
        outcome = supervisor.serve(self.runtime, log=lambda s: None)
        self.assertEqual(outcome["reason"], "idle_stop")
        self.assertTrue((self.runtime.state_dir / supervisor.WAKE_FILE).is_file())
        self.assertEqual(supervisor.serve(self.runtime, max_passes=1, log=lambda s: None)["reason"], "max_passes")

    def test_inbox_drop_becomes_a_provisioned_workspace(self):
        drop = self.inbox / "goal-1"
        drop.mkdir()
        (drop / "task.md").write_text("# Task\n\nProve a small lemma about lists.\n\n## Criteria\n\n- the lemma is machine-checked\n")
        outcome = supervisor.pass_once(self.runtime)
        self.assertTrue(outcome["intake"], outcome)
        created = [p for p in self.root.iterdir() if p.is_dir() and (p / "events.jsonl").is_file()]
        self.assertEqual(len(created), 1)
        self.assertTrue((created[0] / supervisor.READY_FILE).is_file())
        self.assertTrue((created[0] / ".checker-key").is_file())


class DrillTests(SupervisorFixture):
    def test_minimal_drill_passes_every_check(self):
        report = drill.run(self.runtime, backend="minimal")
        failed = [c for c in report["checks"] if not c["ok"]]
        self.assertEqual(failed, [], failed)

    def test_lying_drill_is_caught(self):
        report = drill.run(self.runtime, backend="minimal", lie=True)
        self.assertTrue(report["ok"], report["checks"])
        self.assertTrue(any(c["check"] == "lying_fingerprint_rejected_before_the_kernel" and c["ok"] for c in report["checks"]))

    def test_fake_claude_rate_limit_drill(self):
        report = drill.run(self.runtime, backend="claude_code", simulate_rate_limit=True)
        self.assertTrue(report["ok"], report["checks"])


if __name__ == "__main__":
    unittest.main()
