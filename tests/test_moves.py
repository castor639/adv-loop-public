"""Pillar B, tier two: moves, registries, the candidate tournament, lenses.

Field-free fixtures throughout: assets are named things with locators, barriers
are named walls, wishes are dated hopes — no domain knowledge required.
"""

import unittest

from src.adv_loop.errors import TransitionError, ValidationError
from src.adv_loop.policy import REVIEW_LENSES

try:
    from test_engine import Harness
except ImportError:
    from tests.test_engine import Harness


def free_researcher_payload(case, harness, seed, **overrides):
    payload = harness.base(seed, outcome=overrides.pop("outcome", "progress"))
    directive = harness.engine.next_instruction()
    case.assertEqual(directive["role"], "researcher")
    payload["plan_id"] = directive["active_plan_id"]
    payload["basin"] = overrides.pop("basin", f"basin-{seed}")
    payload["criterion_targets"] = overrides.pop("criterion_targets", ["C1"])
    payload.update(overrides)
    return payload


class MoveContractTests(unittest.TestCase):
    def test_move_defaults_to_test_and_unknown_moves_are_rejected(self):
        harness = Harness(self)
        harness.plan()
        payload = free_researcher_payload(self, harness, "default-move")
        state = harness.engine.record_attempt(payload)
        self.assertEqual(state["attempts"][-1]["move"], "test")
        harness.critique("no_progress", "dm-critic")
        bad = free_researcher_payload(self, harness, "bad-move", move="meander")
        with self.assertRaisesRegex(ValidationError, "Unknown research move"):
            harness.engine.record_attempt(bad)

    def test_survey_must_register_an_asset_and_assets_get_stable_ids(self):
        harness = Harness(self)
        harness.plan()
        empty = free_researcher_payload(self, harness, "survey-empty", move="survey")
        with self.assertRaisesRegex(ValidationError, "register at least one"):
            harness.engine.record_attempt(empty)
        good = free_researcher_payload(
            self, harness, "survey-good", move="survey",
            assets_registered=[{
                "name": "supplier-feed-export",
                "kind": "source",
                "locator": "https://example.test/feed.csv",
                "note": "the raw feed both reports claim to summarize",
            }],
        )
        state = harness.engine.record_attempt(good)
        self.assertEqual(state["assets"][0]["id"], "AS0001")
        self.assertEqual(state["assets"][0]["producer_attempt_id"], state["attempts"][-1]["id"])

    def test_combine_demands_a_never_before_joined_asset_pair(self):
        harness = Harness(self)
        harness.plan()
        for index, name in enumerate(["ledger-export", "dashboard-query", "email-digest"]):
            payload = free_researcher_payload(
                self, harness, f"survey-{index}", move="survey",
                assets_registered=[{"name": name, "kind": "source", "locator": f"artifact://{name}"}],
            )
            harness.engine.record_attempt(payload)
            harness.critique("mixed", f"survey-critic-{index}")
            harness.drain_escalations(f"survey-drain-{index}")
        first = free_researcher_payload(
            self, harness, "combine-1", move="combine",
            combination={"asset_ids": ["AS0001", "AS0002"]},
        )
        state = harness.engine.record_attempt(first)
        self.assertEqual(state["combined_asset_pairs"], [["AS0001", "AS0002"]])
        harness.critique("no_progress", "combine-critic-1")
        harness.drain_escalations("combine-drain")
        repeat = free_researcher_payload(
            self, harness, "combine-2", move="combine",
            combination={"asset_ids": ["AS0002", "AS0001"]},
        )
        with self.assertRaisesRegex(ValidationError, "already combined"):
            harness.engine.record_attempt(repeat)
        fresh = free_researcher_payload(
            self, harness, "combine-3", move="combine",
            combination={"asset_ids": ["AS0001", "AS0003"]},
        )
        state = harness.engine.record_attempt(fresh)
        self.assertIn(["AS0001", "AS0003"], state["combined_asset_pairs"])
        harness.critique("mixed", "combine-critic-3")
        harness.drain_escalations("combine-drain-3")
        without_object = free_researcher_payload(self, harness, "combine-4", combination={"asset_ids": ["AS0002", "AS0003"]})
        with self.assertRaisesRegex(ValidationError, "requires move=combine"):
            harness.engine.record_attempt(without_object)

    def test_barrier_probe_registers_and_targets_named_walls(self):
        harness = Harness(self)
        harness.plan()
        declare = free_researcher_payload(
            self, harness, "probe-1", move="barrier_probe",
            barrier_probe={
                "new_barrier": {
                    "name": "reporting-lag",
                    "statement": "the dashboard aggregates a day later than the ledger",
                },
                "pattern": "bound",
                "approach": "measure the maximum possible lag from timestamps",
            },
        )
        state = harness.engine.record_attempt(declare)
        barrier = state["barriers"][0]
        self.assertEqual(barrier["id"], "BR0001")
        self.assertEqual(barrier["probes"][0]["pattern"], "bound")
        harness.critique("mixed", "probe-critic-1")
        harness.drain_escalations("probe-drain-1")
        again = free_researcher_payload(
            self, harness, "probe-2", move="barrier_probe",
            barrier_probe={"barrier_id": "BR0001", "pattern": "dual", "approach": "attack the lag from the ledger side"},
        )
        state = harness.engine.record_attempt(again)
        self.assertEqual(len(state["barriers"][0]["probes"]), 2)
        harness.critique("mixed", "probe-critic-2")
        harness.drain_escalations("probe-drain-2")
        unknown = free_researcher_payload(
            self, harness, "probe-3", move="barrier_probe",
            barrier_probe={"barrier_id": "BR9999", "pattern": "dual", "approach": "x"},
        )
        with self.assertRaisesRegex(ValidationError, "Unknown barrier"):
            harness.engine.record_attempt(unknown)
        bad_pattern = free_researcher_payload(
            self, harness, "probe-4", move="barrier_probe",
            barrier_probe={"barrier_id": "BR0001", "pattern": "wish-harder", "approach": "x"},
        )
        with self.assertRaisesRegex(ValidationError, "pattern"):
            harness.engine.record_attempt(bad_pattern)

    def test_replicate_targets_only_validated_attempts_and_may_reuse_their_strategy(self):
        harness = Harness(self)
        harness.plan()
        harness.research(
            "original",
            outcome="progress",
            extra_evidence=[{
                "ref": "orig-observation",
                "kind": "test",
                "quality": "direct",
                "claim": "the first reconciliation pass narrowed the discrepancy",
                "locator": "artifact://orig-observation",
                "method": "direct comparison",
                "fingerprint": "0" * 63 + "1",
                "independence_key": "orig-observation-key",
                "supports": ["C1"],
            }],
        )
        harness.critique("validated_progress", "orig-critic")
        state = harness.engine.load()
        validated_id = state["latest_research_attempt_id"]
        original_strategy = state["attempts"][-2]["strategy"]
        replica = free_researcher_payload(
            self, harness, "replica", move="replicate", replication_of=validated_id,
        )
        replica["strategy"] = dict(original_strategy)
        state = harness.engine.record_attempt(replica)
        self.assertEqual(state["attempts"][-1]["replication_of"], validated_id)
        harness.critique("mixed", "replica-critic")
        not_validated = free_researcher_payload(
            self, harness, "replica-bad", move="replicate",
            replication_of=state["attempts"][-1]["id"],
        )
        with self.assertRaisesRegex(ValidationError, "validated-progress"):
            harness.engine.record_attempt(not_validated)


