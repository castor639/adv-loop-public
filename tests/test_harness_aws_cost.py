"""Cost Explorer polling by tag, the AWS guard's GPU line, and the GPU spend ledgers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import budget
from harness.aws import aws_cost

REPO_ROOT = Path(__file__).resolve().parents[1]
UNGROUPED = {"ResultsByTime": [
    {"TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"}, "Total": {"UnblendedCost": {"Amount": "12.5", "Unit": "USD"}}, "Groups": []},
    {"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-13"}, "Total": {"UnblendedCost": {"Amount": "0.75", "Unit": "USD"}}, "Groups": []},
]}
GROUPED = {"ResultsByTime": [
    {"Groups": [
        {"Keys": ["adv-loop$harness"], "Metrics": {"UnblendedCost": {"Amount": "10.25", "Unit": "USD"}}},
        {"Keys": ["adv-loop$gpu"], "Metrics": {"UnblendedCost": {"Amount": "3.5", "Unit": "USD"}}},
        {"Keys": ["adv-loop$"], "Metrics": {"UnblendedCost": {"Amount": "1.0", "Unit": "USD"}}},
    ], "Total": {}},
    {"Groups": [
        {"Keys": ["adv-loop$gpu"], "Metrics": {"UnblendedCost": {"Amount": "0.5", "Unit": "USD"}}},
        {"Keys": ["adv-loop$other"], "Metrics": {"UnblendedCost": {"Amount": "0.25", "Unit": "USD"}}},
    ], "Total": {}},
]}


class FakeRunner:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, stdout=json.dumps(self.payload), stderr="")


class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "aws-spend.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_grouped_query_adds_the_tag_group_and_writes_by_tag(self):
        runner = FakeRunner(GROUPED)
        state = aws_cost.refresh(self.state, "2026-09-01", group_by_tag="adv-loop", runner=runner, until="2026-10-13")
        argv = runner.calls[0]
        self.assertEqual(argv[:3], ["aws", "ce", "get-cost-and-usage"])
        self.assertEqual(argv[argv.index("--time-period") + 1], "Start=2026-09-01,End=2026-10-13")
        self.assertEqual(argv[argv.index("--group-by") + 1], "Type=TAG,Key=adv-loop")
        self.assertEqual(json.loads(argv[argv.index("--filter") + 1]),
                         {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}})
        self.assertEqual(state["by_tag"], {"harness": 10.25, "gpu": 4.0, "untagged": 1.0, "other": 0.25})
        self.assertEqual(state["aws_spend_usd"], 15.5)
        self.assertFalse(state["stopped"] or state["gpu_disabled"])
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["by_tag"]["gpu"], 4.0)

    def test_ungrouped_query_keeps_the_total_and_the_old_entry_point(self):
        runner = FakeRunner(UNGROUPED)
        self.assertEqual(aws_cost.cumulative_usd("2026-09-01", "2026-10-13", runner=runner), 13.25)
        self.assertNotIn("--group-by", runner.calls[0])
        state = aws_cost.refresh(self.state, "2026-09-01", runner=runner)
        self.assertEqual(state["aws_spend_usd"], 13.25)
        self.assertNotIn("by_tag", state)

    def test_cli_with_a_fake_aws_on_path(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "aws"
        fake.write_text(f"#!{sys.executable}\nimport json, sys\nassert '--group-by' in sys.argv, sys.argv\n"
                        f"print(json.dumps({json.dumps(GROUPED)}))\n", encoding="utf-8")
        fake.chmod(0o755)
        done = subprocess.run([sys.executable, str(REPO_ROOT / "harness" / "aws" / "aws_cost.py"), "--since", "2026-09-01",
                               "--state", str(self.state), "--group-by-tag", "adv-loop"],
                              env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        printed = json.loads(done.stdout)
        self.assertEqual(printed["by_tag"]["harness"], 10.25)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["aws_spend_usd"], 15.5)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.guard = budget.AwsSpendGuard(Path(self.tmp.name) / "aws-spend.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_stores_by_tag_and_allows_gpu_counts_projected_and_unreported(self):
        state = self.guard.update(1500.0, by_tag={"gpu": 10.004, "harness": 90})
        self.assertEqual(state["by_tag"], {"gpu": 10.0, "harness": 90.0})
        self.assertEqual(self.guard.state()["by_tag"]["harness"], 90.0)
        self.assertTrue(self.guard.allows_gpu())
        self.assertTrue(self.guard.allows_gpu(projected_usd=99.9))
        self.assertFalse(self.guard.allows_gpu(projected_usd=100.0), "the line itself is refused")
        self.assertFalse(self.guard.allows_gpu(projected_usd=20.0, unreported_usd=90.0))
        self.assertTrue(self.guard.allows_gpu(projected_usd=20.0, unreported_usd=70.0))
        self.guard.update(1850.0)
        self.assertFalse(self.guard.allows_gpu())
        self.assertFalse(self.guard.allows_sessions())
        self.assertNotIn("by_tag", self.guard.state())
        self.guard.update(1650.0)
        self.assertFalse(self.guard.allows_gpu(), "the original gate still holds")
        self.assertTrue(self.guard.allows_sessions())

    def test_stale(self):
        self.assertTrue(self.guard.stale(3600), "no state at all")
        self.guard.update(1.0)
        self.assertFalse(self.guard.stale(3600))
        self.assertTrue(self.guard.stale(-1))
        old = dict(self.guard.state(), at="2026-01-01T00:00:00Z")
        self.guard.state_file.write_text(json.dumps(old), encoding="utf-8")
        self.assertTrue(self.guard.stale(12 * 3600))
        self.guard.state_file.write_text(json.dumps(dict(old, at="not a time")), encoding="utf-8")
        self.assertTrue(self.guard.stale(12 * 3600))
        del old["at"]
        self.guard.state_file.write_text(json.dumps(old), encoding="utf-8")
        self.assertTrue(self.guard.stale(12 * 3600))


class LedgerTests(unittest.TestCase):
    def test_record_and_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            path = state / budget.STATE_SPEND_FILE
            row = budget.record_gpu_spend(path, {"kind": "box_run", "usd": 1.5, "seconds": 5368})
            self.assertTrue(row["at"].endswith("Z"))
            budget.record_gpu_spend(path, {"kind": "box_run", "usd": 0.25, "at": "2026-09-12T10:00:00Z"})
            budget.record_gpu_spend(path, {"kind": "job", "usd": 99.0})
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("not json\n")
            self.assertEqual(budget.gpu_spend_total(state), 1.75)
            self.assertEqual(budget.gpu_spend_total(state, running_since="2026-09-12T10:00:00Z", now=budget.parse_utc("2026-09-12T11:00:00Z")),
                             round(1.75 + 1.006, 6))
            self.assertEqual(budget.gpu_spend_total(state, running_since="garbage"), 1.75)
            self.assertEqual(budget.gpu_spend_total(Path(tmp) / "nowhere"), 0.0)
            self.assertEqual(budget.GPU_SPEND_FILE, ".gpu-spend.jsonl")
            self.assertEqual(budget.GPU_RATE_USD_PER_HOUR, 1.006)

    def test_parse_utc_forms(self):
        self.assertEqual(budget.parse_utc("2026-09-12T10:00:00Z"), budget.parse_utc("2026-09-12T10:00:00+00:00"))
        self.assertEqual(budget.parse_utc("2026-09-12T10:00:00.250Z"), budget.parse_utc("2026-09-12T10:00:00Z") + 0.25)
        self.assertIsNone(budget.parse_utc(None))
        self.assertIsNone(budget.parse_utc("yesterday"))


if __name__ == "__main__":
    unittest.main()
