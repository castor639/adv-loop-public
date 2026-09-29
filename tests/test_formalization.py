"""Capability C: a rank-gated criterion cannot be satisfied, verified, or
completed by prose — only by checker-attested evidence at or above its rank.

Checker records are attested structures, so fixtures fabricate them with
plain hashes; no proof assistant or solver is ever invoked here.
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.adv_loop.cli import build_parser, dispatch
from src.adv_loop.engine import LoopEngine
from src.adv_loop.errors import IntegrityError, TransitionError, ValidationError
from src.adv_loop.policy import (
    CHECKED_RANK_FLOOR,
    FORMALIZATION_RANKS,
    criteria_evidence_ready,
    rank_at_least,
)
from src.adv_loop.storage import canonical_json, object_hash

try:
    from test_engine import Harness, digest
except ImportError:  # invoked as tests.test_formalization rather than via discovery
    from tests.test_engine import Harness, digest


def accepting_checker(label, accepted=True):
    return {
        "checker_id": f"chk-{label}",
        "checker_version": "1.0",
        "accepted": accepted,
        "artifact_hash": digest(f"artifact-{label}"),
        "log_hash": digest(f"log-{label}"),
    }


def make_raw_workspace(root: Path, policy_version: str, criteria):
    """Hand-write a task_created event, bypassing create()'s validation."""

    workspace = root / "raw-task"
    workspace.mkdir()
    payload = {
        "protocol_version": 2,
        "policy_version": policy_version,
        "task_id": "raw-task",
        "task": "Reconcile the two conflicting weekly reports",
        "criteria": criteria,
        "budget": {},
        "controls": [],
    }
    unsigned = {
        "seq": 1,
        "at": "2026-01-01T00:00:00+00:00",
        "type": "task_created",
        "request_id": "create:raw-task",
        "payload": payload,
        "prev_hash": "0" * 64,
    }
    event = {**unsigned, "hash": object_hash(unsigned)}
    (workspace / "events.jsonl").write_text(canonical_json(event) + "\n", encoding="utf-8")
    return LoopEngine(workspace)


class RankLadderTests(unittest.TestCase):
    def test_ladder_order_and_comparators(self):
        self.assertEqual(FORMALIZATION_RANKS, (
            "sourced_claim", "replicated_experiment", "executable_spec",
            "smt_discharge", "model_check", "kernel_proof",
        ))
        self.assertTrue(rank_at_least("kernel_proof", "sourced_claim"))
        self.assertTrue(rank_at_least("smt_discharge", CHECKED_RANK_FLOOR))
        self.assertFalse(rank_at_least("executable_spec", CHECKED_RANK_FLOOR))


class CriterionRankTests(unittest.TestCase):
    def test_min_rank_is_settable_at_create_on_v4_only(self):
        harness = Harness(self, criteria=[
            {"text": "The result is machine-checked", "min_formalization_rank": "kernel_proof"},
            "A plain criterion",
        ])
        state = harness.engine.load()
        self.assertEqual(state["criteria"][0]["min_formalization_rank"], "kernel_proof")
        self.assertIsNone(state["criteria"][1]["min_formalization_rank"])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TransitionError):
                LoopEngine.create(Path(tmp), "A task",
                                  [{"text": "x", "min_formalization_rank": "kernel_proof"}],
                                  {}, task_id="t3", policy_version="3.0")
            with self.assertRaises(ValidationError):
                LoopEngine.create(Path(tmp), "A task",
                                  [{"text": "x", "min_formalization_rank": "very_true"}],
                                  {}, task_id="t4")

    def test_hand_built_logs_gate_the_rank_key_by_version(self):
        ranked = [{"id": "C1", "text": "Checked result", "min_formalization_rank": "kernel_proof"}]
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_raw_workspace(Path(tmp), "3.0", ranked)
            with self.assertRaises(IntegrityError):
                engine.load()
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_raw_workspace(
                Path(tmp), "4.0",
                [{"id": "C1", "text": "Checked result", "min_formalization_rank": "very_true"}],
            )
            with self.assertRaises(IntegrityError):
                engine.load()
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_raw_workspace(Path(tmp), "4.0", ranked)
            state = engine.load()
            self.assertEqual(state["criteria"][0]["min_formalization_rank"], "kernel_proof")

    def test_add_criterion_rank_is_v4_only(self):
        harness = Harness(self)
        state = harness.engine.add_criterion({
            "request_id": "add-ranked",
            "text": "The follow-on claim is machine-checked",
            "rationale": "the result needs a mechanical check",
            "min_formalization_rank": "smt_discharge",
        })
        self.assertEqual(state["criteria"][-1]["min_formalization_rank"], "smt_discharge")
        with tempfile.TemporaryDirectory() as tmp:
            engine = LoopEngine.create(Path(tmp), "A 3.0 task", ["Done"], {},
                                       task_id="t3", policy_version="3.0")
            with self.assertRaises(TransitionError):
                engine.add_criterion({
                    "request_id": "add-ranked",
                    "text": "Another criterion",
                    "rationale": "why",
                    "min_formalization_rank": "smt_discharge",
                })

    def test_criteria_file_flag_parses_and_creates_ranked_criteria(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["init", "t", "--criterion", "a", "--criteria-file", "f.json"])
        with tempfile.TemporaryDirectory() as tmp:
            criteria_path = Path(tmp) / "criteria.json"
            criteria_path.write_text(json.dumps([
                {"text": "Machine-checked claim", "min_formalization_rank": "kernel_proof"},
                "Plain claim",
            ]), encoding="utf-8")
            args = parser.parse_args([
                "init", "A ranked task", "--criteria-file", str(criteria_path),
                "--root", str(Path(tmp) / "ws"), "--task-id", "ranked",
            ])
            result = dispatch(args)
            self.assertEqual(result["state"]["criteria"][0]["min_formalization_rank"], "kernel_proof")
            self.assertIsNone(result["state"]["criteria"][1]["min_formalization_rank"])


