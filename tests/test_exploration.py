"""Pillar B, tier one: cheap generation, forced triage, conceptual-novelty memory.

Fixtures stay mundane and field-free on purpose: the engine must order the
same kinds of thinking for any task in any field.
"""

import unittest

from src.adv_loop.errors import TransitionError, ValidationError
from src.adv_loop.policy import (
    BASIN_STALL_CLOSURE,
    LADDER_CYCLE,
    LADDER_SPAN,
    SEED_MIN,
    TRIAGE_PROMOTION_CAP,
)

try:
    from test_engine import Harness, strategy
except ImportError:
    from tests.test_engine import Harness, strategy


def fail_to_streak_two(case, harness):
    harness.plan()
    harness.fail_cycle("seed-a")
    harness.fail_cycle("seed-b")
    directive = harness.engine.next_instruction()
    case.assertEqual(directive["role"], "explorer")
    case.assertEqual(directive["mode"], "ideation")
    return directive


class ForcedIdeationTests(unittest.TestCase):
    def test_streak_two_forces_ideation_and_the_directive_is_history_free(self):
        harness = Harness(self)
        directive = fail_to_streak_two(self, harness)
        self.assertEqual(directive["escalation"]["mark"], "ideation@C1#0")
        for leaky in ("recent_attempts", "active_plan", "failure_streak", "recent_failed_strategies"):
            self.assertNotIn(leaky, directive)
        self.assertEqual(directive["context_scope"], "minimal")
        self.assertEqual(directive["seed_count_range"][0], SEED_MIN)

    def test_ideation_is_proof_inert_and_requires_the_minimal_context_attestation(self):
        harness = Harness(self)
        directive = fail_to_streak_two(self, harness)
        base = {
            "request_id": "ideation-bad",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "a", "model": "m", "context_id": "fresh-x"},
            "context_scope": "minimal",
            "ideation_kind": directive["ideation_kind"],
            "seeds": [
                {
                    "claim": f"claim {i}",
                    "basin": f"basin {i}",
                    "first_unjustified_step": "step",
                    "kill_test": "test",
                }
                for i in range(3)
            ],
        }
        poisoned = dict(base)
        poisoned["evidence"] = [{"ref": "x"}]
        with self.assertRaisesRegex(ValidationError, "proof-inert"):
            harness.engine.record_attempt(poisoned)
        unattested = dict(base)
        unattested["context_scope"] = "full"
        with self.assertRaisesRegex(ValidationError, "minimal"):
            harness.engine.record_attempt(unattested)

    def test_seed_contract_is_enforced(self):
        harness = Harness(self)
        directive = fail_to_streak_two(self, harness)
        payload = {
            "request_id": "ideation-thin",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "a", "model": "m", "context_id": "fresh-y"},
            "context_scope": "minimal",
            "ideation_kind": directive["ideation_kind"],
            "seeds": [{"claim": "only one", "basin": "b", "first_unjustified_step": "s", "kill_test": "k"}],
        }
        with self.assertRaisesRegex(ValidationError, "between"):
            harness.engine.record_attempt(payload)
        payload["request_id"] = "ideation-missing-kill"
        payload["seeds"] = [
            {"claim": f"claim {i}", "basin": "b", "first_unjustified_step": "s"}
            for i in range(3)
        ]
        with self.assertRaisesRegex(ValidationError, "kill_test"):
            harness.engine.record_attempt(payload)

    def test_seed_claims_can_never_be_reproposed(self):
        harness = Harness(self)
        fail_to_streak_two(self, harness)
        harness.ideation("first")
        harness.triage("first")
        state = harness.engine.load()
        taken_claim = state["attempts"][-2]["seeds"][0]["claim"]
        harness.fail_cycle("seed-c")  # drives C1 to streak 3 via cs rung later; next rung is cs
        # Voluntary ideation from the free slot after draining the ladder:
        harness.drain_escalations("post-c")
        directive = harness.engine.next_instruction(explore=True)
        self.assertEqual(directive["mode"], "ideation")
        payload = {
            "request_id": "ideation-repeat",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "a", "model": "m", "context_id": "fresh-z"},
            "context_scope": "minimal",
            "ideation_kind": directive["ideation_kind"],
            "seeds": [
                {"claim": taken_claim, "basin": "b", "first_unjustified_step": "s", "kill_test": "k"},
                {"claim": "new claim 1", "basin": "b", "first_unjustified_step": "s", "kill_test": "k"},
                {"claim": "new claim 2", "basin": "b", "first_unjustified_step": "s", "kill_test": "k"},
            ],
        }
        with self.assertRaisesRegex(ValidationError, "duplicates a seed"):
            harness.engine.record_attempt(payload)

    def test_ideation_and_triage_are_streak_neutral(self):
        harness = Harness(self)
        fail_to_streak_two(self, harness)
        before = harness.engine.load()["failure_streak"]
        harness.ideation("neutral")
        harness.triage("neutral")
        after = harness.engine.load()
        self.assertEqual(after["failure_streak"], before)
        self.assertEqual(after["criterion_failure_streaks"]["C1"], before)

    def test_voluntary_ideation_is_legal_only_from_the_free_researcher_slot(self):
        harness = Harness(self)
        with self.assertRaisesRegex(TransitionError, "not currently legal"):
            harness.engine.next_instruction(explore=True)
        harness.plan()
        directive = harness.engine.next_instruction(explore=True)
        self.assertEqual(directive["mode"], "ideation")
        harness.ideation("voluntary", explore=True)
        state = harness.engine.load()
        self.assertEqual(state["pending_triage"], state["attempts"][-1]["id"])
        self.assertEqual(harness.engine.next_instruction()["mode"], "triage")


