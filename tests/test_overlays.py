"""Capability B: a 4.0 workspace amends its own contract under proof gates.

Repeating process faults schedule surgeon/overlay_diagnosis instead of a
human question; a fresh-context review must adopt any amendment; adopted
overlays only ever add or tighten. Fixtures stay field-free throughout.
"""

import tempfile
import unittest
from pathlib import Path

from src.adv_loop.driver import drive
from src.adv_loop.engine import LoopEngine
from src.adv_loop.errors import AdapterError, IntegrityError, TransitionError, ValidationError
from src.adv_loop.storage import canonical_json, object_hash

try:
    from test_engine import Harness, digest
except ImportError:  # invoked as tests.test_overlays rather than via discovery
    from tests.test_engine import Harness, digest


def hook_delta(hook_id="proof-check", rank="kernel_proof"):
    return {
        "schema_version": 1,
        "ops": [{
            "op": "register_validator_hook",
            "hook_id": hook_id,
            "rank": rank,
            "command": ["python3", "-c", "pass"],
            "timeout_seconds": 60,
        }],
    }


def instructions_delta():
    return {
        "schema_version": 1,
        "ops": [{
            "op": "add_role_instructions",
            "role": "researcher",
            "mode": "experiment",
            "instructions": ["Always cite the payload manifest entry the experiment consumed."],
        }],
    }


def make_v3_engine(case):
    harness = Harness(case)
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    harness.engine = LoopEngine.create(
        Path(temp.name), "Reconcile the two conflicting weekly reports",
        ["The discrepancy is explained"], {}, task_id="v3-task", policy_version="3.0",
    )
    return harness


def forge_event(engine, event_type, payload, request_id):
    """Append a correctly hashed but semantically forged event to the log."""

    with engine.store.lock():
        events = engine.store.read_unlocked()
        unsigned = {
            "seq": len(events) + 1,
            "at": "2026-02-01T00:00:00+00:00",
            "type": event_type,
            "request_id": request_id,
            "payload": payload,
            "prev_hash": events[-1]["hash"],
        }
        event = {**unsigned, "hash": object_hash(unsigned)}
        with open(engine.store.events_path, "a", encoding="utf-8") as handle:
            handle.write(canonical_json(event) + "\n")


class ProcessFaultTests(unittest.TestCase):
    def test_faults_accumulate_and_the_task_stays_active(self):
        harness = Harness(self)
        before = harness.engine.load()
        signature = harness.record_fault("net", times=2)
        state = harness.engine.load()
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["process_faults"][signature]["count"], 2)
        self.assertEqual(state["revision"], before["revision"] + 2)
        self.assertEqual(state["attempt_count"], 0)
        self.assertTrue(harness.engine.audit()["ok"])

    def test_three_repeats_of_one_signature_demand_a_surgeon(self):
        harness = Harness(self)
        harness.record_fault("alpha", times=2)
        harness.record_fault("beta", times=2)
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "initial_plan")
        signature = harness.record_fault("alpha", times=1)
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["role"], "surgeon")
        self.assertEqual(directive["mode"], "overlay_diagnosis")
        self.assertEqual(directive["demand"]["kind"], "fault")
        self.assertEqual(directive["demand"]["signature"], signature)

    def test_fault_demands_are_cyclic_per_threshold(self):
        harness = Harness(self)
        signature = harness.record_fault("loop", times=3)
        harness.surgeon("research_failure")
        state = harness.engine.load()
        self.assertIn(f"fault:{signature}#1", state["surgeon_marks"])
        self.assertEqual(harness.engine.next_instruction()["mode"], "initial_plan")
        harness.record_fault("loop", times=3)
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "overlay_diagnosis")
        self.assertTrue(directive["demand"]["mark"].endswith("#2"))

    def test_surgeon_preempts_pending_critique_but_never_finalize(self):
        harness = Harness(self)
        harness.plan()
        harness.research("first")
        harness.record_fault("storm", times=3)
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "overlay_diagnosis")
        harness.surgeon("research_failure")
        self.assertEqual(harness.engine.next_instruction()["mode"], "attempt_review")
        harness.critique("mixed")

        done = Harness(self)
        done.ready_for_completion()
        done.record_fault("late", times=3)
        self.assertEqual(done.engine.next_instruction()["action"], "finalize")
        state = done.engine.finalize()
        self.assertEqual(state["status"], "completed")

    def test_driver_records_a_fault_on_adapter_exhaustion(self):
        harness = Harness(self)

        def broken_adapter(envelope):
            raise RuntimeError("the tool is missing")

        with self.assertRaises(AdapterError):
            drive(harness.engine, broken_adapter, adapter_retries=1)
        state = harness.engine.load()
        self.assertEqual(len(state["process_faults"]), 1)
        fault = next(iter(state["process_faults"].values()))
        self.assertEqual(fault["count"], 1)
        self.assertEqual(fault["source"], "driver:adapter_exhausted")

        v3 = make_v3_engine(self)
        with self.assertRaises(AdapterError):
            drive(v3.engine, broken_adapter, adapter_retries=1)
        self.assertNotIn("process_faults", v3.engine.load())