class WishTests(unittest.TestCase):
    def test_wishes_are_declared_rechecked_and_fulfilled(self):
        harness = Harness(self)
        harness.plan()
        payload = free_researcher_payload(
            self, harness, "wisher",
            wishes_declared=[{
                "statement": "the archived raw exports for week 12 become retrievable",
                "would_open": "a direct row-level diff of the two reports",
                "test": "request the archive index and look for week-12 files",
                "recheck_after": "2026-01-01T00:00:00Z",
            }],
        )
        state = harness.engine.record_attempt(payload)
        wish = state["wishes"][0]
        self.assertEqual(wish["id"], "W0001")
        self.assertEqual(wish["status"], "open")
        supervision = harness.engine.supervision()
        self.assertEqual(supervision["wishes_due"][0]["id"], "W0001")

        state = harness.engine.fulfill_wish({
            "request_id": "wish-done",
            "wish_id": "W0001",
            "note": "the archive team restored the week-12 exports",
        })
        self.assertEqual(state["wishes"][0]["status"], "fulfilled")
        self.assertEqual(harness.engine.supervision()["wishes_due"], [])
        with self.assertRaises(TransitionError):
            harness.engine.fulfill_wish({
                "request_id": "wish-again", "wish_id": "W0001", "note": "twice",
            })
        with self.assertRaisesRegex(ValidationError, "Unknown wish"):
            harness.engine.fulfill_wish({
                "request_id": "wish-missing", "wish_id": "W9999", "note": "no such wish",
            })


