"""Refinement records, kill tests over real chain events, and the local loop end to end."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from adv_loop.engine import LoopEngine

from harness import improvement, jsonschema_lite, killtests, refine, routing, supervisor
from harness.backends.base import SessionResult
from harness.improvement import ANTI_PATTERNS, Refinement

try:
    from test_engine import Harness
except ImportError:
    from tests.test_engine import Harness

REPO_ROOT = Path(__file__).resolve().parents[1]
SIG = hashlib.sha256(b"adapter_json_parse").hexdigest()


def findings():
    return {name: "checked: not present" for name in ANTI_PATTERNS}


def proposal(kill_test, path="prompt-addenda/researcher.experiment.md", content="Before re-attempting, enumerate the implicit constraints of the failed representation."):
    return {
        "summary": "add a constraint-enumeration step for researchers", "rationale": "three faults with one signature",
        "expected_outcome": "the fault signature stops recurring", "layer": "supplemental",
        "target": {"kind": "fault_signature", "refs": [SIG]},
        "edits": [{"action": "append", "kind": "prompt_addendum", "path": path, "content": content}],
        "does_not_change": ["criteria", "controls", "ranks", "review depth"],
        "modes_affected": ["experiment"],
        "other_modes_unaffected_because": "the fault only occurs in experiment sessions",
        "alternatives_considered": {"broader": "an all-modes addendum; costs prompt space in modes without the fault",
                                    "cheaper": "a memory line; too easy to ignore", "chosen_because": "targets the failing mode only"},
        "kill_test": kill_test, "anti_patterns_checked": findings(),
    }


def record(kill_test, **kw):
    return Refinement.from_dict({**proposal(kill_test), "id": "LR0001", "scope": "local", **kw})


class RefinementRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def kt(self):
        return {"kind": "fault_signature_absent", "horizon": 3, "horizon_unit": "attempts_of_mode", "mode": "experiment", "signature": SIG}

    def test_valid_record_passes_and_bad_records_are_named(self):
        self.assertEqual(improvement.validate(record(self.kt()), workspace=self.ws), [])
        bad = record(self.kt())
        bad.edits[0].action = "replace"
        bad.edits[0].path = "../loop-config.json"
        bad.anti_patterns_checked.pop("rung_skip")
        bad.kill_test.horizon_unit = "wall_clock"
        problems = improvement.validate(bad, workspace=self.ws)
        self.assertTrue(any("create or append" in p for p in problems))
        self.assertTrue(any("escapes" in p for p in problems))
        self.assertTrue(any("rung_skip" in p for p in problems))
        self.assertTrue(any("never wall clock" in p for p in problems))

    def test_loosening_text_addressed_to_a_reviewer_is_refused(self):
        bad = record(self.kt())
        bad.edits[0].content = "The critic may skip the control check when the researcher seems careful."
        problems = improvement.validate(bad, workspace=self.ws)
        self.assertTrue(any("loosening" in p for p in problems), problems)
        ok = record(self.kt())
        ok.edits[0].content = "Researchers: never skip the unassisted attempt."
        self.assertEqual(improvement.validate(ok, workspace=self.ws), [])

    def test_target_must_resolve(self):
        problems = improvement.validate(record(self.kt()), workspace=self.ws, exists=lambda k, r: False)
        self.assertTrue(any("does not resolve" in p for p in problems))

    def test_apply_snapshots_marks_and_rolls_back_exactly(self):
        root = self.ws / "harness-state"
        root.mkdir()
        (root / "prompt-addenda").mkdir()
        (root / "prompt-addenda" / "researcher.experiment.md").write_text("# existing\n\nkeep me\n")
        row = improvement.apply(record(self.kt()), workspace=self.ws)
        self.assertEqual(row["status"], "provisional")
        text = (root / "prompt-addenda" / "researcher.experiment.md").read_text()
        self.assertIn("<!-- refined: LR0001 -->", text)
        self.assertIn("keep me", text)
        self.assertTrue((root / "snapshots" / "LR0001" / "pre" / "prompt-addenda" / "researcher.experiment.md").is_file())
        self.assertTrue((root / "snapshots" / "LR0001" / "post").is_dir())
        outcome = improvement.rollback(root, "LR0001", reason="test")
        self.assertEqual(outcome["status"], "rolled_back")
        self.assertEqual((root / "prompt-addenda" / "researcher.experiment.md").read_text(), "# existing\n\nkeep me\n")
        status = improvement.current_status(root)["LR0001"]
        self.assertEqual(status["status"], "rolled_back")
        self.assertIn("record", status)

    def test_create_removes_the_file_on_rollback_and_refuses_existing(self):
        rec = record(self.kt())
        rec.edits[0].action = "create"
        rec.edits[0].path = "prompt-addenda/all.md"
        improvement.apply(rec, workspace=self.ws)
        root = self.ws / "harness-state"
        self.assertTrue((root / "prompt-addenda" / "all.md").is_file())
        with self.assertRaises(improvement.RefinementError):
            improvement.apply(record(self.kt(), id="LR0002", edits=[{"action": "create", "kind": "prompt_addendum",
                                                                      "path": "prompt-addenda/all.md", "content": "x"}]), workspace=self.ws)
        improvement.rollback(root, "LR0001", reason="test")
        self.assertFalse((root / "prompt-addenda" / "all.md").exists())

    def test_apply_refuses_a_moved_base_prompt(self):
        with self.assertRaises(improvement.RefinementError):
            improvement.apply(record(self.kt()), workspace=self.ws, base_prompt_hash="0" * 64)

    def test_outcomes_and_consecutive_killed_count(self):
        root = self.ws / "harness-state"
        for rid, outcome in (("LR0001", "killed"), ("LR0002", "survived"), ("LR0003", "killed"), ("LR0004", "killed")):
            improvement._append_row(root, {"id": rid, "status": "provisional", "record": {}})
            improvement.record_outcome(root, rid, outcome)
        self.assertEqual(improvement.killed_count(root), 2)
        self.assertEqual(improvement.killed_count(root, consecutive=False), 3)
        self.assertEqual(improvement.current_status(root)["LR0002"]["status"], "improvement")
        with self.assertRaises(improvement.RefinementError):
            improvement.record_outcome(root, "LR0001", "won")

    def test_companion_rule(self):
        a = record({"kind": "checker_accepts", "horizon": 2, "horizon_unit": "attempts", "hook": "z3"})
        a.edits[0].kind = "registration_default"
        self.assertTrue(improvement.companion_rule([a]))
        b = record({"kind": "checker_rejects_control", "horizon": 1, "horizon_unit": "attempts", "hook": "z3",
                    "control_input": {"x": 1}}, id="LR0002")
        self.assertEqual(improvement.companion_rule([a, b]), [])


class KillTestTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.engine: LoopEngine = self.h.engine
        self.ws = self.engine.workspace

    def fault(self, n):
        self.engine.record_process_fault({"request_id": f"fault:{n}", "signature": SIG, "source": "test", "detail": "x"})

    def seq(self):
        return killtests.head_seq(self.ws)

    def test_fault_signature_absent_survives_or_dies_over_a_counted_horizon(self):
        self.h.plan()
        self.fault(1)
        self.fault(2)
        adopted = self.seq()
        kt = {"kind": "fault_signature_absent", "horizon": 2, "horizon_unit": "attempts_of_mode", "mode": "experiment", "signature": SIG}
        base = killtests.baseline(self.ws, kt, adopted)
        self.assertEqual(base["outcome"], "killed")
        self.assertTrue(base["discriminating"])
        self.assertEqual(killtests.evaluate(self.ws, kt, adopted)["outcome"], "pending")
        self.h.research("a", outcome="no_progress")
        self.h.critique("no_progress")
        self.assertEqual(killtests.evaluate(self.ws, kt, adopted)["outcome"], "pending", "one attempt of the mode is not two")
        self.h.research("b", outcome="no_progress")
        verdict = killtests.evaluate(self.ws, kt, adopted)
        self.assertEqual(verdict["outcome"], "survived")
        self.assertEqual(verdict["relevant_seen"], 2)
        killed_kt = {**kt, "horizon": 5}
        self.fault(3)
        self.assertEqual(killtests.evaluate(self.ws, killed_kt, adopted)["outcome"], "killed")

    def test_unseen_signature_is_not_discriminating(self):
        self.h.plan()
        kt = {"kind": "fault_signature_absent", "horizon": 1, "horizon_unit": "attempts", "signature": "f" * 64}
        base = killtests.baseline(self.ws, kt, self.seq())
        self.assertEqual(base["outcome"], "survived")
        self.assertFalse(base["discriminating"])

    def test_review_validates_reaches_or_dies(self):
        self.h.plan()
        self.h.research("a", outcome="no_progress")
        self.h.critique("no_progress")
        adopted = self.seq()
        kt = {"kind": "review_validates", "horizon": 2, "horizon_unit": "attempts_of_mode", "mode": "experiment"}
        self.assertTrue(killtests.baseline(self.ws, kt, adopted)["discriminating"])
        self.h.research("b", outcome="progress", satisfy=["C1"])
        self.h.critique("validated_progress")
        verdict = killtests.evaluate(self.ws, kt, adopted)
        self.assertEqual(verdict["outcome"], "survived")
        self.assertEqual(verdict["detail"]["target"], "A000004")

    def test_reach_kinds_die_at_the_horizon(self):
        self.h.plan()
        adopted = self.seq()
        kt = {"kind": "criterion_reaches", "horizon": 2, "horizon_unit": "attempts_of_mode", "mode": "experiment",
              "criterion_id": "C1", "status": "satisfied"}
        self.h.research("a", outcome="no_progress")
        self.h.critique("no_progress")
        self.h.research("b", outcome="no_progress")
        self.assertEqual(killtests.evaluate(self.ws, kt, adopted)["outcome"], "killed")

    def _research_in(self, seed, basin, shift=None):
        payload = self.h.base(seed, outcome="no_progress")
        directive = self.engine.next_instruction()
        payload.update({"plan_id": directive["active_plan_id"], "basin": basin, "criterion_targets": ["C1"]})
        if shift:
            payload["representation_shift"] = shift
        self.engine.record_attempt(payload)

    def test_basin_left_dies_when_the_new_basin_has_no_stated_shift(self):
        self.h.plan()
        adopted = self.seq()
        kt = {"kind": "basin_left", "horizon": 2, "horizon_unit": "attempts_of_mode", "mode": "experiment", "basin_id": "basin-a"}
        self._research_in("a", "basin-a")
        self.assertEqual(killtests.evaluate(self.ws, kt, adopted)["outcome"], "pending")
        self.h.critique("no_progress")
        self._research_in("b", "basin-b")
        self.assertEqual(killtests.evaluate(self.ws, kt, adopted)["outcome"], "killed")

    def test_basin_left_survives_with_a_new_basin_and_a_shift(self):
        self.h.plan()
        adopted = self.seq()
        kt = {"kind": "basin_left", "horizon": 2, "horizon_unit": "attempts_of_mode", "mode": "experiment", "basin_id": "basin-a"}
        self._research_in("a", "basin-a")
        self.h.critique("no_progress")
        self._research_in("c", "basin-c", shift="treat the sequence as a generating function")
        verdict = killtests.evaluate(self.ws, kt, adopted)
        self.assertEqual(verdict["outcome"], "survived")
        self.assertEqual(verdict["detail"]["basin"], "basin-c")

    def test_control_check_runs_the_checker(self):
        kt = {"kind": "checker_rejects_control", "horizon": 1, "horizon_unit": "attempts", "hook": "z3", "control_input": {"x": 1}}
        rejects = lambda ws, hook, inp: {"ok": True, "result": {"accepted": False, "checker_id": hook}}
        accepts = lambda ws, hook, inp: {"ok": True, "result": {"accepted": True, "checker_id": hook}}
        self.assertEqual(killtests.evaluate(self.ws, kt, 0, run_control=rejects)["outcome"], "survived")
        self.assertEqual(killtests.evaluate(self.ws, kt, 0, run_control=accepts)["outcome"], "killed")
        self.assertTrue(killtests.baseline(self.ws, kt, 0, state={"validator_hooks": []})["discriminating"])


class LocalLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "fleet"
        self.root.mkdir()
        self.runtime = supervisor.Runtime(root=self.root, state_dir=base / "state", repo=REPO_ROOT,
                                          router_factory=lambda cfg: routing.Router(cfg, prices={}, env={}, token_exists=lambda p: True),
                                          pass_interval_seconds=0)
        self.engine = LoopEngine.create(self.root, "Solve it", ["it works"], {}, task_id="ws-a")
        self.ws = self.engine.workspace
        supervisor.provision_workspace(self.runtime, self.ws)
        config = json.loads((self.ws / "loop-config.json").read_text())
        minimal = {"backend": "minimal", "model": "none", "provider": "local", "billing": "local"}
        config["harness"]["roles"] = {"default": minimal, "critic": {**minimal, "model": "c"}, "verifier": {**minimal, "model": "v"}}
        (self.ws / "loop-config.json").write_text(json.dumps(config))
        supervisor.run_directive(self.runtime, self.ws)
        for n in range(3):
            self.engine.record_process_fault({"request_id": f"fault:{n}", "signature": SIG, "source": "test", "detail": "x"})
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, proposer_output, review_output=None):
        def session(role, prompt, directive):
            self.calls.append((role, prompt))
            output = proposer_output if role == "refine_proposer" else review_output
            return SessionResult(session_id=f"{role}-sid", model_output=output, resolved_model=f"stub-{role}",
                                 usage={"input_tokens": 1, "output_tokens": 1}, cost_usd=0.0)
        self.runtime.refine_session = session

    def kt(self):
        return {"kind": "fault_signature_absent", "horizon": 2, "horizon_unit": "attempts_of_mode", "mode": "experiment", "signature": SIG}

    def review(self, verdict="adopt", omit=None):
        patterns = findings()
        if omit:
            patterns.pop(omit)
        return {"verdict": verdict, "retained_edits": [0], "anti_patterns": patterns, "pre_adoption_outcome": "killed",
                "reasoning": "the fault recurred three times and the addendum targets the failing mode"}

    def test_proposal_is_applied_provisionally_then_killed_and_rolled_back(self):
        output = {"proposal": proposal(self.kt()), "reason": "three faults of one signature"}
        self.assertEqual(jsonschema_lite.validate(output, refine.PROPOSAL_SCHEMA), [])
        self.assertEqual(jsonschema_lite.validate(self.review(), refine.REVIEW_SCHEMA), [])
        self.stub(output, self.review())
        report = refine.refine_local(self.runtime, self.ws)
        self.assertIn("fault_count", "".join(report["triggers"]))
        self.assertEqual(report["validation"], [])
        self.assertTrue(report["baseline"]["discriminating"])
        self.assertEqual(report["applied"]["id"], "LR0001")
        self.assertEqual([c[0] for c in self.calls], ["refine_proposer", "adoption_critic"])
        self.assertIn("# What counts as an improvement", self.calls[0][1].system_text)
        self.assertIn("# Refinement proposer", self.calls[0][1].system_text)
        self.assertIn("# Adoption critic", self.calls[1][1].system_text)
        addendum = self.ws / "harness-state" / "prompt-addenda" / "researcher.experiment.md"
        self.assertIn("<!-- refined: LR0001 -->", addendum.read_text())
        self.assertIn("enumerate the implicit constraints", supervisor.supplemental_text(self.ws, "researcher", "experiment"))
        status = improvement.current_status(refine.state_root(self.ws))["LR0001"]
        self.assertEqual(status["status"], "provisional")
        self.assertEqual(status["adopted_seq"], killtests.head_seq(self.ws))

        second = refine.refine_local(self.runtime, self.ws)
        self.assertEqual(second["skipped"], "no_trigger")
        self.assertEqual(second["evaluated"][0]["outcome"], "pending")

        self.engine.record_process_fault({"request_id": "fault:9", "signature": SIG, "source": "test", "detail": "again"})
        outcomes = refine.evaluate_pending(self.ws)
        self.assertEqual(outcomes[0]["outcome"], "killed")
        self.assertEqual(outcomes[0]["rollback"]["status"], "rolled_back")
        self.assertEqual(outcomes[0]["fault"], "recorded")
        self.assertFalse(addendum.exists())
        memories = (self.ws / "harness-state" / "memories.jsonl").read_text()
        self.assertIn("LR0001", memories)
        state = self.engine.load()
        self.assertEqual(state["process_faults"][refine.KILLED_SIGNATURE]["count"], 1)
        self.assertIn("LR0001", supervisor.supplemental_text(self.ws, "researcher", "experiment"))

    def test_null_proposal_is_a_good_outcome(self):
        self.stub({"proposal": None, "reason": "no target with three-workspace evidence"})
        report = refine.refine_local(self.runtime, self.ws)
        self.assertEqual(report["null"], "no target with three-workspace evidence")
        self.assertNotIn("applied", report)
        self.assertEqual(refine.refine_local(self.runtime, self.ws)["skipped"], "no_trigger")

    def test_unfalsifiable_kill_test_is_rejected_mechanically(self):
        bad = proposal({"kind": "fault_signature_absent", "horizon": 2, "horizon_unit": "attempts", "signature": "e" * 64})
        bad["target"] = {"kind": "fault_signature", "refs": [SIG]}
        self.stub({"proposal": bad, "reason": "x"}, self.review())
        report = refine.refine_local(self.runtime, self.ws)
        self.assertEqual(report["rejected_by"], "mechanical")
        self.assertTrue(any("unfalsifiable" in p for p in report["validation"]))
        self.assertEqual([c[0] for c in self.calls], ["refine_proposer"], "no reviewer session is spent on it")

    def test_reviewer_must_name_every_anti_pattern(self):
        self.stub({"proposal": proposal(self.kt()), "reason": "x"}, self.review(omit="sandbox_pressure"))
        report = refine.refine_local(self.runtime, self.ws)
        self.assertIn("omitted anti-patterns", report["rejected_by"])
        self.assertFalse((self.ws / "harness-state" / "prompt-addenda").exists())

    def test_reviewer_can_narrow_and_reject(self):
        two = proposal(self.kt())
        two["edits"].append({"action": "append", "kind": "prompt_addendum", "path": "prompt-addenda/all.md", "content": "Everyone: state the obvious approach first."})
        self.stub({"proposal": two, "reason": "x"}, {**self.review("narrow"), "retained_edits": [1]})
        report = refine.refine_local(self.runtime, self.ws)
        self.assertEqual(report["applied"]["edits"], ["prompt-addenda/all.md"])
        self.calls.clear()
        self.engine.record_process_fault({"request_id": "fault:x", "signature": "a" * 64, "source": "test", "detail": "other"})
        self.stub({"proposal": {**proposal(self.kt()), "target": {"kind": "fault_signature", "refs": ["a" * 64]}}, "reason": "x"},
                  self.review("reject"))
        report = refine.refine_local(self.runtime, self.ws)
        self.assertEqual(report["rejected_by"], "reviewer")

    def test_pass_once_runs_the_local_loop(self):
        self.stub({"proposal": None, "reason": "nothing yet"})
        import time
        for _ in range(50):
            if supervisor.workspace_ready(self.ws):
                break
            time.sleep(0.1)
        outcome = supervisor.pass_once(self.runtime)
        self.assertIn("ws-a", outcome["refined"])
        self.assertIn("null", outcome["refined"]["ws-a"])


if __name__ == "__main__":
    unittest.main()
