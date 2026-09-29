"""Phases D and E: discoveries compound, and the portfolio proposes what's next.

Re-dropping joins instead of forking; the commons makes results citable; the
frontier turns portfolio signals into new inbox drops through a validated
generator contract — closing the loop drop → run → learn → propose → drop.
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.adv_loop.commons import commons_report
from src.adv_loop.errors import AdapterError, LoopError
from src.adv_loop.frontier import frontier_report
from src.adv_loop.intake import intake_report
from src.adv_loop.runner import process_inbox

try:
    from test_engine import Harness
    from test_persistence import drive_to_blocked
except ImportError:  # invoked as tests.test_frontier rather than via discovery
    from tests.test_engine import Harness
    from tests.test_persistence import drive_to_blocked

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "frontier_generator", REPO_ROOT / "adapters" / "frontier_generator.py"
)
frontier_generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frontier_generator)


class IntakeJoinTests(unittest.TestCase):
    def test_reapplying_the_same_drop_joins_the_existing_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            drop = Path(tmp) / "drop"
            (drop / "alpha").mkdir(parents=True)
            (drop / "beta").mkdir()
            (drop / "alpha" / "a.py").write_text("a", encoding="utf-8")
            (drop / "beta" / "b.py").write_text("b", encoding="utf-8")
            root = Path(tmp) / "ws"
            first = intake_report(str(drop), root=root, apply=True)
            heads = {}
            for row in first["results"]:
                events = (Path(row["workspace"]) / "events.jsonl").read_bytes()
                heads[row["task_id"]] = events
            second = intake_report(str(drop), root=root, apply=True)
            self.assertEqual(second["exit_code"], 0)
            for row in second["results"]:
                self.assertTrue(row["ok"])
                self.assertTrue(row["existing"])
                self.assertEqual(
                    (Path(row["workspace"]) / "events.jsonl").read_bytes(),
                    heads[row["task_id"]],
                )
            for candidate in second["proposal"]["candidates"]:
                self.assertTrue(candidate["existing"])


class CommonsTests(unittest.TestCase):
    def test_commons_exports_manifests_reports_and_tolerates_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fleet"
            root.mkdir()
            completed = Harness(self)
            completed.ready_for_completion()
            completed.engine.finalize()
            shutil.copytree(completed.engine.workspace, root / "done-task")
            active = Harness(self)
            active.plan()
            shutil.copytree(active.engine.workspace, root / "open-task")
            broken = root / "broken"
            broken.mkdir()
            (broken / "events.jsonl").write_text("not json\n", encoding="utf-8")

            out = Path(tmp) / "commons"
            report = commons_report(root, out)
            self.assertEqual(report["exported"], 2)
            self.assertEqual(report["exit_code"], 2)
            manifest = json.loads((out / "test-task" / "manifest.json").read_text())
            self.assertIn(manifest["status"], {"completed", "active"})
            done_dirs = [d for d in out.iterdir() if d.is_dir()]
            reports = [d for d in done_dirs if (d / "report.md").is_file()]
            self.assertEqual(len(reports), 1)
            self.assertIn("# Report", (reports[0] / "report.md").read_text(encoding="utf-8"))
            index = json.loads((out / "index.json").read_text())
            self.assertEqual(len(index["entries"]), 2)
            again = commons_report(root, out)
            self.assertEqual(again["exported"], 2)


GENERATOR_STUB = ["python3", "-c", (
    "import json,sys\n"
    "env = json.load(sys.stdin)\n"
    "assert env['protocol'] == 'adv-loop-frontier/1'\n"
    "assert env['fleet'], 'signals missing'\n"
    "blocked = [s for s in env['fleet'] if s['status'] == 'blocked']\n"
    "assert blocked and blocked[0]['blocked_premise'], 'blocked premise not surfaced'\n"
    "print(json.dumps({'proposals': [\n"
    "  {'name': 'reopen-the-supplier-route',\n"
    "   'brief': '# Reopen the supplier route from a second vantage\\n\\n"
    "## Acceptance criteria\\n\\n- An alternative data path is demonstrated\\n',\n"
    "   'charter': {'steward': {'auto_answer': ['authorization']}}},\n"
    "  {'name': 'extend-the-verified-result',\n"
    "   'brief': '# Extend the verified result to the adjacent case\\n\\n"
    "## Acceptance criteria\\n\\n- The adjacent case is directly verified\\n',\n"
    "   'charter': None},\n"
    "]}))\n"
)]


class FrontierTests(unittest.TestCase):
    def portfolio_root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "fleet"
        root.mkdir()
        completed = Harness(self)
        completed.ready_for_completion()
        completed.engine.finalize()
        shutil.copytree(completed.engine.workspace, root / "done-task")
        blocked = drive_to_blocked(self)
        shutil.copytree(blocked.engine.workspace, root / "blocked-task")
        return Path(temp.name), root

    def test_signals_feed_the_generator_and_proposals_become_drops(self):
        base, root = self.portfolio_root()
        out = base / "incoming"
        from src.adv_loop.driver import SubprocessAdapter
        report = frontier_report(root, SubprocessAdapter(GENERATOR_STUB, 60),
                                 out=out, apply=True)
        self.assertEqual(report["signals"], 2)
        self.assertEqual(len(report["written"]), 2)
        drop = out / "reopen-the-supplier-route"
        self.assertIn("## Acceptance criteria", (drop / "brief.md").read_text(encoding="utf-8"))
        charter = json.loads((drop / "charter.json").read_text())
        self.assertEqual(charter["steward"]["auto_answer"], ["authorization"])

        # Full circle: the frontier's drops become chartered workspaces.
        results = process_inbox(out, root)
        self.assertTrue(all(item["ok"] for item in results))
        new_dirs = [d for d in root.iterdir() if d.is_dir()]
        self.assertGreaterEqual(len(new_dirs), 4)

    def test_dry_run_writes_nothing_and_the_contract_is_enforced(self):
        base, root = self.portfolio_root()
        out = base / "incoming"
        from src.adv_loop.driver import SubprocessAdapter
        report = frontier_report(root, SubprocessAdapter(GENERATOR_STUB, 60), out=out)
        self.assertEqual(report["mode"], "dry_run")
        self.assertFalse(out.exists())

        with self.assertRaises(LoopError):
            frontier_report(root, None)

        def too_many(envelope):
            return {"proposals": [{"name": f"p-{i}", "brief": "# x\n- y"} for i in range(9)]}
        with self.assertRaises(AdapterError):
            frontier_report(root, too_many, max_proposals=3)

        def bad_name(envelope):
            return {"proposals": [{"name": "Bad Name!", "brief": "# x"}]}
        with self.assertRaises(AdapterError):
            frontier_report(root, bad_name)

    def test_reference_generator_parses_model_output(self):
        proposals_json = json.dumps({"proposals": [
            {"name": "one-good-idea", "brief": "# One good idea\n\n## Acceptance criteria\n\n- It works\n",
             "charter": None},
        ]})

        def transport(request):
            self.assertEqual(request["thinking"], {"type": "adaptive"})
            self.assertIn("FLEET SIGNALS", request["messages"][0]["content"])
            return {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "```json\n" + proposals_json + "\n```"}],
                    "usage": {"input_tokens": 10, "output_tokens": 10}}

        value = frontier_generator.generate(
            {"protocol": "adv-loop-frontier/1", "fleet": [], "max_proposals": 3},
            transport, model="claude-opus-5",
        )
        self.assertEqual(value["proposals"][0]["name"], "one-good-idea")


if __name__ == "__main__":
    unittest.main()