class SurgeonAttemptTests(unittest.TestCase):
    def test_surgeon_is_proof_inert_and_bound_to_the_demand(self):
        harness = Harness(self)
        signature = harness.record_fault("bound", times=3)
        directive = harness.engine.next_instruction()
        base = {
            "request_id": "surgeon-bad",
            "directive_id": directive["directive_id"],
            "role": "surgeon",
            "mode": "overlay_diagnosis",
            "actor": {"agent_id": "a", "model": "m", "context_id": "surgeon-ctx"},
            "decisions": [],
        }
        diagnosis = {
            "raw_detail": "the adapter keeps dying",
            "classification": "research_failure",
            "kill_test": "a clean run",
            "next_experiment": "retry with the new rung",
            "fault_signature": signature,
        }
        with self.assertRaisesRegex(ValidationError, "proof-inert"):
            harness.engine.record_attempt({**base, "diagnosis": dict(diagnosis),
                                           "evidence": [harness.evidence("e", ["C1"])]})
        with self.assertRaisesRegex(ValidationError, "classification is unknown"):
            harness.engine.record_attempt({**base, "diagnosis": {**diagnosis, "classification": "bad"}})
        with self.assertRaisesRegex(ValidationError, "demanded fault signature"):
            harness.engine.record_attempt({**base, "diagnosis": {**diagnosis, "fault_signature": digest("other")}})
        with self.assertRaisesRegex(ValidationError, "harness_gap diagnosis proposes"):
            harness.engine.record_attempt({**base, "diagnosis": {
                **diagnosis, "proposed_delta": hook_delta(), "delta_fingerprint": digest("d"),
            }})
        state = harness.engine.record_attempt({**base, "diagnosis": diagnosis})
        self.assertEqual(state["attempts"][-1]["role"], "surgeon")
        self.assertIsNone(state["pending_overlay_review"])

    def test_forbidden_operations_are_rejected_by_name(self):
        harness = Harness(self)
        harness.record_fault("gapped", times=3)
        directive = harness.engine.next_instruction()
        forbidden = (
            "skip_verifier", "skip_critic", "waive_direct_evidence", "reuse_strategy_fingerprint",
            "reopen_terminal", "lower_criterion_rank", "declare_human_input_unnecessary",
            "cross_workspace_write", "patch_kernel_source", "delete_events",
        )
        for name in forbidden:
            payload = {
                "request_id": f"surgeon-{name}",
                "directive_id": directive["directive_id"],
                "role": "surgeon",
                "mode": "overlay_diagnosis",
                "actor": {"agent_id": "a", "model": "m", "context_id": "surgeon-ctx"},
                "diagnosis": {
                    "raw_detail": "gap", "classification": "harness_gap",
                    "kill_test": "k", "next_experiment": "n",
                    "fault_signature": directive["demand"]["signature"],
                    "proposed_delta": {"schema_version": 1, "ops": [{"op": name}]},
                    "delta_fingerprint": digest(name),
                },
                "decisions": [],
            }
            with self.assertRaisesRegex(ValidationError, "allowlist", msg=name):
                harness.engine.record_attempt(payload)
        state = harness.engine.load()
        self.assertEqual(state["attempt_count"], 0)
        self.assertEqual(state["overlays"], [])

    def test_tighten_only_directions_are_enforced(self):
        harness = Harness(self, criteria=[
            {"text": "Machine-checked claim", "min_formalization_rank": "model_check"},
            "Plain claim",
        ])
        state = harness.engine.load()
        cases = [
            ({"op": "raise_criterion_rank", "criterion_id": "C1", "min_formalization_rank": "model_check"},
             "only raise"),
            ({"op": "raise_criterion_rank", "criterion_id": "C1", "min_formalization_rank": "executable_spec"},
             "only raise"),
            ({"op": "require_min_rank", "criterion_id": "C1", "min_formalization_rank": "kernel_proof"},
             "no rank"),
            ({"op": "raise_criterion_rank", "criterion_id": "C2", "min_formalization_rank": "kernel_proof"},
             "only raise"),
            ({"op": "add_stall_class", "signature": digest("s"), "label": "slow", "threshold": 4},
             "sooner"),
            ({"op": "add_stall_class", "signature": digest("s"), "label": "slow", "threshold": 1},
             "sooner"),
        ]
        for op, message in cases:
            with self.assertRaisesRegex(ValidationError, message, msg=op["op"]):
                harness.engine._validate_overlay_delta({"schema_version": 1, "ops": [op]}, state)
        duplicates = {"schema_version": 1, "ops": [
            {"op": "pin_toolchain_hash", "toolchain_id": "chk", "artifact_hash": digest("a")},
            {"op": "pin_toolchain_hash", "toolchain_id": "chk", "artifact_hash": digest("b")},
        ]}
        with self.assertRaisesRegex(ValidationError, "already pinned"):
            harness.engine._validate_overlay_delta(duplicates, state)
        legal = {"schema_version": 1, "ops": [
            {"op": "require_min_rank", "criterion_id": "C2", "min_formalization_rank": "executable_spec"},
            {"op": "raise_criterion_rank", "criterion_id": "C1", "min_formalization_rank": "kernel_proof"},
            {"op": "add_stall_class", "signature": digest("s"), "label": "slow", "threshold": 2},
        ]}
        normalized = harness.engine._validate_overlay_delta(legal, state)
        self.assertEqual(len(normalized["ops"]), 3)


