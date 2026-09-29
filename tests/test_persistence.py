"""Pillar A: stopping is a governed state, not an accident.

Every fixture here is deliberately mundane and field-free — reconciling
reports, checking a supplier feed — because the machinery must behave
identically for any task in any field.
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.adv_loop.cli import build_parser, guard_report
from src.adv_loop.engine import LoopEngine
from src.adv_loop.errors import IdempotencyConflict, TransitionError, ValidationError
from src.adv_loop.storage import canonical_json, object_hash

try:
    from test_engine import Harness
except ImportError:  # invoked as tests.test_persistence rather than via discovery
    from tests.test_engine import Harness


# Keys that may exist only in 4.0 (or later) state. Freeze rails assert absence.
V4_ONLY_STATE_KEYS = (
    "overlays", "overlay_revision", "evidence_kind_registry", "validator_hooks",
    "sandboxes", "stall_classes", "role_instruction_overlays", "toolchain_pins",
    "process_faults", "pending_overlay_review", "pending_overlay_adoption", "surgeon_marks",
)
# Keys that may exist only in 5.0 state.
V5_ONLY_STATE_KEYS = ("charter_hash",)


def drive_to_blocked(case):
    """Walk a workspace honestly through the whole ladder into blocked."""

    harness = Harness(case)
    harness.plan()
    for index in range(1, 8):
        harness.fail_cycle(f"stuck-{index}", dependency_evidence=index == 1)
    harness.escalation("audit")
    state = harness.engine.load()
    research_ids = [attempt["id"] for attempt in state["attempts"] if attempt["role"] == "researcher"][:3]
    direct_evidence = next(item["id"] for item in state["evidence"].values() if item["quality"] == "direct")
    harness.fail_cycle("post-audit")
    harness.fail_cycle("probe-rung")
    harness.fail_cycle("combine-rung")
    harness.drain_escalations("cycle-two")
    decision = {
        "request_id": "block-it",
        "dependency": "private external API",
        "attempt_ids": research_ids,
        "evidence_ids": [direct_evidence],
        "alternatives_exhausted": "every authorized route was tried and refused",
        "human_action": "grant read access",
        "retest": {
            "premise": "the supplier endpoint refuses every authorized credential",
            "probe": "retry the documented endpoint with the issued credential",
            "recheck_after": "2026-01-01T00:00:00Z",
        },
    }
    harness.engine.mark_blocked(decision)
    return harness


class GuardTests(unittest.TestCase):
    def test_guard_flags_active_and_accepts_terminal_and_awaiting_human(self):
        harness = Harness(self)
        report = guard_report([harness.engine.workspace])
        self.assertEqual(report["exit_code"], 1)
        self.assertFalse(report["ok"])
        row = report["workspaces"][0]
        self.assertEqual(row["status"], "active")
        kinds = {item["kind"] for item in row["obligations"]}
        self.assertIn("pending_directive", kinds)

        harness.engine.request_human_input({
            "request_id": "ask-1",
            "question": "Which of the two conflicting reports is authoritative?",
            "classification": "other_human_judgment",
        })
        report = guard_report([harness.engine.workspace])
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["workspaces"][0]["status"], "awaiting_human")

        harness.engine.provide_human_input({"request_id": "answer-1", "answer": "The ledger export wins."})
        harness.ready_for_completion()
        harness.engine.finalize()
        report = guard_report([harness.engine.workspace])
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["workspaces"][0]["status"], "completed")

    def test_guard_reports_exit_code_two_for_a_broken_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "broken"
            workspace.mkdir()
            (workspace / "events.jsonl").write_text("not json\n", encoding="utf-8")
            report = guard_report([workspace])
            self.assertEqual(report["exit_code"], 2)
            self.assertFalse(report["workspaces"][0]["ok"])

    def test_guard_flags_a_blocked_workspace_whose_retest_is_due(self):
        harness = drive_to_blocked(self)
        report = guard_report([harness.engine.workspace])
        row = report["workspaces"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(row["obligations"][0]["kind"], "retest_due")

        harness.engine.record_retest({
            "request_id": "retest-1",
            "outcome": "premise_holds",
            "note": "endpoint still refuses the credential",
            "recheck_after": "2999-01-01T00:00:00Z",
        })
        report = guard_report([harness.engine.workspace])
        self.assertEqual(report["exit_code"], 0)

        harness.engine.record_retest({
            "request_id": "retest-2",
            "outcome": "premise_no_longer_holds",
            "note": "the endpoint now answers with data",
        })
        report = guard_report([harness.engine.workspace])
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["workspaces"][0]["obligations"][0]["kind"], "unblock_due")


class LeaseTests(unittest.TestCase):
    def test_next_writes_the_obligation_lease_and_record_clears_it(self):
        harness = Harness(self)
        directive = harness.engine.next_instruction()
        lease_path = harness.engine.lease_path
        self.assertTrue(lease_path.exists())
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["directive_id"], directive["directive_id"])
        self.assertEqual(lease["role"], "planner")

        supervision = harness.engine.supervision()
        self.assertFalse(supervision["pending_directive"]["stale"])
        self.assertEqual(supervision["pending_directive"]["events_since"], 0)

        harness.plan()
        self.assertFalse(lease_path.exists())

    def test_supervision_marks_a_lease_stale_after_another_writer_lands(self):
        harness = Harness(self)
        harness.engine.next_instruction()
        harness.plan()
        # Simulate a crashed session that still holds the old lease file.
        stale_lease = {
            "issued_at": "2026-01-01T00:00:00+00:00",
            "action": "attempt",
            "directive_id": "D-000000000000000000000000",
            "role": "planner",
            "mode": "initial_plan",
            "required_move": None,
            "event_head": "0" * 64,
            "revision": 1,
        }
        harness.engine.lease_path.write_text(canonical_json(stale_lease) + "\n", encoding="utf-8")
        supervision = harness.engine.supervision()
        self.assertTrue(supervision["pending_directive"]["stale"])
        self.assertGreaterEqual(supervision["pending_directive"]["events_since"], 1)

    def test_supervision_reports_stall_past_the_configured_horizon(self):
        harness = Harness(self)
        (harness.engine.workspace / "loop-config.json").write_text(
            json.dumps({"stall_horizon_seconds": 0}), encoding="utf-8"
        )
        supervision = harness.engine.supervision()
        self.assertTrue(supervision["stalled"])
        self.assertEqual(supervision["stall_horizon_seconds"], 0)


class AwaitingHumanTests(unittest.TestCase):
    def test_ask_and_answer_round_trip_preserves_the_pending_work(self):
        harness = Harness(self)
        harness.plan()
        before = harness.engine.next_instruction()
        state = harness.engine.request_human_input({
            "request_id": "ask-1",
            "question": "The two dashboards disagree; which export is the source of truth?",
            "context": "weekly report reconciliation",
            "classification": "other_human_judgment",
        })
        self.assertEqual(state["status"], "awaiting_human")
        step = harness.engine.next_instruction()
        self.assertEqual(step["action"], "await_human")
        self.assertIn("dashboards disagree", step["question"])

        with self.assertRaises(TransitionError):
            harness.engine.record_attempt({
                "request_id": "no-attempts-while-waiting",
                "directive_id": before["directive_id"],
            })

        state = harness.engine.provide_human_input({
            "request_id": "answer-1",
            "answer": "The ledger export is authoritative.",
        })
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["human_exchanges"][0]["answer"], "The ledger export is authoritative.")
        resumed = harness.engine.next_instruction()
        self.assertEqual(resumed["action"], "attempt")
        self.assertEqual(resumed["role"], "researcher")
        self.assertTrue(harness.engine.audit()["ok"])

    def test_answer_requires_a_pending_question_and_requests_are_idempotent(self):
        harness = Harness(self)
        with self.assertRaises(TransitionError):
            harness.engine.provide_human_input({"request_id": "answer-x", "answer": "nothing was asked"})
        decision = {"request_id": "ask-1", "question": "Proceed with the fallback source?",
                    "classification": "authorization"}
        harness.engine.request_human_input(decision)
        state = harness.engine.request_human_input(decision)
        self.assertEqual(state["status"], "awaiting_human")
        with self.assertRaises(IdempotencyConflict):
            harness.engine.request_human_input({"request_id": "ask-1", "question": "different question",
                                                "classification": "authorization"})


class RevivalTests(unittest.TestCase):
    def test_blocked_records_the_retest_and_unblock_returns_to_active(self):
        harness = drive_to_blocked(self)
        state = harness.engine.load()
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(
            state["terminal"]["retest"]["premise"],
            "the supplier endpoint refuses every authorized credential",
        )
        supervision = harness.engine.supervision(state)
        self.assertTrue(supervision["retest"]["due"])

        with self.assertRaises(TransitionError):
            harness.engine.record_attempt({"request_id": "r", "directive_id": "D-x"})

        revived = harness.engine.unblock({
            "request_id": "unblock-1",
            "reason": "the supplier endpoint now answers authorized requests",
            "note": "verified by the recorded retest probe",
        })
        self.assertEqual(revived["status"], "active")
        self.assertIsNone(revived["terminal"])
        self.assertEqual(revived["revivals"][0]["reason"], "the supplier endpoint now answers authorized requests")
        # History is intact and the loop resumes exactly where it left off.
        self.assertEqual(revived["attempt_count"], harness.engine.load()["attempt_count"])
        self.assertEqual(harness.engine.next_instruction()["action"], "attempt")
        self.assertTrue(harness.engine.audit()["ok"])

    def test_block_without_a_retest_premise_is_refused(self):
        harness = Harness(self)
        harness.plan()
        for index in range(1, 8):
            harness.fail_cycle(f"nr-{index}", dependency_evidence=index == 1)
        harness.escalation("audit")
        harness.fail_cycle("post")
        harness.fail_cycle("probe")
        harness.fail_cycle("combine")
        harness.drain_escalations("cycle-two")
        state = harness.engine.load()
        research_ids = [a["id"] for a in state["attempts"] if a["role"] == "researcher"][:3]
        direct_evidence = next(i["id"] for i in state["evidence"].values() if i["quality"] == "direct")
        with self.assertRaisesRegex(ValidationError, "retest"):
            harness.engine.mark_blocked({
                "request_id": "no-retest",
                "dependency": "private external API",
                "attempt_ids": research_ids,
                "evidence_ids": [direct_evidence],
                "alternatives_exhausted": "all routes tried",
                "human_action": "grant read access",
            })


class DurabilityHookTests(unittest.TestCase):
    def test_on_record_hook_runs_after_every_append(self):
        harness = Harness(self)
        (harness.engine.workspace / "loop-config.json").write_text(
            json.dumps({"on_record": [
                "python3", "-c",
                "import pathlib; p = pathlib.Path('.hook-count');"
                " p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else '1')",
            ]}),
            encoding="utf-8",
        )
        harness.plan()
        count_path = harness.engine.workspace / ".hook-count"
        self.assertTrue(count_path.exists())
        status = json.loads(harness.engine.on_record_status_path.read_text(encoding="utf-8"))
        self.assertTrue(status["ok"])
        supervision = harness.engine.supervision()
        self.assertEqual(supervision["on_record"]["events_since"], 0)

    def test_on_record_failure_is_surfaced_not_swallowed(self):
        harness = Harness(self)
        (harness.engine.workspace / "loop-config.json").write_text(
            json.dumps({"on_record": ["python3", "-c", "import sys; sys.exit(3)"]}),
            encoding="utf-8",
        )
        harness.plan()
        status = json.loads(harness.engine.on_record_status_path.read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertEqual(status["returncode"], 3)
        supervision = harness.engine.supervision()
        self.assertFalse(supervision["on_record"]["ok"])


class V2FreezeEventTests(unittest.TestCase):
    """The additive events work on 2.0 logs without touching 2.0 semantics."""

    @staticmethod
    def make_v2_workspace(root: Path) -> LoopEngine:
        workspace = root / "v2-task"
        workspace.mkdir()
        payload = {
            "protocol_version": 2,
            "policy_version": "2.0",
            "task_id": "v2-task",
            "task": "Reconcile the two conflicting weekly reports",
            "criteria": [{"id": "C1", "text": "The discrepancy is explained with direct evidence"}],
            "budget": {},
        }
        unsigned = {
            "seq": 1,
            "at": "2026-01-01T00:00:00+00:00",
            "type": "task_created",
            "request_id": "create:v2-task",
            "payload": payload,
            "prev_hash": "0" * 64,
        }
        event = {**unsigned, "hash": object_hash(unsigned)}
        (workspace / "events.jsonl").write_text(canonical_json(event) + "\n", encoding="utf-8")
        engine = LoopEngine(workspace)
        engine.recover()
        return engine

    def test_v2_state_gains_no_v3_keys_and_no_v3_fields_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_v2_workspace(Path(tmp))
            state = engine.load()
            self.assertEqual(state["policy_version"], "2.0")
            for key in ("candidates", "basins", "criterion_failure_streaks", "pending_triage",
                        "assets", "barriers", "wishes", "lessons",
                        "controls") + V4_ONLY_STATE_KEYS + V5_ONLY_STATE_KEYS:
                self.assertNotIn(key, state)
            directive = engine.next_instruction()
            self.assertEqual(directive["mode"], "initial_plan")
            self.assertNotIn("lens", directive)

            plan_payload = {
                "request_id": "v2-plan",
                "directive_id": directive["directive_id"],
                "role": "planner",
                "mode": "initial_plan",
                "actor": {"agent_id": "a", "model": "m", "context_id": "c-1"},
                "strategy": {
                    "decomposition": "d", "source_class": "s", "retrieval_method": "r",
                    "reasoning_method": "m", "tool": "t", "verification_method": "v",
                },
                "hypothesis": "h", "action": "a", "observation": "o", "interpretation": "i",
                "uncertainties": [], "next_step": "n", "outcome": "progress",
                "evidence": [], "criterion_updates": [], "contradictions": [],
                "contradiction_resolutions": [], "decisions": [],
                "plan": {
                    "assumptions": ["x"], "subproblems": ["y"], "candidate_experiments": ["z"],
                    "falsification_tests": ["f"], "rejected_assumptions": [],
                },
            }
            engine.record_attempt(plan_payload)

            research = dict(plan_payload)
            research.pop("plan")
            directive = engine.next_instruction()
            research.update({
                "request_id": "v2-research",
                "directive_id": directive["directive_id"],
                "role": "researcher",
                "mode": "experiment",
                "plan_id": directive["active_plan_id"],
                "criterion_targets": ["C1"],
                "basin": "a-conceptual-family",
            })
            with self.assertRaisesRegex(ValidationError, "unknown fields"):
                engine.record_attempt(research)
            research.pop("basin")
            state = engine.record_attempt(research)
            self.assertEqual(state["attempt_count"], 2)
            self.assertTrue(engine.audit()["ok"])

    def test_v2_ask_human_pause_and_resume_works_without_version_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_v2_workspace(Path(tmp))
            paused = engine.request_human_input({
                "request_id": "v2-ask",
                "question": "Which report is authoritative?",
            })
            self.assertEqual(paused["status"], "awaiting_human")
            self.assertEqual(paused["policy_version"], "2.0")
            self.assertEqual(engine.next_instruction()["action"], "await_human")
            resumed = engine.provide_human_input({"request_id": "v2-answer", "answer": "The ledger."})
            self.assertEqual(resumed["status"], "active")
            self.assertEqual(engine.next_instruction()["mode"], "initial_plan")
            self.assertTrue(engine.audit()["ok"])


class V3FreezeTests(unittest.TestCase):
    """3.0 workspaces keep exact 3.0 semantics under a 4.0-default engine."""

    @staticmethod
    def make_v3_workspace(root: Path) -> LoopEngine:
        return LoopEngine.create(
            root,
            "Check the supplier feed against the ledger",
            ["The discrepancy is explained with direct evidence"],
            {},
            task_id="v3-task",
            policy_version="3.0",
        )

    def test_v3_state_gains_no_v4_keys_and_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_v3_workspace(Path(tmp))
            state = engine.load()
            self.assertEqual(state["policy_version"], "3.0")
            for key in V4_ONLY_STATE_KEYS + V5_ONLY_STATE_KEYS:
                self.assertNotIn(key, state)
            # 3.0 machinery is intact: controls exist, the planner is scheduled.
            self.assertIn("criterion_failure_streaks", state)
            directive = engine.next_instruction()
            self.assertEqual(directive["mode"], "initial_plan")
            self.assertTrue(engine.audit()["ok"])


class V4FreezeTests(unittest.TestCase):
    """4.0 workspaces keep exact 4.0 semantics under a 5.0-default engine."""

    def test_v4_state_gains_no_v5_keys_and_rejects_charter_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = LoopEngine.create(
                Path(tmp), "Check the supplier feed", ["Done"], {},
                task_id="v4-task", policy_version="4.0",
            )
            state = engine.load()
            self.assertEqual(state["policy_version"], "4.0")
            for key in V5_ONLY_STATE_KEYS:
                self.assertNotIn(key, state)
            self.assertIn("overlays", state)
            with self.assertRaises(TransitionError):
                LoopEngine.create(
                    Path(tmp), "Another task", ["Done"], {}, task_id="v4-charter",
                    policy_version="4.0", charter_hash="ab" * 32,
                )


class PolicyVersionPinTests(unittest.TestCase):
    def test_new_workspaces_default_to_the_current_version_with_v4_state(self):
        harness = Harness(self)
        state = harness.engine.load()
        self.assertEqual(state["policy_version"], "5.0")
        for key in V4_ONLY_STATE_KEYS:
            self.assertIn(key, state)
        self.assertEqual(state["overlay_revision"], 0)
        self.assertEqual(state["overlays"], [])
        self.assertIsNone(state["charter_hash"])

    def test_create_refuses_frozen_and_unknown_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError):
                LoopEngine.create(Path(tmp), "A task", ["Done"], {}, task_id="t-20", policy_version="2.0")
            with self.assertRaises(ValidationError):
                LoopEngine.create(Path(tmp), "A task", ["Done"], {}, task_id="t-99", policy_version="9.9")

    def test_init_policy_flag_parses_and_rejects_frozen_versions(self):
        parser = build_parser()
        args = parser.parse_args(["init", "A task", "--policy", "3.0"])
        self.assertEqual(args.policy, "3.0")
        args = parser.parse_args(["init", "A task"])
        self.assertIsNone(args.policy)
        with self.assertRaises(SystemExit):
            parser.parse_args(["init", "A task", "--policy", "2.0"])

    def test_init_charter_is_hashed_into_the_chain_and_copied_to_payload(self):
        from src.adv_loop.cli import dispatch
        import hashlib
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            charter = Path(tmp) / "charter.md"
            charter.write_text("# Charter\n\nGrants: none needed.\n", encoding="utf-8")
            args = parser.parse_args([
                "init", "A chartered task", "--charter", str(charter),
                "--root", str(Path(tmp) / "ws"), "--task-id", "chartered",
            ])
            result = dispatch(args)
            expected = hashlib.sha256(charter.read_bytes()).hexdigest()
            self.assertEqual(result["state"]["charter_hash"], expected)
            copied = Path(tmp) / "ws" / "chartered" / "payload" / "charter.md"
            self.assertEqual(copied.read_text(encoding="utf-8"), charter.read_text(encoding="utf-8"))
            engine = LoopEngine(Path(tmp) / "ws" / "chartered")
            self.assertTrue(engine.audit()["ok"])


if __name__ == "__main__":
    unittest.main()