class TriageTests(unittest.TestCase):
    def test_triage_promotes_at_most_the_cap_and_creates_scored_candidates(self):
        harness = Harness(self)
        directive = fail_to_streak_two(self, harness)
        payload = {
            "request_id": "big-batch",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "a", "model": "m", "context_id": "big-batch-fresh"},
            "context_scope": "minimal",
            "ideation_kind": directive["ideation_kind"],
            "seeds": [
                {"claim": f"distinct claim {index}", "basin": f"family {index}",
                 "first_unjustified_step": "s", "kill_test": "k"}
                for index in range(TRIAGE_PROMOTION_CAP + 1)
            ],
        }
        harness.engine.record_attempt(payload)
        with self.assertRaisesRegex(ValidationError, "cap"):
            harness.triage("greedy", promote=tuple(range(TRIAGE_PROMOTION_CAP + 1)))
        state = harness.triage("generous", promote=tuple(range(TRIAGE_PROMOTION_CAP)))
        candidates = state["candidates"]
        self.assertEqual(len(candidates), TRIAGE_PROMOTION_CAP)
        self.assertEqual({item["status"] for item in candidates}, {"open"})
        self.assertTrue(all(item["score"] == 1000.0 for item in candidates))
        self.assertEqual(candidates[0]["id"], "Q0001")

    def test_triage_must_judge_every_seed_exactly_once_with_a_fresh_context(self):
        harness = Harness(self)
        fail_to_streak_two(self, harness)
        harness.ideation("once")
        directive = harness.engine.next_instruction()
        explorer = harness.engine.load()["attempts"][-1]
        payload = {
            "request_id": "triage-lazy",
            "directive_id": directive["directive_id"],
            "role": "critic",
            "mode": "triage",
            "actor": {"agent_id": "a", "model": "m", "context_id": "triage-fresh"},
            "triage": {
                "target_attempt_id": directive["target_attempt_id"],
                "verdicts": [{"seed_index": 0, "decision": "killed", "reason": "weak"}],
            },
        }
        with self.assertRaisesRegex(ValidationError, "every seed"):
            harness.engine.record_attempt(payload)
        payload["request_id"] = "triage-same-context"
        payload["triage"]["verdicts"] = [
            {"seed_index": index, "decision": "killed", "reason": "weak"} for index in range(3)
        ]
        payload["actor"]["context_id"] = explorer["actor"]["context_id"]
        with self.assertRaisesRegex(ValidationError, "fresh context"):
            harness.engine.record_attempt(payload)

    def test_promoted_seeds_must_be_debated_against_open_candidates(self):
        harness = Harness(self)
        fail_to_streak_two(self, harness)
        harness.ideation("first")
        harness.triage("first", promote=(0,))
        harness.fail_cycle("more-1")
        harness.drain_escalations("more-1")
        harness.serial += 90  # keep claims globally unique
        harness.ideation("second", explore=True)
        directive = harness.engine.next_instruction()
        payload = {
            "request_id": "triage-no-debate",
            "directive_id": directive["directive_id"],
            "role": "critic",
            "mode": "triage",
            "actor": {"agent_id": "a", "model": "m", "context_id": "triage-nodebate"},
            "triage": {
                "target_attempt_id": directive["target_attempt_id"],
                "verdicts": [
                    {"seed_index": 0, "decision": "promoted", "reason": "strong"},
                    {"seed_index": 1, "decision": "killed", "reason": "weak"},
                    {"seed_index": 2, "decision": "killed", "reason": "weak"},
                ],
            },
        }
        with self.assertRaisesRegex(ValidationError, "compared pairwise"):
            harness.engine.record_attempt(payload)