class OverlayReviewTests(unittest.TestCase):
    def gap_harness(self, delta=None):
        harness = Harness(self)
        harness.record_fault("gap", times=3)
        harness.surgeon("harness_gap", delta=delta or hook_delta())
        return harness

    def test_review_requires_a_fresh_context_and_clears_the_obligation(self):
        harness = self.gap_harness()
        state = harness.engine.load()
        surgeon_id = state["pending_overlay_review"]
        self.assertIsNotNone(surgeon_id)
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "overlay_review")
        surgeon_context = state["attempts"][-1]["actor"]["context_id"]
        payload = {
            "request_id": "review-same-context",
            "directive_id": directive["directive_id"],
            "role": "critic",
            "mode": "overlay_review",
            "actor": {"agent_id": "a", "model": "m", "context_id": surgeon_context},
            "overlay_review": {
                "target_attempt_id": surgeon_id,
                "verdict": "adopt",
                "reasons": ["r"],
                "uncertainty": "u",
            },
            "decisions": [],
        }
        with self.assertRaisesRegex(ValidationError, "fresh context"):
            harness.engine.record_attempt(payload)
        payload["actor"]["context_id"] = "genuinely-fresh"
        payload["overlay_review"]["verdict"] = "unsure"
        with self.assertRaisesRegex(ValidationError, "verdict"):
            harness.engine.record_attempt(payload)

    def test_adopt_auto_appends_a_monotonic_engine_authored_event(self):
        harness = self.gap_harness()
        stale_probe = harness.engine.next_instruction()
        state = harness.overlay_review("adopt")
        self.assertEqual(state["overlay_revision"], 1)
        self.assertEqual(len(state["overlays"]), 1)
        overlay = state["overlays"][0]
        self.assertEqual(overlay["overlay_id"], "OV0001")
        self.assertEqual(overlay["from_revision"], 0)
        self.assertEqual(overlay["to_revision"], 1)
        self.assertEqual(overlay["content_hash"], object_hash(overlay["delta"]))
        self.assertEqual(overlay["verdict"], "adopt")
        with harness.engine.store.lock():
            events = harness.engine.store.read_unlocked()
        adoption = events[-1]
        self.assertEqual(adoption["type"], "contract_overlay_adopted")
        self.assertTrue(adoption["request_id"].startswith("system:overlay:"))
        self.assertEqual(state["validator_hooks"][0]["hook_id"], "proof-check")
        self.assertIsNone(state["pending_overlay_adoption"])
        current = harness.engine.next_instruction()
        self.assertNotEqual(current.get("directive_id"), stale_probe.get("directive_id"))
        self.assertTrue(harness.engine.audit()["ok"])

    def test_reject_completes_the_obligation_without_adopting(self):
        harness = self.gap_harness()
        state = harness.overlay_review("reject")
        self.assertEqual(state["overlay_revision"], 0)
        self.assertEqual(state["overlays"], [])
        self.assertEqual(state["validator_hooks"], [])
        self.assertIsNone(state["pending_overlay_review"])
        self.assertEqual(harness.engine.next_instruction()["mode"], "initial_plan")

    def test_narrow_adopts_exactly_the_subset(self):
        wide = {
            "schema_version": 1,
            "ops": hook_delta()["ops"] + instructions_delta()["ops"],
        }
        harness = self.gap_harness(delta=wide)
        narrowed = {"schema_version": 1, "ops": [wide["ops"][0]]}
        state = harness.overlay_review("narrow", narrowed=narrowed)
        self.assertEqual(state["overlay_revision"], 1)
        self.assertEqual(len(state["overlays"][0]["delta"]["ops"]), 1)
        self.assertEqual(state["validator_hooks"][0]["hook_id"], "proof-check")
        self.assertEqual(state["role_instruction_overlays"], [])

        other = self.gap_harness()
        foreign = {"schema_version": 1, "ops": instructions_delta()["ops"]}
        with self.assertRaisesRegex(ValidationError, "subset"):
            other.overlay_review("narrow", narrowed=foreign)

    def test_adopted_instructions_and_registries_bind_the_future(self):
        wide = {
            "schema_version": 1,
            "ops": instructions_delta()["ops"] + [{
                "op": "register_evidence_kind",
                "kind": "checker-attested",
                "required_fields": ["checker"],
                "min_rank": "executable_spec",
            }],
        }
        harness = self.gap_harness(delta=wide)
        harness.overlay_review("adopt")
        harness.plan()
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["role"], "researcher")
        self.assertIn("Always cite the payload manifest entry the experiment consumed.",
                      directive["instructions"])
        bare = dict(harness.evidence("e1", ["C1"]))
        bare["kind"] = "checker-attested"
        with self.assertRaisesRegex(ValidationError, "required fields"):
            harness.research("registered-kind", extra_evidence=[bare])
        checked = harness.evidence("e2", ["C1"], rank="executable_spec", checker={
            "checker_id": "chk", "checker_version": "1.0", "accepted": True,
            "artifact_hash": digest("artifact-e2"),
        })
        checked["kind"] = "checker-attested"
        state = harness.research("registered-kind-ok", extra_evidence=[checked])
        self.assertEqual(state["evidence"]["E000001"]["kind"], "checker-attested")

    def test_overlay_add_criterion_is_additive_and_reopens_the_proof(self):
        harness = Harness(self)
        harness.plan()
        harness.research("success", satisfy=["C1"])
        harness.critique("validated_progress")
        harness.verify()
        self.assertTrue(harness.engine.load()["verification"]["passed"])
        harness.record_fault("grow", times=3)
        harness.surgeon("harness_gap", delta={
            "schema_version": 1,
            "ops": [{"op": "add_criterion", "text": "The follow-on claim also holds",
                     "min_formalization_rank": None, "rationale": "the gap showed a missing goal"}],
        })
        state = harness.overlay_review("adopt")
        self.assertEqual([c["id"] for c in state["criteria"]], ["C1", "C2"])
        self.assertFalse(state["verification"]["passed"])
        self.assertFalse(state["report"]["ready"])

    def test_one_workspace_overlay_never_touches_another(self):
        bystander = Harness(self)
        before = bystander.engine.load()
        harness = self.gap_harness()
        harness.overlay_review("adopt")
        after = bystander.engine.load()
        self.assertEqual(before["integrity"]["event_head"], after["integrity"]["event_head"])
        self.assertEqual(after["overlays"], [])
        self.assertTrue(bystander.engine.audit()["ok"])


