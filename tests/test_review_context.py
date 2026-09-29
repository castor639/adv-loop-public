"""Review the requested result even after the researcher claims satisfaction.

Archived-prefix tests exercise the actual acceptance drift. The small executed
artifact scenario proves the existing correction path remains usable by a fresh
critic with only the directive; it does not estimate model judgment quality.
"""

import copy
import hashlib
import unittest
from pathlib import Path

from src.adv_loop.driver import drive
from src.adv_loop.engine import LoopEngine
from src.adv_loop.errors import TransitionError, ValidationError
from src.adv_loop.policy import expected_step, with_directive_id

try:
    from test_engine import Harness
    import test_persistence as persistence
except ImportError:
    from tests.test_engine import Harness
    from tests import test_persistence as persistence


class ReviewContextTests(unittest.TestCase):
    def test_fresh_critic_can_correct_partial_work_without_hidden_history(self):
        harness = Harness(self, criteria=[
            "All ten source records are recovered",
            "A clean replay reproduces the complete ten-record recovery",
        ])
        harness.plan()
        artifact = harness.engine.workspace / "replay-result.txt"
        artifact.write_text("Recovered 7 of 10 records. Replay succeeded.\n", encoding="utf-8")
        fingerprint = hashlib.sha256(artifact.read_bytes()).hexdigest()
        research = harness.base("partial-replay")
        research.update({
            "plan_id": harness.engine.next_instruction()["active_plan_id"],
            "basin": "replay", "criterion_targets": ["C2"],
            "observation": "Replay succeeded for seven records; three remain missing.",
            "uncertainties": ["The complete-recovery qualification is not met."],
            "evidence": [{
                **harness.evidence("replay", ["C2"]),
                "locator": artifact.name, "fingerprint": fingerprint,
                "claim": "Seven records replay successfully",
            }],
            "criterion_updates": [{
                "id": "C2", "status": "satisfied", "evidence_refs": ["replay"],
                "reason": "The replay portion works",
            }],
        })
        harness.engine.record_attempt(research)
        review = harness.base("review")
        directive = harness.engine.next_instruction()
        review["lens"] = directive["lens"]
        review["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "invalid", "reasons": ["Complete recovery is unproved"],
            "uncertainty": "The three missing records remain unresolved",
        }
        reused_context = copy.deepcopy(review)
        reused_context["actor"]["context_id"] = research["actor"]["context_id"]
        with self.assertRaisesRegex(ValidationError, "fresh context"):
            harness.engine.record_attempt(reused_context)

        def critic(envelope):
            context = envelope["directive"]["review_context"]
            target = context["target_attempt"]
            required = next(c for c in context["criteria"] if c["id"] == "C2")
            self.assertIn("complete ten-record", required["text"])
            observed = (Path(envelope["workspace"]) / target["evidence"][0]["locator"]).read_bytes()
            self.assertIn(b"7 of 10", observed)
            self.assertEqual(hashlib.sha256(observed).hexdigest(), target["evidence"][0]["fingerprint"])
            correction = copy.deepcopy(review)
            correction["evidence"] = [{
                **harness.evidence("review-observation", ["C2"], independence_key="critic-file-read"),
                "locator": target["evidence"][0]["locator"],
                # Same observed artifact means same hash, even in a new context.
                "fingerprint": hashlib.sha256(observed).hexdigest(),
                "claim": "Replay output records only seven of the required ten records",
            }]
            correction["criterion_updates"] = [{
                "id": "C2", "status": "partial", "evidence_refs": ["review-observation"],
                "reason": "Observed partial recovery does not meet the complete-result criterion",
            }]
            return correction

        outcome = drive(harness.engine, critic, max_cycles=1, adapter_retries=0)
        self.assertEqual(outcome["accepted_attempts"], 1)
        self.assertEqual(outcome["state"]["criteria"][1]["status"], "partial")
        self.assertEqual(outcome["state"]["status"], "active")
        self.assertFalse(outcome["state"]["verification"]["passed"])
        self.assertEqual(outcome["state"]["failure_streak"], 1)
        review["request_id"] = "stale-review-with-new-request-id"
        with self.assertRaises(TransitionError):
            harness.engine.record_attempt(review)  # old review cannot write
        with self.assertRaises(TransitionError):
            harness.engine.finalize()
        self.assertTrue(harness.engine.audit()["ok"])

    def test_context_is_isolated_and_does_not_leak_into_ideation(self):
        harness = Harness(self)
        harness.plan()
        state = harness.research("candidate", satisfy=["C1"])
        original = copy.deepcopy(state)
        directive = with_directive_id(state, expected_step(state))
        context = directive["review_context"]
        context["target_attempt"]["evidence"][0]["fingerprint"] = "0" * 64
        context["criteria"][0]["text"] = "weakened text"
        self.assertEqual(state, original)
        self.assertNotIn("review_context", state)
        self.assertEqual(
            harness.engine.next_instruction(),
            with_directive_id(original, expected_step(original)),
        )
        harness.critique()
        # Use an open workspace to request voluntary minimal ideation.
        other = Harness(self)
        other.plan()
        minimal = other.engine.next_instruction(explore=True)
        self.assertEqual(minimal["context_scope"], "minimal")
        self.assertNotIn("review_context", minimal)
        self.assertNotIn("target_attempt", minimal)

    def test_frozen_policy_reviews_remain_unchanged(self):
        for version in ("2.0", "3.0", "4.0"):
            with self.subTest(version=version):
                harness = Harness(self)
                root = Path(harness.temp.name)
                if version == "2.0":
                    harness.engine = persistence.V2FreezeEventTests.make_v2_workspace(root)
                else:
                    harness.engine = LoopEngine.create(
                        root, "Recover the records", ["Complete recovery"], {},
                        task_id="older", policy_version=version,
                    )
                harness.plan()
                research = harness.base("candidate")
                research["plan_id"] = harness.engine.next_instruction()["active_plan_id"]
                research["criterion_targets"] = ["C1"]
                if version != "2.0":
                    research["basin"] = "candidate"
                harness.engine.record_attempt(research)
                directive = harness.engine.next_instruction()
                self.assertEqual(directive["mode"], "attempt_review")
                self.assertNotIn("review_context", directive)
                before = (harness.engine.workspace / "events.jsonl").read_bytes()
                harness.engine.load()
                self.assertTrue(harness.engine.audit()["ok"])
                self.assertEqual((harness.engine.workspace / "events.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