class BasinTests(unittest.TestCase):
    def test_two_confirmed_failures_close_a_basin_and_reentry_needs_a_shift(self):
        harness = Harness(self)
        harness.plan()
        harness.research("basin-1", outcome="failed")
        payload_basin = harness.engine.load()["attempts"][-1]["basin"]
        harness.critique("no_progress", "c1")
        # Second failed attempt in the same basin:
        payload = harness.base("basin-2", outcome="failed")
        directive = harness.engine.next_instruction()
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = payload_basin
        payload["criterion_targets"] = ["C1"]
        harness.engine.record_attempt(payload)
        harness.critique("no_progress", "c2")
        state = harness.engine.load()
        basin_key = next(iter(state["basins"]))
        self.assertEqual(state["basins"][basin_key]["status"], "closed")

        harness.drain_escalations("basin")
        directive = harness.engine.next_instruction()
        reentry = harness.base("basin-3", outcome="failed")
        reentry["plan_id"] = directive["active_plan_id"]
        reentry["basin"] = payload_basin
        reentry["criterion_targets"] = ["C1"]
        with self.assertRaisesRegex(ValidationError, "representation_shift"):
            harness.engine.record_attempt(reentry)
        reentry["representation_shift"] = "recast the reconciliation as a set-difference problem"
        state = harness.engine.record_attempt(reentry)
        self.assertEqual(state["attempts"][-1]["representation_shift"],
                         "recast the reconciliation as a set-difference problem")

    def test_validated_progress_reopens_the_basin(self):
        harness = Harness(self)
        harness.plan()
        shared = "the shared conceptual family"
        for index in (1, 2):
            payload = harness.base(f"rb-{index}", outcome="failed")
            directive = harness.engine.next_instruction()
            payload["plan_id"] = directive["active_plan_id"]
            payload["basin"] = shared
            payload["criterion_targets"] = ["C1"]
            harness.engine.record_attempt(payload)
            harness.critique("no_progress", f"rb-critic-{index}")
        harness.drain_escalations("rb")
        directive = harness.engine.next_instruction()
        winner = harness.base("rb-win", outcome="progress")
        winner["plan_id"] = directive["active_plan_id"]
        winner["basin"] = shared
        winner["representation_shift"] = "shifted to comparing the raw exports directly"
        winner["criterion_targets"] = ["C1"]
        ref = "rb-proof"
        winner["evidence"] = [harness.evidence(ref, ["C1"])]
        winner["criterion_updates"] = [{
            "id": "C1", "status": "satisfied", "evidence_refs": [ref], "reason": "directly shown",
        }]
        harness.engine.record_attempt(winner)
        state = harness.critique("validated_progress", "rb-final")
        basin_key = next(iter(state["basins"]))
        self.assertEqual(state["basins"][basin_key]["status"], "open")
        self.assertEqual(state["basins"][basin_key]["failures"], 0)

    def test_endorsed_progress_that_leaves_the_criterion_open_stalls_the_basin(self):
        """The near-miss case: a critic keeps endorsing work that never closes the criterion.

        Before 4.0 counted stalls, each endorsement wiped the basin's history, so a researcher could
        keep editing one baseline indefinitely and nothing in the loop ever asked for a different
        approach. Somebody outside the loop had to say it. Now the basin closes on its own.
        """

        harness = Harness(self)
        harness.plan()
        shared = "the one baseline"
        for index in (1, 2, 3):
            directive = harness.engine.next_instruction()
            payload = harness.base(f"stall-{index}", outcome="progress")
            payload["plan_id"] = directive["active_plan_id"]
            payload["basin"] = shared
            payload["criterion_targets"] = ["C1"]
            ref = f"stall-proof-{index}"
            payload["evidence"] = [harness.evidence(ref, ["C1"])]
            harness.engine.record_attempt(payload)
            state = harness.critique("validated_progress", f"stall-critic-{index}")
            basin_key = next(iter(state["basins"]))
            entry = state["basins"][basin_key]
            self.assertEqual(entry["stalls"], index)
            self.assertEqual(entry["failures"], 0, "endorsed work is not a failure")
            self.assertEqual(state["criteria"][0]["status"], "open", "the criterion never closed")
            self.assertEqual(entry["status"], "closed" if index >= BASIN_STALL_CLOSURE else "open")

        harness.drain_escalations("stall")
        directive = harness.engine.next_instruction()
        reentry = harness.base("stall-4", outcome="progress")
        reentry["plan_id"] = directive["active_plan_id"]
        reentry["basin"] = shared
        reentry["criterion_targets"] = ["C1"]
        with self.assertRaises(ValidationError):
            harness.engine.record_attempt(reentry)

    def test_a_mixed_verdict_stalls_the_basin_without_condemning_it(self):
        harness = Harness(self)
        harness.plan()
        shared = "partly useful family"
        directive = harness.engine.next_instruction()
        payload = harness.base("mixed-1", outcome="progress")
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = shared
        payload["criterion_targets"] = ["C1"]
        harness.engine.record_attempt(payload)
        state = harness.critique("mixed", "mixed-critic-1")
        entry = state["basins"][next(iter(state["basins"]))]
        self.assertEqual(entry["stalls"], 1)
        self.assertEqual(entry["failures"], 0)
        self.assertEqual(entry["status"], "open")

    def test_seeds_reentering_a_closed_basin_need_a_shift_too(self):
        harness = Harness(self)
        harness.plan()
        shared = "closed family"
        for index in (1, 2):
            payload = harness.base(f"sb-{index}", outcome="failed")
            directive = harness.engine.next_instruction()
            payload["plan_id"] = directive["active_plan_id"]
            payload["basin"] = shared
            payload["criterion_targets"] = ["C1"]
            harness.engine.record_attempt(payload)
            harness.critique("no_progress", f"sb-critic-{index}")
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "ideation")
        self.assertEqual(directive["basins"]["closed family"]["status"], "closed")
        payload = {
            "request_id": "sb-ideation",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "a", "model": "m", "context_id": "sb-fresh"},
            "context_scope": "minimal",
            "ideation_kind": directive["ideation_kind"],
            "seeds": [
                {"claim": "re-enter the closed family head-on", "basin": shared,
                 "first_unjustified_step": "s", "kill_test": "k"},
                {"claim": "another idea", "basin": "fresh family", "first_unjustified_step": "s", "kill_test": "k"},
                {"claim": "a third idea", "basin": "fresh family", "first_unjustified_step": "s", "kill_test": "k"},
            ],
        }
        with self.assertRaisesRegex(ValidationError, "representation_shift"):
            harness.engine.record_attempt(payload)
        payload["seeds"][0]["representation_shift"] = "treat the family's core object as data, not code"
        state = harness.engine.record_attempt(payload)
        self.assertEqual(state["attempts"][-1]["seeds"][0]["representation_shift"],
                         "treat the family's core object as data, not code")