class EvidenceRankTests(unittest.TestCase):
    def test_evidence_rank_fields_are_v4_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = LoopEngine.create(Path(tmp), "A 3.0 task", ["Done"], {},
                                       task_id="t3", policy_version="3.0")
            harness = Harness(self)
            harness.engine = engine
            harness.plan()
            with self.assertRaisesRegex(ValidationError, "unknown fields"):
                harness.research("v3-ranked", extra_evidence=[
                    harness.evidence("r1", ["C1"], rank="sourced_claim"),
                ])
        harness = Harness(self)
        harness.plan()
        state = harness.research("v4-ranked", extra_evidence=[
            harness.evidence("r1", ["C1"], rank="sourced_claim"),
        ])
        recorded = state["evidence"]["E000001"]
        self.assertEqual(recorded["formalization_rank"], "sourced_claim")

    def test_checked_ranks_require_an_accepting_checker(self):
        harness = Harness(self)
        harness.plan()
        with self.assertRaisesRegex(ValidationError, "accepting checker"):
            harness.research("no-checker", extra_evidence=[
                harness.evidence("r1", ["C1"], rank="smt_discharge"),
            ])
        with self.assertRaisesRegex(ValidationError, "did not accept"):
            harness.research("refused", extra_evidence=[
                harness.evidence("r2", ["C1"], rank="sourced_claim",
                                 checker=accepting_checker("refused", accepted=False)),
            ])
        state = harness.research("mixed", extra_evidence=[
            harness.evidence("below-floor", ["C1"], rank="executable_spec"),
            harness.evidence("observation", ["C1"], quality="indirect",
                             checker=accepting_checker("observation", accepted=False)),
        ])
        ranks = {item["id"]: item.get("formalization_rank") for item in state["evidence"].values()}
        self.assertEqual(ranks["E000001"], "executable_spec")
        self.assertIsNone(ranks["E000002"])
        self.assertFalse(state["evidence"]["E000002"]["checker"]["accepted"])

    def test_checker_artifact_hash_must_equal_the_fingerprint(self):
        harness = Harness(self)
        harness.plan()
        item = harness.evidence("r1", ["C1"], rank="kernel_proof",
                                checker=accepting_checker("bound"))
        item["fingerprint"] = digest("something-else")
        with self.assertRaisesRegex(ValidationError, "artifact hash"):
            harness.research("mismatch", extra_evidence=[item])


class RankGateTests(unittest.TestCase):
    def ranked_harness(self):
        harness = Harness(self, criteria=[
            {"text": "The result is machine-checked", "min_formalization_rank": "kernel_proof"},
        ])
        # A checked rank with no registered backend owes a harness diagnosis
        # before anything else; adopt the checker hook overlay to discharge it.
        harness.surgeon("harness_gap", delta={
            "schema_version": 1,
            "ops": [{
                "op": "register_validator_hook",
                "hook_id": "proof-check",
                "rank": "kernel_proof",
                "command": ["python3", "-c", "pass"],
                "timeout_seconds": 60,
            }],
        })
        harness.overlay_review("adopt")
        return harness

    def test_satisfaction_needs_same_item_ranked_direct_evidence(self):
        harness = self.ranked_harness()
        harness.plan()
        self.assertFalse(criteria_evidence_ready(harness.engine.load()))
        with self.assertRaisesRegex(ValidationError, "rank-gated"):
            harness.research("prose", satisfy=["C1"])
        with self.assertRaisesRegex(ValidationError, "rank-gated"):
            harness.research("weak", satisfy=["C1"],
                             evidence_kwargs={"rank": "executable_spec"})
        state = harness.research("proved", satisfy=["C1"],
                                 evidence_kwargs={"rank": "kernel_proof",
                                                  "checker": accepting_checker("primary")})
        self.assertEqual(state["criteria"][0]["status"], "satisfied")
        self.assertTrue(criteria_evidence_ready(state))
        harness.critique("validated_progress")
        self.assertTrue(criteria_evidence_ready(harness.engine.load()))

    def test_verification_and_completion_need_a_second_checker_run(self):
        harness = self.ranked_harness()
        harness.plan()
        harness.research("proved", satisfy=["C1"],
                         evidence_kwargs={"rank": "kernel_proof",
                                          "checker": accepting_checker("primary")})
        harness.critique("validated_progress")
        with self.assertRaisesRegex(ValidationError, "minimum rank"):
            harness.verify("prose-verify")
        harness.verify("checked-verify",
                       evidence_kwargs={"rank": "kernel_proof",
                                        "checker": accepting_checker("independent")})
        harness.synthesize()
        state = harness.engine.finalize()
        self.assertEqual(state["status"], "completed")

    def test_unranked_criteria_are_untouched_by_the_gates(self):
        harness = Harness(self)
        harness.ready_for_completion()
        state = harness.engine.finalize()
        self.assertEqual(state["status"], "completed")


if __name__ == "__main__":
    unittest.main()
