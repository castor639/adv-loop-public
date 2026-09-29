"""Capability A: one fleet pass assesses a whole root and drives what owes work.

Adapters are python3 -c stubs; workspaces are copied Harness fixtures, so the
suite needs no network, no tooling, and no domain content.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.adv_loop.engine import LoopEngine
from src.adv_loop.runner import fleet_report, workspace_obligations
from src.adv_loop.cli import guard_report

try:
    import test_persistence
    from test_engine import Harness
    from test_persistence import drive_to_blocked
except ImportError:  # invoked as tests.test_runner rather than via discovery
    from tests import test_persistence
    from tests.test_engine import Harness
    from tests.test_persistence import drive_to_blocked

# Referenced through the module so unittest discovery does not collect the
# imported TestCase class a second time.
make_v2_workspace = test_persistence.V2FreezeEventTests.make_v2_workspace


PLANNER_STUB = (
    "import json,sys\n"
    "env = json.load(sys.stdin)\n"
    "d = env['directive']\n"
    "dims = ['decomposition','source_class','retrieval_method','reasoning_method','tool','verification_method']\n"
    "sub = {\n"
    "  'request_id': 'fleet-' + d['directive_id'][-12:],\n"
    "  'directive_id': d['directive_id'], 'role': d['role'], 'mode': d['mode'],\n"
    "  'actor': {'agent_id': 'stub', 'model': 'm', 'context_id': 'ctx-' + d['directive_id'][-8:]},\n"
    "  'strategy': {k: k + '-x' for k in dims},\n"
    "  'hypothesis': 'h', 'action': 'a', 'observation': 'o', 'interpretation': 'i',\n"
    "  'uncertainties': [], 'next_step': 'n', 'outcome': 'progress',\n"
    "  'evidence': [], 'criterion_updates': [], 'contradictions': [],\n"
    "  'contradiction_resolutions': [], 'decisions': [],\n"
    "  'plan': {'assumptions': ['x'], 'subproblems': ['y', 'z'], 'candidate_experiments': ['e'],\n"
    "           'falsification_tests': ['f'], 'rejected_assumptions': []},\n"
    "}\n"
    "print(json.dumps(sub))\n"
)
STUB_ADAPTER = ["python3", "-c", PLANNER_STUB]
FAILING_ADAPTER = ["python3", "-c", "import sys; sys.exit(3)"]


class FleetEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "fleet"
        self.root.mkdir()

    def test_enumeration_skips_non_workspaces_and_leaves_no_litter(self):
        LoopEngine.create(self.root, "A real task", ["Done"], {}, task_id="real")
        (self.root / "empty").mkdir()
        (self.root / "stray.txt").write_text("not a dir", encoding="utf-8")
        legacy = self.root / "old"
        legacy.mkdir()
        (legacy / "state.json").write_text("{}", encoding="utf-8")
        report = fleet_report(self.root, dry_run=True)
        self.assertEqual(report["scanned"], 3)
        self.assertEqual(len(report["workspaces"]), 2)
        self.assertFalse((self.root / "empty" / ".adv-loop.lock").exists())
        self.assertFalse((self.root / "empty" / "events.jsonl").exists())
        legacy_row = next(row for row in report["workspaces"] if row["workspace"].endswith("old"))
        self.assertEqual(legacy_row["error"]["error"], "legacy_workspace")
        self.assertEqual(report["exit_code"], 2)

    def test_skipped_rows_match_guard_obligations_exactly(self):
        completed = Harness(self)
        completed.ready_for_completion()
        completed.engine.finalize()
        shutil.copytree(completed.engine.workspace, self.root / "completed-task")
        waiting = Harness(self)
        waiting.engine.request_human_input({"request_id": "ask", "question": "Which source wins?",
                                            "classification": "other_human_judgment"})
        shutil.copytree(waiting.engine.workspace, self.root / "waiting-task")
        blocked = drive_to_blocked(self)
        shutil.copytree(blocked.engine.workspace, self.root / "blocked-task")

        report = fleet_report(self.root, dry_run=True)
        rows = {Path(row["workspace"]).name: row for row in report["workspaces"]}
        self.assertFalse(rows["completed-task"]["drivable"])
        self.assertEqual(rows["completed-task"]["obligations"], [])
        self.assertFalse(rows["waiting-task"]["drivable"])
        self.assertEqual(rows["waiting-task"]["obligations"], [])
        self.assertFalse(rows["blocked-task"]["drivable"])
        self.assertEqual(rows["blocked-task"]["obligations"][0]["kind"], "retest_due")
        guard = guard_report([self.root / "blocked-task"])
        self.assertEqual(rows["blocked-task"]["obligations"], guard["workspaces"][0]["obligations"])
        self.assertEqual(report["exit_code"], 1)

    def test_priority_orders_owed_escalations_before_recency(self):
        fresh = Harness(self)
        shutil.copytree(fresh.engine.workspace, self.root / "fresh-task")
        owed = Harness(self)
        owed.plan()
        owed.fail_cycle("one")
        owed.fail_cycle("two")
        self.assertIsNotNone(owed.engine.supervision()["owed_escalation"])
        shutil.copytree(owed.engine.workspace, self.root / "owed-task")

        report = fleet_report(self.root, dry_run=True, adapter=STUB_ADAPTER)
        self.assertEqual(
            [Path(item).name for item in report["drive_order"]],
            ["owed-task", "fresh-task"],
        )
        rows = {Path(row["workspace"]).name: row for row in report["workspaces"]}
        self.assertEqual(rows["owed-task"]["priority"], 0)
        self.assertIn("owed_escalation", rows["owed-task"]["priority_reasons"])
        self.assertEqual(rows["fresh-task"]["priority"], 1)


class FleetDriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "fleet"
        self.root.mkdir()

    def test_workspace_config_adapter_wins_and_missing_adapters_are_reported(self):
        LoopEngine.create(self.root, "Task with its own adapter", ["Done"], {}, task_id="configured")
        (self.root / "configured" / "loop-config.json").write_text(
            json.dumps({"adapter": STUB_ADAPTER}), encoding="utf-8"
        )
        LoopEngine.create(self.root, "Task without an adapter", ["Done"], {}, task_id="bare")
        LoopEngine.create(self.root, "Task with a broken declaration", ["Done"], {}, task_id="broken")
        (self.root / "broken" / "loop-config.json").write_text(
            json.dumps({"adapter": "not-an-argv"}), encoding="utf-8"
        )
        report = fleet_report(self.root)
        rows = {Path(row["workspace"]).name: row for row in report["workspaces"]}
        self.assertEqual(rows["configured"]["adapter_source"], "workspace-config")
        self.assertEqual(rows["configured"]["drive"]["accepted_attempts"], 1)
        self.assertFalse(rows["bare"]["driven"])
        self.assertIn("no adapter", rows["bare"]["note"])
        self.assertFalse(rows["broken"]["driven"])
        self.assertIn("invalid adapter config", rows["broken"]["note"])
        self.assertEqual(report["exit_code"], 1)

    def test_cli_fallback_drives_workspaces_without_their_own_adapter(self):
        LoopEngine.create(self.root, "First bare task", ["Done"], {}, task_id="one")
        LoopEngine.create(self.root, "Second bare task", ["Done"], {}, task_id="two")
        report = fleet_report(self.root, max_parallel=2, adapter=STUB_ADAPTER)
        rows = {Path(row["workspace"]).name: row for row in report["workspaces"]}
        for name in ("one", "two"):
            self.assertEqual(rows[name]["adapter_source"], "cli")
            self.assertEqual(rows[name]["drive"]["accepted_attempts"], 1)
            engine = LoopEngine(self.root / name)
            state = engine.load()
            self.assertEqual(state["attempt_count"], 1)
            self.assertTrue(engine.audit()["ok"])
        self.assertEqual(report["driven"], 2)
        # One accepted planning attempt each: both workspaces still owe work.
        self.assertEqual(report["exit_code"], 1)

    def test_adapter_failure_is_one_row_and_the_task_stays_active(self):
        LoopEngine.create(self.root, "Task whose adapter dies", ["Done"], {}, task_id="dying")
        report = fleet_report(self.root, adapter=FAILING_ADAPTER)
        row = report["workspaces"][0]
        self.assertTrue(row["driven"])
        self.assertEqual(row["adapter_error"]["error"], "adapter_error")
        state = LoopEngine(self.root / "dying").load()
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["attempt_count"], 0)
        self.assertEqual(report["exit_code"], 1)

    def test_a_corrupt_workspace_is_one_bad_row_not_a_poisoned_pass(self):
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "events.jsonl").write_text("not json\n", encoding="utf-8")
        LoopEngine.create(self.root, "Healthy task", ["Done"], {}, task_id="healthy")
        report = fleet_report(self.root, adapter=STUB_ADAPTER)
        rows = {Path(row["workspace"]).name: row for row in report["workspaces"]}
        self.assertFalse(rows["broken"]["ok"])
        self.assertEqual(rows["healthy"]["drive"]["accepted_attempts"], 1)
        self.assertEqual(report["exit_code"], 2)

    def test_exit_zero_only_when_nothing_owes_work(self):
        completed = Harness(self)
        completed.ready_for_completion()
        completed.engine.finalize()
        shutil.copytree(completed.engine.workspace, self.root / "done-task")
        report = fleet_report(self.root, dry_run=True)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["ok"])
        LoopEngine.create(self.root, "New work arrives", ["Done"], {}, task_id="fresh")
        report = fleet_report(self.root, dry_run=True)
        self.assertEqual(report["exit_code"], 1)

    def test_fleet_drives_a_v2_workspace_without_new_event_types(self):
        self.root.rmdir()
        self.root.mkdir()
        make_v2_workspace(self.root)
        report = fleet_report(self.root, adapter=STUB_ADAPTER)
        row = report["workspaces"][0]
        self.assertEqual(row["policy_version"], "2.0")
        self.assertEqual(row["drive"]["accepted_attempts"], 1)
        events = [
            json.loads(line)["type"]
            for line in (self.root / "v2-task" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(set(events), {"task_created", "attempt_recorded"})


if __name__ == "__main__":
    unittest.main()