class PerCriterionLadderTests(unittest.TestCase):
    def test_validated_side_quest_does_not_silence_the_stuck_criterion(self):
        harness = Harness(self, ["Stuck goal", "Easy goal"])
        harness.plan()
        for index in (1, 2):
            payload = harness.base(f"stuck-{index}", outcome="failed")
            directive = harness.engine.next_instruction()
            payload["plan_id"] = directive["active_plan_id"]
            payload["basin"] = f"stuck-basin-{index}"
            payload["criterion_targets"] = ["C1"]
            harness.engine.record_attempt(payload)
            harness.critique("no_progress", f"stuck-critic-{index}")
        state = harness.engine.load()
        self.assertEqual(state["criterion_failure_streaks"], {"C1": 2, "C2": 0})
        self.assertEqual(harness.engine.next_instruction()["mode"], "ideation")
        harness.ideation("stuck")
        harness.triage("stuck")

        # Side-quest: validated progress on C2.
        directive = harness.engine.next_instruction()
        side = harness.base("side-quest", outcome="progress")
        side["plan_id"] = directive["active_plan_id"]
        side["basin"] = "side-basin"
        side["criterion_targets"] = ["C2"]
        ref = "side-proof"
        side["evidence"] = [harness.evidence(ref, ["C2"])]
        side["criterion_updates"] = [{
            "id": "C2", "status": "satisfied", "evidence_refs": [ref], "reason": "directly shown",
        }]
        harness.engine.record_attempt(side)
        state = harness.critique("validated_progress", "side-critic")
        # Global streak resets, but C1's ladder is untouched:
        self.assertEqual(state["failure_streak"], 0)
        self.assertEqual(state["criterion_failure_streaks"]["C1"], 2)
        self.assertIn("ideation@C1#0", state["escalations_completed"])

        # One more confirmed failure on C1 and the ladder fires for C1, not C2.
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["role"], "researcher")
        again = harness.base("stuck-3", outcome="failed")
        again["plan_id"] = directive["active_plan_id"]
        again["basin"] = "stuck-basin-3"
        again["criterion_targets"] = ["C1"]
        harness.engine.record_attempt(again)
        harness.critique("no_progress", "stuck-critic-3")
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "contradiction_search")
        self.assertEqual(directive["escalation"]["criterion_id"], "C1")

    def test_the_cyclic_ladder_reaches_cycle_one_marks(self):
        harness = Harness(self)
        harness.plan()
        for index in range(1, 8):
            harness.fail_cycle(f"cy-{index}", dependency_evidence=index == 1)
        harness.escalation("cy-audit")
        harness.fail_cycle("cy-8")
        harness.fail_cycle("cy-9")
        harness.fail_cycle("cy-10")
        harness.drain_escalations("cy-cycle1")
        state = harness.engine.load()
        marks = set(state["escalations_completed"])
        for offset, _, mode, move in LADDER_CYCLE:
            self.assertIn(f"{move or mode}@C1#0", marks)
        self.assertIn("ideation@C1#1", marks)
        self.assertEqual(state["criterion_failure_streaks"]["C1"], 10)
        # The next demand already exists: the ladder never runs out.
        harness.fail_cycle("cy-11")
        self.assertEqual(harness.engine.next_instruction()["mode"], "contradiction_search")
        self.assertEqual(harness.engine.next_instruction()["escalation"]["mark"],
                         f"contradiction_search@C1#{1}")
        self.assertEqual(LADDER_SPAN, 8)


