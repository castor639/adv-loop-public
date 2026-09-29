"""Only an attempt directive may be handed to an attempt-producing adapter."""

import unittest
from pathlib import Path
from unittest.mock import patch

from src.adv_loop.driver import drive
from src.adv_loop.errors import AdapterError

try:
    from test_engine import Harness
    import test_persistence as persistence
except ImportError:
    from tests.test_engine import Harness
    from tests import test_persistence as persistence


class DriverHandoffTests(unittest.TestCase):
    def fail_if_invoked(self, envelope):
        self.fail("An attempt adapter was invoked while no attempt was authorized")

    def test_old_policy_pause_is_resumable_only_after_recorded_answer(self):
        harness = Harness(self)
        harness.engine = persistence.V2FreezeEventTests.make_v2_workspace(Path(harness.temp.name))
        harness.engine.request_human_input({"request_id": "ask", "question": "Which source is authorized?"})
        result = drive(harness.engine, self.fail_if_invoked)
        self.assertEqual(result["driver_status"], "paused")
        self.assertEqual(result["state"]["policy_version"], "2.0")
        harness.engine.provide_human_input({"request_id": "answer", "answer": "Use the public ledger"})
        proposal = harness.base("authorized-plan")
        proposal["plan"] = {
            "assumptions": ["The public ledger contains the source records"],
            "subproblems": ["Read the ledger", "Check record coverage"],
            "candidate_experiments": ["Count and reconcile the records"],
            "falsification_tests": ["Find a missing source record"],
            "rejected_assumptions": [],
        }
        called = []

        def adapter(envelope):
            called.append(envelope["directive"]["action"])
            return proposal

        result = drive(harness.engine, adapter, max_cycles=1, adapter_retries=0)
        self.assertEqual(called, ["attempt"])
        self.assertEqual(result["accepted_attempts"], 1)
        self.assertEqual(harness.engine.load()["status"], "active")
        self.assertTrue(harness.engine.audit()["ok"])

    def test_pause_arriving_during_work_rejects_old_attempt_then_returns_question(self):
        harness = Harness(self)
        harness.plan()
        proposal = harness.base("in-flight")
        proposal.update({
            "plan_id": harness.engine.next_instruction()["active_plan_id"],
            "basin": "in-flight", "criterion_targets": ["C1"],
        })
        called = []

        def adapter(envelope):
            called.append(envelope["directive"]["action"])
            harness.engine.request_human_input({
                "request_id": "operator-pause", "question": "Which private source may be read?",
                "classification": "private_data",
            })
            return proposal

        result = drive(harness.engine, adapter)
        self.assertEqual(called, ["attempt"])
        self.assertEqual(result["driver_status"], "paused")
        self.assertEqual(result["state"]["status"], "awaiting_human")
        self.assertEqual(result["state"]["attempt_count"], 1)  # only the plan landed
        self.assertEqual(result["next"]["question"], "Which private source may be read?")
        self.assertTrue(harness.engine.audit()["ok"])

    def test_unknown_actions_fail_before_calling_the_adapter(self):
        harness = Harness(self)
        with patch.object(harness.engine, "next_instruction", return_value={"action": "unknown"}):
            with self.assertRaisesRegex(AdapterError, "unsupported action"):
                drive(harness.engine, self.fail_if_invoked)


if __name__ == "__main__":
    unittest.main()