class GapFlagTests(unittest.TestCase):
    def test_a_flagged_review_demands_one_diagnosis(self):
        harness = Harness(self)
        harness.plan()
        harness.research("odd", outcome="failed")
        harness.critique("no_progress", harness_gap=True)
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "overlay_diagnosis")
        self.assertEqual(directive["demand"]["kind"], "gap_flag")
        harness.surgeon("research_failure")
        self.assertNotEqual(harness.engine.next_instruction()["mode"], "overlay_diagnosis")

    def test_the_flag_is_structural_and_v4_only(self):
        harness = Harness(self)
        harness.plan()
        harness.research("odd", outcome="failed")
        directive = harness.engine.next_instruction()
        payload = harness.base("bad-flag", context="fresh-bad-flag")
        payload["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "no_progress",
            "reasons": ["r"],
            "uncertainty": "u",
            "harness_gap": "yes",
        }
        if "lens" in directive:
            payload["lens"] = directive["lens"]
        with self.assertRaisesRegex(ValidationError, "literal true"):
            harness.engine.record_attempt(payload)

        v3 = make_v3_engine(self)
        v3.plan()
        v3.research("odd", outcome="failed")
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            v3.critique("no_progress", harness_gap=True)


class RankBackendTests(unittest.TestCase):
    def test_a_rejected_backend_diagnosis_silences_the_demand(self):
        harness = Harness(self, criteria=[
            {"text": "Machine-checked claim", "min_formalization_rank": "smt_discharge"},
        ])
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "overlay_diagnosis")
        self.assertEqual(directive["demand"]["kind"], "rank_backend")
        harness.surgeon("harness_gap", delta=hook_delta(rank="smt_discharge"))
        state = harness.overlay_review("reject")
        self.assertEqual(state["validator_hooks"], [])
        self.assertIn("backend:C1:smt_discharge", state["surgeon_marks"])
        self.assertEqual(harness.engine.next_instruction()["mode"], "initial_plan")