class CriterionAddedTests(unittest.TestCase):
    def test_add_criterion_is_additive_and_invalidates_verification(self):
        harness = Harness(self)
        harness.plan()
        harness.research("win", satisfy=["C1"])
        harness.critique()
        harness.verify()
        state = harness.engine.load()
        self.assertTrue(state["verification"]["passed"])
        state = harness.engine.add_criterion({
            "request_id": "add-1",
            "text": "The root cause is reproduced on a second, independent data pull",
            "rationale": "a better question surfaced during review",
        })
        self.assertEqual(state["criteria"][1]["id"], "C2")
        self.assertEqual(state["criteria"][1]["status"], "open")
        self.assertFalse(state["verification"]["passed"])
        self.assertEqual(state["criterion_failure_streaks"]["C2"], 0)
        self.assertEqual(harness.engine.next_instruction()["role"], "researcher")
        with self.assertRaisesRegex(ValidationError, "already exists"):
            harness.engine.add_criterion({
                "request_id": "add-2",
                "text": "The root cause is reproduced on a second, independent data pull",
                "rationale": "duplicate",
            })
        self.assertTrue(harness.engine.audit()["ok"])

    def test_planner_can_propose_additive_criteria_in_decompose(self):
        harness = Harness(self)
        harness.plan()
        for index in range(1, 5):
            harness.fail_cycle(f"pc-{index}")
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "decompose")
        payload = harness.base("pc-decompose")
        payload["plan"] = {
            "assumptions": ["a"],
            "subproblems": ["part one", "part two"],
            "candidate_experiments": ["e"],
            "falsification_tests": ["f"],
            "rejected_assumptions": [],
        }
        payload["proposed_criteria"] = [{
            "text": "Each half of the decomposition is independently demonstrated",
            "rationale": "the split is only real if both halves carry proof",
        }]
        state = harness.engine.record_attempt(payload)
        self.assertEqual(state["criteria"][1]["text"],
                         "Each half of the decomposition is independently demonstrated")
        self.assertEqual(state["criteria"][1]["id"], "C2")

    def test_v2_workspaces_refuse_criterion_added(self):
        import tempfile
        from pathlib import Path
        try:
            from test_persistence import V2FreezeEventTests
        except ImportError:
            from tests.test_persistence import V2FreezeEventTests
        with tempfile.TemporaryDirectory() as tmp:
            engine = V2FreezeEventTests.make_v2_workspace(Path(tmp))
            with self.assertRaisesRegex(TransitionError, "policy version 3.0"):
                engine.add_criterion({
                    "request_id": "v2-add",
                    "text": "extra goal",
                    "rationale": "not allowed on 2.0",
                })


if __name__ == "__main__":
    unittest.main()
