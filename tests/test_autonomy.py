"""Phase B: zero-touch operation inside the charter's envelope.

The steward answers pre-authorized questions, probes file retests and wishes,
blocked tasks revive themselves, spend ceilings stop the fleet, the watch loop
stops honestly, and the inbox turns dropped folders into running workspaces.
Every automated decision is an ordinary audited chain event.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.adv_loop.engine import LoopEngine
from src.adv_loop.runner import fleet_report, fleet_watch, process_inbox

try:
    from test_engine import Harness
    from test_persistence import drive_to_blocked
except ImportError:  # invoked as tests.test_autonomy rather than via discovery
    from tests.test_engine import Harness
    from tests.test_persistence import drive_to_blocked


ANSWER_STEWARD = ["python3", "-c", (
    "import json,sys\n"
    "env = json.load(sys.stdin)\n"
    "assert env['protocol'] == 'adv-loop-steward/1'\n"
    "assert env['charter'] is not None\n"
    "print(json.dumps({'answer': 'custom steward answer citing the charter'}))"
)]
ESCALATE_STEWARD = ["python3", "-c",
                    "import json,sys; json.load(sys.stdin); print(json.dumps({'escalate': True}))"]
DEAD_PROBE = ["python3", "-c",
              "import json,sys; json.load(sys.stdin);"
              " print(json.dumps({'outcome': 'premise_no_longer_holds', 'note': 'the endpoint answers now'}))"]
HOLDING_PROBE = ["python3", "-c",
                 "import json,sys; json.load(sys.stdin);"
                 " print(json.dumps({'outcome': 'premise_holds', 'note': 'still refused',"
                 " 'recheck_after': '2999-01-01T00:00:00Z'}))"]
WISH_PROBE = ["python3", "-c",
              "import json,sys; env=json.load(sys.stdin);"
              " print(json.dumps({'fulfilled': True, 'note': 'the release shipped'}))"]


def write_config(workspace: Path, config):
    (workspace / "loop-config.json").write_text(json.dumps(config), encoding="utf-8")


def write_charter(workspace: Path):
    payload = workspace / "payload"
    payload.mkdir(exist_ok=True)
    (payload / "charter.md").write_text(
        "# Charter\nGrants: the granted credential may be used freely.\n", encoding="utf-8"
    )


class StewardTests(unittest.TestCase):
    def fleet_root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "fleet"
        root.mkdir()
        return root

    def waiting_workspace(self, root, name, classification="authorization"):
        harness = Harness(self)
        harness.engine.request_human_input({
            "request_id": "ask-1",
            "question": "May I use the granted credential for the export?",
            "classification": classification,
        })
        destination = root / name
        shutil.copytree(harness.engine.workspace, destination)
        return destination

    def test_builtin_steward_answers_covered_classifications(self):
        root = self.fleet_root()
        workspace = self.waiting_workspace(root, "covered")
        write_config(workspace, {"steward": {"auto_answer": ["authorization"]}})
        write_charter(workspace)
        report = fleet_report(root, dry_run=True)
        row = report["workspaces"][0]
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["autonomy"][0]["kind"], "steward_answered")
        state = LoopEngine(workspace).load()
        self.assertIn("[steward]", state["human_exchanges"][0]["answer"])
        self.assertTrue(LoopEngine(workspace).audit()["ok"])

    def test_steward_command_hook_answers_or_escalates(self):
        root = self.fleet_root()
        answered = self.waiting_workspace(root, "answered")
        write_config(answered, {"steward": {"auto_answer": ["authorization"],
                                            "command": ANSWER_STEWARD}})
        write_charter(answered)
        escalated = self.waiting_workspace(root, "escalated")
        write_config(escalated, {"steward": {"auto_answer": ["authorization"],
                                             "command": ESCALATE_STEWARD}})
        write_charter(escalated)
        report = fleet_report(root, dry_run=True)
        rows = {Path(row["workspace"]).name: row for row in report["workspaces"]}
        self.assertEqual(rows["answered"]["status"], "active")
        state = LoopEngine(answered).load()
        self.assertEqual(state["human_exchanges"][0]["answer"],
                         "custom steward answer citing the charter")
        self.assertEqual(rows["escalated"]["status"], "awaiting_human")
        self.assertEqual(rows["escalated"]["autonomy"][0]["kind"], "steward_escalated")

    def test_safety_boundary_is_never_auto_answered(self):
        root = self.fleet_root()
        workspace = self.waiting_workspace(root, "boundary", classification="safety_boundary")
        write_config(workspace, {"steward": {"auto_answer": ["safety_boundary", "authorization"]}})
        write_charter(workspace)
        report = fleet_report(root, dry_run=True)
        row = report["workspaces"][0]
        self.assertEqual(row["status"], "awaiting_human")
        self.assertNotIn("autonomy", row)

    def test_uncovered_classifications_stay_with_the_operator(self):
        root = self.fleet_root()
        workspace = self.waiting_workspace(root, "uncovered", classification="private_data")
        write_config(workspace, {"steward": {"auto_answer": ["authorization"]}})
        report = fleet_report(root, dry_run=True)
        self.assertEqual(report["workspaces"][0]["status"], "awaiting_human")


class ProbeTests(unittest.TestCase):
    def fleet_root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "fleet"
        root.mkdir()
        return root

    def test_retest_probe_files_the_outcome_and_revives_a_dead_premise(self):
        root = self.fleet_root()
        blocked = drive_to_blocked(self)
        destination = root / "blocked-task"
        shutil.copytree(blocked.engine.workspace, destination)
        write_config(destination, {"retest_probe": DEAD_PROBE})
        report = fleet_report(root, dry_run=True)
        row = report["workspaces"][0]
        kinds = [action["kind"] for action in row["autonomy"]]
        self.assertEqual(kinds, ["retest_filed", "auto_unblocked"])
        state = LoopEngine(destination).load()
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["retests"][-1]["outcome"], "premise_no_longer_holds")
        self.assertTrue(LoopEngine(destination).audit()["ok"])

    def test_holding_probe_keeps_the_block_and_reschedules(self):
        root = self.fleet_root()
        blocked = drive_to_blocked(self)
        destination = root / "blocked-task"
        shutil.copytree(blocked.engine.workspace, destination)
        write_config(destination, {"retest_probe": HOLDING_PROBE})
        report = fleet_report(root, dry_run=True)
        row = report["workspaces"][0]
        self.assertEqual(row["autonomy"][0], {"kind": "retest_filed", "outcome": "premise_holds"})
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["obligations"], [])
        self.assertEqual(report["exit_code"], 0)

    def test_wish_probe_fulfills_due_wishes(self):
        root = self.fleet_root()
        harness = Harness(self)
        harness.plan()
        payload = harness.base("wisher", outcome="progress")
        directive = harness.engine.next_instruction()
        payload.update({"plan_id": directive["active_plan_id"], "criterion_targets": ["C1"],
                        "basin": "wish-basin"})
        payload["wishes_declared"] = [{
            "statement": "the upstream release ships",
            "would_open": "the direct route",
            "test": "check the release page",
            "recheck_after": "2020-01-01T00:00:00Z",
        }]
        harness.engine.record_attempt(payload)
        destination = root / "wishing-task"
        shutil.copytree(harness.engine.workspace, destination)
        write_config(destination, {"wish_probe": WISH_PROBE})
        report = fleet_report(root, dry_run=True)
        row = report["workspaces"][0]
        self.assertIn({"kind": "wish_fulfilled", "wish_id": "W0001"}, row["autonomy"])
        state = LoopEngine(destination).load()
        self.assertEqual(state["wishes"][0]["status"], "fulfilled")


class SpendCeilingTests(unittest.TestCase):
    def test_workspace_and_global_ceilings_stop_driving(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fleet"
            root.mkdir()
            LoopEngine.create(root, "Costly task", ["Done"], {}, task_id="costly")
            (root / "costly" / ".spend.jsonl").write_text(
                json.dumps({"estimated_usd": 7.5}) + "\n" + json.dumps({"estimated_usd": 2.5}) + "\n",
                encoding="utf-8",
            )
            write_config(root / "costly", {"spend_ceiling_usd": 5})
            report = fleet_report(root)
            row = report["workspaces"][0]
            self.assertFalse(row["driven"])
            self.assertIn("workspace spend ceiling", row["note"])
            self.assertEqual(row["spend_usd"], 10.0)

            LoopEngine.create(root, "Second task", ["Done"], {}, task_id="second")
            report = fleet_report(root, spend_ceiling_usd=8.0,
                                  adapter=["python3", "-c", "pass"])
            self.assertTrue(report["spend_stopped"])
            second = next(r for r in report["workspaces"] if r["workspace"].endswith("second"))
            self.assertIn("global spend ceiling", second["note"])
            self.assertEqual(report["driven"], 0)


class WatchTests(unittest.TestCase):
    def test_watch_stops_on_kill_file_quiescence_and_max_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fleet"
            root.mkdir()
            (root / ".fleet-stop").write_text("", encoding="utf-8")
            result = fleet_watch(root, interval=0, dry_run=True)
            self.assertEqual(result["stop_reason"], "kill_file")
            self.assertEqual(result["passes"], 0)
            (root / ".fleet-stop").unlink()

            completed = Harness(self)
            completed.ready_for_completion()
            completed.engine.finalize()
            shutil.copytree(completed.engine.workspace, root / "done-task")
            result = fleet_watch(root, interval=0, dry_run=True)
            self.assertEqual(result["stop_reason"], "quiescent")
            self.assertEqual(result["passes"], 1)
            self.assertEqual(result["exit_code"], 0)

            LoopEngine.create(root, "Open task", ["Done"], {}, task_id="open-task")
            result = fleet_watch(root, interval=0, max_passes=3, dry_run=True)
            self.assertEqual(result["stop_reason"], "max_passes")
            self.assertEqual(result["passes"], 3)
            self.assertEqual(result["exit_code"], 1)


class InboxTests(unittest.TestCase):
    def test_inbox_drops_become_workspaces_with_their_charter_attached(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "fleet"
            root.mkdir()
            inbox = base / "incoming"
            drop = inbox / "mixed-drop"
            (drop / "alpha").mkdir(parents=True)
            (drop / "beta").mkdir()
            (drop / "alpha" / "a.py").write_text("a", encoding="utf-8")
            (drop / "beta" / "b.py").write_text("b", encoding="utf-8")
            (drop / "charter.md").write_text("# Charter\nGrants: read-only web.", encoding="utf-8")
            (drop / "charter.json").write_text(
                json.dumps({"steward": {"auto_answer": ["authorization"]},
                            "spend_ceiling_usd": 25}), encoding="utf-8")
            (inbox / "broken.zip").write_bytes(b"not really a zip")

            results = process_inbox(inbox, root)
            by_drop = {item["drop"]: item for item in results}
            self.assertTrue(by_drop["mixed-drop"]["ok"])
            self.assertEqual(len(by_drop["mixed-drop"]["workspaces"]), 2)
            self.assertFalse(by_drop["broken.zip"]["ok"])
            self.assertTrue((inbox / "processed" / "mixed-drop").is_dir())
            self.assertTrue((inbox / "failed" / "broken.zip").is_file())
            self.assertTrue((inbox / "failed" / "broken.zip.error.json").is_file())

            for workspace in by_drop["mixed-drop"]["workspaces"]:
                config = json.loads((Path(workspace) / "loop-config.json").read_text())
                self.assertEqual(config["steward"], {"auto_answer": ["authorization"]})
                self.assertEqual(config["spend_ceiling_usd"], 25)
                charter = Path(workspace) / "payload" / "charter.md"
                self.assertIn("read-only web", charter.read_text(encoding="utf-8"))

            again = process_inbox(inbox, root)
            self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main()
