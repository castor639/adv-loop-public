"""The per-task spend cap: sized before the work, clamped on read, enforced on every drive path."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adv_loop.engine import LoopEngine

from harness import pause, provision, routing, supervisor, taskbudget, transcripts
from harness.backends.base import SessionResult

REPO_ROOT = Path(__file__).resolve().parents[1]
MINIMAL = {"backend": "minimal", "model": "none", "provider": "local", "billing": "local"}


def local_router(cfg):
    return routing.Router(cfg, prices={}, env={}, token_exists=lambda p: True)


class ClampTests(unittest.TestCase):
    def test_an_unvalidated_charter_number_is_clamped_on_the_way_out(self):
        # A charter's harness block reaches loop-config.json through a shallow merge with no schema,
        # so the clamp has to live on the read side or a model-authored number goes straight through.
        cap = taskbudget.read({"harness": {"task_budget": {"max_usd": 999999, "max_gpu_hours": 500}}})
        self.assertEqual(cap["max_usd"], taskbudget.MAX_TASK_USD)
        self.assertEqual(cap["max_gpu_hours"], taskbudget.MAX_TASK_GPU_HOURS)

    def test_a_cap_below_the_floor_is_raised_and_nonsense_falls_back(self):
        cap = taskbudget.read({"harness": {"task_budget": {"max_usd": 0.01, "max_gpu_hours": -3}}})
        self.assertEqual(cap["max_usd"], taskbudget.MIN_TASK_USD)
        self.assertEqual(cap["max_gpu_hours"], 0.0)
        nonsense = taskbudget.read({"harness": {"task_budget": {"max_usd": True, "max_gpu_hours": "lots"}}})
        self.assertEqual(nonsense["max_usd"], taskbudget.DEFAULT_TASK_USD)
        self.assertEqual(nonsense["max_gpu_hours"], taskbudget.DEFAULT_TASK_GPU_HOURS)

    def test_no_block_means_no_cap(self):
        self.assertIsNone(taskbudget.read({}))
        self.assertIsNone(taskbudget.read({"harness": {}}))


class EnforcementTests(unittest.TestCase):
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

    def workspace(self, name="ws-budget"):
        engine = LoopEngine.create(self.root, "Solve it", [{"text": "it works"}], {}, task_id=name)
        ws = engine.workspace
        supervisor.provision_workspace(self.runtime, ws)
        config = json.loads((ws / "loop-config.json").read_text())
        config["harness"]["roles"] = {"default": MINIMAL, "critic": {**MINIMAL, "model": "critic-model"},
                                      "verifier": {**MINIMAL, "model": "verifier-model"}}
        (ws / "loop-config.json").write_text(json.dumps(config, indent=2, sort_keys=True))
        return engine, ws

    def spend(self, ws, usd):
        with (ws / ".spend.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"estimated_usd": usd, "mode": "experiment"}) + "\n")

    def test_both_pots_count_toward_one_dollar_cap(self):
        _, ws = self.workspace()
        config = {"harness": {"task_budget": {"max_usd": 20.0, "max_gpu_hours": 0}}}
        self.spend(ws, 12.0)
        self.assertIsNone(taskbudget.exceeded(ws, config))
        with (ws / ".gpu-spend.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "job", "usd": 9.0, "hours_billed": 1.5}) + "\n")
        crossed = taskbudget.exceeded(ws, config)
        self.assertEqual(crossed["kind"], "usd")
        self.assertAlmostEqual(crossed["spent_usd"], 21.0)

    def test_gpu_hours_have_their_own_line(self):
        _, ws = self.workspace()
        config = {"harness": {"task_budget": {"max_usd": 500.0, "max_gpu_hours": 2}}}
        with (ws / ".gpu-spend.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "job", "usd": 2.0, "hours_billed": 2.5}) + "\n")
        self.assertEqual(taskbudget.exceeded(ws, config)["kind"], "gpu_hours")

    def test_crossing_the_cap_pauses_the_workspace_and_records_no_event(self):
        engine, ws = self.workspace()
        supervisor.run_directive(self.runtime, ws)
        head_before = engine.load()["integrity"]["event_head"]
        config = provision.read_config(ws)
        config["harness"]["task_budget"] = {"max_usd": 5.0, "max_gpu_hours": 0, "set_by": "test"}
        provision.write_config(ws, config)
        self.spend(ws, 50.0)
        outcome = supervisor.run_directive(self.runtime, ws)
        self.assertEqual(outcome["driver_status"], "paused")
        self.assertEqual(outcome["reason"], pause.TASK_BUDGET_STOP)
        self.assertEqual(outcome["sessions"], [])
        record = pause.read(ws / pause.PAUSE_FILE)
        self.assertEqual(record["reason"], pause.TASK_BUDGET_STOP)
        self.assertTrue(record["indefinite"])
        # A spend cap is a decision about money, so it never becomes a research fault.
        self.assertEqual(engine.load()["integrity"]["event_head"], head_before)
        self.assertEqual(engine.load().get("process_faults") or {}, {})

    def test_the_planner_sizes_the_cap_once_and_the_number_is_used(self):
        _, ws = self.workspace()
        seen = []

        def stub(prompt, directive):
            seen.append(prompt)
            return SessionResult(session_id="budget-1", ended="success", usage={},
                                 model_output={"max_usd": 45.0, "max_gpu_hours": 3.0, "expected_sessions": 9,
                                               "rationale": "a search with a checker at the end"})

        self.runtime.budget_session = stub
        supervisor.run_directive(self.runtime, ws)
        cap = taskbudget.read(provision.read_config(ws))
        self.assertEqual(cap["max_usd"], 45.0)
        self.assertEqual(cap["max_gpu_hours"], 3.0)
        self.assertEqual(cap["set_by"], "stub")
        self.assertIn("it works", seen[0].user_text, "the planner is shown the acceptance criteria")
        supervisor.run_directive(self.runtime, ws)
        self.assertEqual(len(seen), 1, "the cap is sized once, not before every directive")

    def test_a_planner_that_cannot_answer_leaves_a_default_cap_not_an_open_one(self):
        _, ws = self.workspace()
        # The minimal backend cannot satisfy the budget schema, so this is the real failure path.
        supervisor.run_directive(self.runtime, ws)
        cap = taskbudget.read(provision.read_config(ws))
        self.assertEqual(cap["max_usd"], taskbudget.DEFAULT_TASK_USD)
        self.assertEqual(cap["set_by"], "default")
        rows = [r for r in transcripts.load_index(ws) if r["mode"] == "budget"]
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