class LensTests(unittest.TestCase):
    def test_lenses_rotate_deterministically_and_must_be_echoed(self):
        harness = Harness(self, controls=["a known-clean copy of the report pipeline"])
        harness.plan()
        seen = []
        for index in range(4):
            harness.research(f"lens-{index}", outcome="failed")
            directive = harness.engine.next_instruction()
            seen.append(directive["lens"])
            if index == 0:
                wrong = harness.base("wrong-lens", context=f"fresh-wl-{index}")
                wrong["lens"] = "novelty"
                wrong["assessment"] = {
                    "target_attempt_id": directive["target_attempt_id"],
                    "verdict": "no_progress",
                    "reasons": ["r"],
                    "uncertainty": "u",
                }
                with self.assertRaisesRegex(ValidationError, "assigned lens"):
                    harness.engine.record_attempt(wrong)
            harness.critique("mixed", f"lens-critic-{index}")
            harness.drain_escalations(f"lens-drain-{index}")
        self.assertEqual(seen, list(REVIEW_LENSES))

    def test_proves_too_much_lens_requires_the_control_and_kills_over_certifiers(self):
        harness = Harness(self, controls=["a shuffled copy of the input that contains no real signal"])
        harness.plan()
        harness.research("ptm-0", outcome="failed")
        harness.critique("mixed", "ptm-critic-0")
        harness.research("ptm-1", outcome="failed")
        harness.critique("mixed", "ptm-critic-1")
        harness.drain_escalations("ptm")
        harness.research("ptm-2", outcome="progress", satisfy=["C1"])
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["lens"], "proves_too_much")
        self.assertEqual(directive["controls"][0]["id"], "CTL1")

        missing = harness.base("ptm-missing", context="fresh-ptm-a")
        missing["lens"] = "proves_too_much"
        missing["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "validated_progress",
            "reasons": ["looks right"],
            "uncertainty": "u",
        }
        with self.assertRaisesRegex(ValidationError, "negative control"):
            harness.engine.record_attempt(missing)

        over_certifier = harness.base("ptm-over", context="fresh-ptm-b")
        over_certifier["lens"] = "proves_too_much"
        over_certifier["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "validated_progress",
            "reasons": ["it certifies the target"],
            "uncertainty": "u",
            "control_check": {
                "control_id": "CTL1",
                "outcome": "endorses_control",
                "note": "the same mechanism also certifies the shuffled input",
            },
        }
        with self.assertRaisesRegex(ValidationError, "proves too much"):
            harness.engine.record_attempt(over_certifier)

        honest = harness.base("ptm-honest", context="fresh-ptm-c")
        honest["lens"] = "proves_too_much"
        honest["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "invalid",
            "reasons": ["the mechanism certifies noise as readily as signal"],
            "uncertainty": "the control may be unrepresentative",
            "control_check": {
                "control_id": "CTL1",
                "outcome": "endorses_control",
                "note": "certified the shuffled input",
            },
        }
        state = harness.engine.record_attempt(honest)
        self.assertEqual(state["attempts"][-2]["review"]["verdict"], "invalid")
        self.assertEqual(state["attempts"][-2]["review"]["lens"], "proves_too_much")