class AskHumanGateTests(unittest.TestCase):
    def test_classification_is_required_and_harness_gap_names_the_surgeon(self):
        harness = Harness(self)
        with self.assertRaisesRegex(ValidationError, "classify"):
            harness.engine.request_human_input({"request_id": "ask-1", "question": "What now?"})
        with self.assertRaisesRegex(TransitionError, "overlay_diagnosis"):
            harness.engine.request_human_input({
                "request_id": "ask-2", "question": "The harness lacks a rung",
                "classification": "harness_gap",
            })
        state = harness.engine.request_human_input({
            "request_id": "ask-3", "question": "May I use the paid credential?",
            "classification": "authorization",
        })
        self.assertEqual(state["status"], "awaiting_human")
        self.assertEqual(state["human_input"]["classification"], "authorization")
        state = harness.engine.provide_human_input({"request_id": "answer-3", "answer": "Yes."})
        self.assertEqual(state["human_exchanges"][0]["classification"], "authorization")

    def test_v3_ask_human_is_untouched(self):
        v3 = make_v3_engine(self)
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            v3.engine.request_human_input({
                "request_id": "ask-1", "question": "Which source?",
                "classification": "authorization",
            })
        state = v3.engine.request_human_input({"request_id": "ask-2", "question": "Which source?"})
        self.assertEqual(state["status"], "awaiting_human")
        self.assertNotIn("classification", state["human_input"])

    def test_ask_human_is_refused_while_diagnosis_is_owed_but_unsafe_still_works(self):
        harness = Harness(self)
        harness.record_fault("owing", times=3)
        with self.assertRaisesRegex(TransitionError, "overlay_diagnosis"):
            harness.engine.request_human_input({
                "request_id": "ask-1", "question": "What should I do about the tool?",
                "classification": "external_dependency",
            })
        state = harness.engine.mark_unsafe({
            "request_id": "unsafe-1",
            "boundary": "the task began requiring credential use outside authorization",
            "risk": "credential misuse",
            "halted_action": "continuing the automated runs",
        })
        self.assertEqual(state["status"], "unsafe")