class MultiCauseTests(unittest.TestCase):
    def test_streak_three_reviews_must_attribute_at_least_two_causes(self):
        harness = Harness(self)
        harness.plan()
        for index in (1, 2, 3):
            harness.fail_cycle(f"mc-{index}")
        harness.drain_escalations("mc")
        harness.research("mc-4", outcome="failed")
        directive = harness.engine.next_instruction()
        self.assertTrue(directive["multi_cause_required"])
        lazy = harness.base("mc-lazy", context="fresh-mc-a")
        lazy["lens"] = directive["lens"]
        lazy["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "no_progress",
            "reasons": ["failed again"],
            "uncertainty": "u",
        }
        with self.assertRaisesRegex(ValidationError, "two"):
            harness.engine.record_attempt(lazy)
        state = harness.critique("no_progress", "mc-thorough")
        lessons = state["lessons"]
        self.assertEqual(len(lessons), 2)
        self.assertEqual(lessons[0]["id"], "L0001")
        self.assertEqual(lessons[0]["criterion_ids"], ["C1"])

    def test_fresh_replan_must_address_every_lesson(self):
        harness = Harness(self)
        harness.plan()
        for index in (1, 2, 3, 4):
            harness.fail_cycle(f"fr-{index}")
        harness.drain_escalations("fr")  # contradiction_search + decompose as owed
        harness.fail_cycle("fr-5")
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "fresh_replan")
        self.assertTrue(directive["lessons"])
        payload = harness.base("fr-plan")
        payload["plan"] = {
            "assumptions": ["a"],
            "subproblems": ["s"],
            "candidate_experiments": ["e"],
            "falsification_tests": ["f"],
            "rejected_assumptions": ["the old framing"],
        }
        with self.assertRaisesRegex(ValidationError, "lesson"):
            harness.engine.record_attempt(payload)
        payload["request_id"] = "fr-plan-2"
        payload["plan"]["lessons_addressed"] = [
            {"lesson_id": lesson["id"], "response": f"the new plan tests {lesson['id']} first"}
            for lesson in directive["lessons"]
        ]
        state = harness.engine.record_attempt(payload)
        self.assertIn("lessons_addressed", state["attempts"][-1]["plan"])


class TournamentTests(unittest.TestCase):
    def test_elo_updates_are_deterministic_from_recorded_debates(self):
        harness = Harness(self)
        harness.plan()
        harness.fail_cycle("elo-1")
        harness.fail_cycle("elo-2")
        harness.ideation("elo-first")
        harness.triage("elo-first", promote=(0,))
        state = harness.engine.load()
        self.assertEqual(state["candidates"][0]["score"], 1000.0)

        harness.fail_cycle("elo-3")
        harness.drain_escalations("elo-3")
        harness.serial += 50
        harness.ideation("elo-second", explore=True)
        state = harness.triage("elo-second", promote=(0,))
        # One debate at equal scores, seed wins: 1000 + 32*0.5 / 1000 - 32*0.5.
        by_id = {item["id"]: item for item in state["candidates"]}
        self.assertEqual(by_id["Q0001"]["score"], 984.0)
        self.assertEqual(by_id["Q0002"]["score"], 1016.0)

    def test_candidate_lifecycle_open_consumed_dead_or_validated(self):
        harness = Harness(self)
        harness.plan()
        harness.fail_cycle("cl-1")
        harness.fail_cycle("cl-2")
        harness.ideation("cl")
        harness.triage("cl", promote=(0, 1))
        state = harness.engine.load()
        first, second = state["candidates"][0]["id"], state["candidates"][1]["id"]

        consume = free_researcher_payload(self, harness, "cl-consume", outcome="failed", candidate_id=first)
        state = harness.engine.record_attempt(consume)
        self.assertEqual(state["candidates"][0]["status"], "consumed")
        state = harness.critique("no_progress", "cl-critic")
        self.assertEqual(state["candidates"][0]["status"], "dead")

        harness.drain_escalations("cl")
        dead_reuse = free_researcher_payload(self, harness, "cl-reuse", candidate_id=first)
        with self.assertRaisesRegex(ValidationError, "never resurrected"):
            harness.engine.record_attempt(dead_reuse)

        winner = free_researcher_payload(
            self, harness, "cl-win", outcome="progress", candidate_id=second,
        )
        ref = "cl-proof"
        winner["evidence"] = [harness.evidence(ref, ["C1"])]
        winner["criterion_updates"] = [{
            "id": "C1", "status": "satisfied", "evidence_refs": [ref], "reason": "shown directly",
        }]
        harness.engine.record_attempt(winner)
        state = harness.critique("validated_progress", "cl-final")
        by_id = {item["id"]: item for item in state["candidates"]}
        self.assertEqual(by_id[second]["status"], "validated")


class EvolveTests(unittest.TestCase):
    def test_second_ideation_evolves_the_top_candidates_and_parents_are_enforced(self):
        harness = Harness(self)
        harness.plan()
        harness.fail_cycle("ev-1")
        harness.fail_cycle("ev-2")
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["ideation_kind"], "broad")
        parented = {
            "request_id": "ev-broad-parents",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "a", "model": "m", "context_id": "ev-fresh-a"},
            "context_scope": "minimal",
            "ideation_kind": "broad",
            "seeds": [
                {"claim": "c1", "basin": "b", "first_unjustified_step": "s", "kill_test": "k",
                 "parents": ["Q0001"]},
                {"claim": "c2", "basin": "b", "first_unjustified_step": "s", "kill_test": "k"},
                {"claim": "c3", "basin": "b", "first_unjustified_step": "s", "kill_test": "k"},
            ],
        }
        with self.assertRaisesRegex(ValidationError, "evolve-kind"):
            harness.engine.record_attempt(parented)
        harness.ideation("ev-broad")
        harness.triage("ev-broad", promote=(0, 1))

        directive = harness.engine.next_instruction(explore=True)
        self.assertEqual(directive["ideation_kind"], "evolve")
        top_ids = {item["id"] for item in directive["top_candidates"]}
        self.assertEqual(top_ids, {"Q0001", "Q0002"})
        orphan = {
            "request_id": "ev-evolve-orphan",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "a", "model": "m", "context_id": "ev-fresh-b"},
            "context_scope": "minimal",
            "ideation_kind": "evolve",
            "seeds": [
                {"claim": "mutated idea", "basin": "b", "first_unjustified_step": "s", "kill_test": "k"},
                {"claim": "mutated idea 2", "basin": "b", "first_unjustified_step": "s", "kill_test": "k",
                 "parents": ["Q0001"]},
                {"claim": "recombined idea", "basin": "b", "first_unjustified_step": "s", "kill_test": "k",
                 "parents": ["Q0001", "Q0002"]},
            ],
        }
        with self.assertRaisesRegex(ValidationError, "parents"):
            harness.engine.record_attempt(orphan)
        orphan["seeds"][0]["parents"] = ["Q0002"]
        orphan["request_id"] = "ev-evolve-good"
        state = harness.engine.record_attempt(orphan)
        self.assertEqual(state["attempts"][-1]["ideation_kind"], "evolve")
        self.assertEqual(state["attempts"][-1]["seeds"][2]["parents"], ["Q0001", "Q0002"])


if __name__ == "__main__":
    unittest.main()