class BlockedGateTests(unittest.TestCase):
    def test_blocked_is_refused_while_diagnosis_is_owed(self):
        harness = Harness(self)
        harness.plan()
        for index in range(1, 8):
            harness.fail_cycle(f"stuck-{index}", dependency_evidence=index == 1)
        harness.escalation("audit")
        state = harness.engine.load()
        research_ids = [a["id"] for a in state["attempts"] if a["role"] == "researcher"][:3]
        direct = next(i["id"] for i in state["evidence"].values() if i["quality"] == "direct")
        harness.fail_cycle("post-audit")
        harness.fail_cycle("probe-rung")
        harness.fail_cycle("combine-rung")
        harness.drain_escalations("cycle-two")
        decision = {
            "request_id": "block-it",
            "dependency": "private external API",
            "attempt_ids": research_ids,
            "evidence_ids": [direct],
            "alternatives_exhausted": "every authorized route was tried and refused",
            "human_action": "grant read access",
            "retest": {
                "premise": "the supplier endpoint refuses every authorized credential",
                "recheck_after": "2026-01-01T00:00:00Z",
            },
        }
        harness.record_fault("blocker", times=3)
        with self.assertRaisesRegex(TransitionError, "harness diagnosis"):
            harness.engine.mark_blocked(decision)
        harness.surgeon("human_dependency")
        state = harness.engine.mark_blocked(decision)
        self.assertEqual(state["status"], "blocked")


class ReplayIntegrityTests(unittest.TestCase):
    def test_a_forged_adoption_fails_replay_closed(self):
        harness = Harness(self)
        forged = {
            "overlay_id": "OV0001",
            "content_hash": object_hash(hook_delta()),
            "from_revision": 0,
            "to_revision": 1,
            "schema_version": 1,
            "delta": hook_delta(),
            "justification_attempt_id": "A000001",
            "critic_attempt_id": "A000002",
            "verdict": "adopt",
            "fingerprint": digest("forged"),
        }
        forge_event(harness.engine, "contract_overlay_adopted", forged, "forged-adoption")
        with self.assertRaisesRegex(IntegrityError, "no adopted review"):
            harness.engine.load()

    def test_v4_events_are_rejected_on_a_v3_log(self):
        v3 = make_v3_engine(self)
        with self.assertRaisesRegex(TransitionError, "4.0"):
            v3.engine.record_process_fault({
                "request_id": "fault-1", "signature": digest("s"),
                "source": "test", "detail": "repeat",
            })
        forge_event(v3.engine, "process_fault_recorded", {
            "decision_hash": digest("d"), "signature": digest("s"),
            "source": "test", "detail": "repeat",
        }, "forged-fault")
        with self.assertRaisesRegex(IntegrityError, "4.0"):
            v3.engine.load()


if __name__ == "__main__":
    unittest.main()
