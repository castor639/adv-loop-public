"""The global loop: cross-workspace signal, K independence, three fresh stages, majority kill tests, PR bundles, never a chain write."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from adv_loop.engine import LoopEngine

from harness import improvement, jsonschema_lite, refine_global, routing, signal, supervisor
from harness.backends.base import SessionResult
from harness.improvement import ANTI_PATTERNS

REPO_ROOT = Path(__file__).resolve().parents[1]
SIG = hashlib.sha256(b"adapter_json_parse").hexdigest()
MINIMAL = {"backend": "minimal", "model": "none", "provider": "local", "billing": "local"}


def findings():
    return {name: "checked: not present" for name in ANTI_PATTERNS}


def proposal(workspaces, layer="supplemental", kill_test=None, edits=None, **extra):
    body = {
        "summary": "fleet-wide constraint enumeration before re-attempts", "rationale": "the same fault recurs in three workspaces",
        "expected_outcome": "the fault signature stops recurring", "layer": layer,
        "target": {"kind": "fault_signature", "refs": [SIG]},
        "edits": edits if edits is not None else [{"action": "append", "kind": "prompt_addendum",
                                                    "path": "prompt-addenda/researcher.experiment.md",
                                                    "content": "Enumerate the implicit constraints of the failed representation before re-attempting."}],
        "does_not_change": ["criteria", "controls", "ranks", "review depth"],
        "modes_affected": ["experiment"], "other_modes_unaffected_because": "the fault only occurs in experiment sessions",
        "alternatives_considered": {"broader": "all modes", "cheaper": "a memory line", "chosen_because": "targets the failing mode"},
        "kill_test": kill_test or {"kind": "fault_signature_absent", "horizon": 2, "horizon_unit": "attempts_of_mode",
                                   "mode": "experiment", "signature": SIG},
        "anti_patterns_checked": findings(), "evidence_workspaces": list(workspaces),
    }
    body.update(extra)
    return body


def review(verdict="adopt"):
    return {"verdict": verdict, "retained_edits": [0], "anti_patterns": findings(), "pre_adoption_outcome": "killed",
            "reasoning": "three workspaces, one signature, targeted mode"}


def narrowness(verdict="pass"):
    return {"verdict": verdict, "answers": {f"item_{i}": f"answer {i}" for i in range(1, 9)}, "cited_ids": ["L0001"]}


class FleetFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "fleet"
        self.root.mkdir()
        self.fleet_state = base / "fleet-state"
        self.proposals = base / "proposals"
        self.runtime = supervisor.Runtime(root=self.root, state_dir=base / "state", repo=REPO_ROOT, fleet_state=self.fleet_state,
                                          router_factory=lambda cfg: routing.Router(cfg, prices={}, env={}, token_exists=lambda p: True),
                                          pass_interval_seconds=0, refine_enabled=False)
        self.engines = {}
        self.calls = []
        for i, name in enumerate(("ws-a", "ws-b", "ws-c")):
            self.make_workspace(name, charter=hashlib.sha256(name.encode()).hexdigest(), profile="formal-mathematics" if i else "systems-software")

    def tearDown(self):
        self.tmp.cleanup()

    def make_workspace(self, name, charter, profile, faults=3):
        engine = LoopEngine.create(self.root, f"Task {name}", ["it works"], {}, task_id=name, charter_hash=charter)
        ws = engine.workspace
        supervisor.provision_workspace(self.runtime, ws)
        config = json.loads((ws / "loop-config.json").read_text())
        config["harness"]["roles"] = {"default": MINIMAL, "critic": {**MINIMAL, "model": "c"}, "verifier": {**MINIMAL, "model": "v"}}
        config["profile"] = profile
        (ws / "loop-config.json").write_text(json.dumps(config))
        supervisor.run_directive(self.runtime, ws)
        for n in range(faults):
            engine.record_process_fault({"request_id": f"fault:{n}", "signature": SIG, "source": "test", "detail": "x"})
        self.engines[name] = engine
        return engine

    def stub(self, proposer, narrow=None, adopt=None, side_effect=None):
        outputs = {"refine_proposer": proposer, "narrowness_critic": narrow, "adoption_critic": adopt}

        def session(role, prompt, directive):
            self.calls.append((role, prompt))
            if side_effect and role == "refine_proposer":
                side_effect()
            return SessionResult(session_id=f"{role}-{len(self.calls)}", model_output=outputs[role], resolved_model=f"stub-{role}",
                                 usage={"input_tokens": 1, "output_tokens": 1}, cost_usd=0.0)
        self.runtime.refine_session = session


class SignalTests(FleetFixture):
    def test_signal_and_measures_shape(self):
        sig = signal.build(self.root, self.fleet_state)
        self.assertEqual(sig["fleet"]["workspace_count"], 3)
        self.assertEqual(sig["fleet"]["distinct_charters"], 3)
        self.assertIn(SIG, sig["fleet"]["recurring_faults"])
        self.assertEqual(len(sig["agenda"]), 8)
        for w in sig["workspaces"]:
            self.assertNotIn("observation", json.dumps(w).lower()[:0] + "")
            self.assertNotIn("payload/", json.dumps(w))
            self.assertIn("consolidate_vs_disrupt", w)
            self.assertIn("distinctness", w)
        m = signal.measures(sig, self.fleet_state)
        self.assertEqual(set(signal.OBJECTIVES), set(m["objectives"]))
        self.assertEqual(set(signal.GUARDS), set(m["guards"]))
        self.assertEqual(m["excluded"], list(signal.NOT_OBJECTIVES))
        self.assertTrue(m["novelty"]["recorded_separately"] and m["acceptability"]["recorded_separately"])
        flat = json.dumps(m["objectives"]).lower()
        for banned in ("completion", "retry", "transcript_size", "spend", "pause"):
            self.assertNotIn(banned, flat)
        self.assertIn("targeted_fault_recurrence_per_100_attempts", m["objectives"])


class IndependenceTests(FleetFixture):
    def test_three_distinct_workspaces_pass_and_recycling_fails(self):
        sig = signal.build(self.root, self.fleet_state)
        ok = improvement.Refinement.from_dict({**proposal(["ws-a", "ws-b", "ws-c"]), "id": "GR0001", "scope": "global"})
        self.assertEqual(refine_global.independence(ok, sig), [])
        two = improvement.Refinement.from_dict({**proposal(["ws-a", "ws-b"]), "id": "GR0001", "scope": "global"})
        self.assertTrue(any("evidence_recycling" in p for p in refine_global.independence(two, sig)))
        self.make_workspace("ws-d", charter=hashlib.sha256(b"ws-a").hexdigest(), profile="formal-mathematics")
        sig = signal.build(self.root, self.fleet_state)
        dup = improvement.Refinement.from_dict({**proposal(["ws-a", "ws-b", "ws-d"]), "id": "GR0001", "scope": "global"})
        self.assertTrue(any("charter" in p for p in refine_global.independence(dup, sig)))
        scoped = improvement.Refinement.from_dict({**proposal(["ws-b", "ws-c"], scope_profile="formal-mathematics"), "id": "GR0001", "scope": "global"})
        self.assertEqual(refine_global.independence(scoped, sig), [])


class GlobalRunTests(FleetFixture):
    def test_three_stages_apply_fleet_state_then_majority_kill_rolls_back(self):
        output = {"proposal": proposal(["ws-a", "ws-b", "ws-c"]), "reason": "recurring signature",
                  "agenda_findings": [{"item": "R2", "supported": False, "note": "no retrieval-first sessions seen"}]}
        self.assertEqual(jsonschema_lite.validate(output, refine_global.PROPOSAL_SCHEMA), [])
        self.assertEqual(jsonschema_lite.validate(narrowness(), refine_global.NARROWNESS_SCHEMA), [])
        self.stub(output, narrowness(), review())
        report = refine_global.run(self.runtime, fleet_state=self.fleet_state)
        self.assertEqual(report["validation"], [], report)
        self.assertEqual([c[0] for c in self.calls], ["refine_proposer", "narrowness_critic", "adoption_critic"])
        self.assertIn("# Narrowness critic", self.calls[1][1].system_text)
        self.assertEqual(report["applied"]["instances"], 3)
        self.assertEqual(report["events_moved"], [])
        addendum = self.fleet_state / "prompt-addenda" / "researcher.experiment.md"
        self.assertIn("<!-- refined: GR0001 -->", addendum.read_text())
        text = supervisor.supplemental_text(self.root / "ws-a", "researcher", "experiment", self.fleet_state)
        self.assertIn("fleet: researcher.experiment.md", text)
        status = improvement.current_status(self.fleet_state)["GR0001"]
        self.assertEqual(status["status"], "provisional")
        self.assertEqual(len(status["instances"]), 3)
        self.assertTrue(all(i["baseline_discriminating"] for i in status["instances"]))

        self.assertEqual(refine_global.evaluate_pending(self.root, self.fleet_state)[0]["outcome"], "pending")
        self.engines["ws-b"].record_process_fault({"request_id": "fault:again", "signature": SIG, "source": "test", "detail": "again"})
        outcomes = refine_global.evaluate_pending(self.root, self.fleet_state)
        self.assertEqual(outcomes[0]["outcome"], "killed")
        self.assertEqual(outcomes[0]["rollback"]["status"], "rolled_back")
        self.assertFalse(addendum.exists())
        self.assertEqual(improvement.current_status(self.fleet_state)["GR0001"]["status"], "rolled_back")

    def test_narrowness_fail_short_circuits(self):
        self.stub({"proposal": proposal(["ws-a", "ws-b", "ws-c"]), "reason": "x", "agenda_findings": []}, narrowness("fail"), review())
        report = refine_global.run(self.runtime, fleet_state=self.fleet_state)
        self.assertEqual(report["rejected_by"], "narrowness")
        self.assertEqual([c[0] for c in self.calls], ["refine_proposer", "narrowness_critic"])
        self.assertFalse((self.fleet_state / "prompt-addenda").exists())

    def test_kernel_layer_becomes_a_pr_bundle_and_never_applies(self):
        kernel = proposal(["ws-a", "ws-b", "ws-c"], layer="kernel", edits=[],
                          patch="", kernel_items=["structured diagnosis.kill_test_spec on surgeon submissions"])
        self.stub({"proposal": kernel, "reason": "needs a kernel field", "agenda_findings": []}, narrowness(), review())
        report = refine_global.run(self.runtime, fleet_state=self.fleet_state, proposals_dir=self.proposals)
        self.assertEqual(report["validation"], [], report)
        bundle = Path(report["pr_bundle"]["bundle"])
        self.assertEqual(sorted(p.name for p in bundle.iterdir()),
                         ["alternatives.md", "does-not-change.md", "evidence-table.md", "freeze-rails.txt", "kill-test.json",
                          "patch.diff", "proposal.json"])
        rails = (bundle / "freeze-rails.txt").read_text()
        self.assertIn("python3 -m unittest tests.test_persistence", rails.replace(__import__("sys").executable, "python3"))
        self.assertIn("Ran ", rails)
        self.assertTrue(report["pr_bundle"]["freeze_rails_ok"], rails[-800:])
        self.assertFalse((self.fleet_state / "prompt-addenda").exists())
        self.assertEqual(improvement.current_status(self.fleet_state)["GR0001"]["status"], "pr_bundle")
        self.assertIn("ws-b", (bundle / "evidence-table.md").read_text())

    def test_a_moving_event_log_aborts_and_pauses(self):
        def move():
            self.engines["ws-c"].record_process_fault({"request_id": "fault:during", "signature": SIG, "source": "test", "detail": "d"})
        self.stub({"proposal": None, "reason": "nothing", "agenda_findings": []}, side_effect=move)
        report = refine_global.run(self.runtime, fleet_state=self.fleet_state)
        self.assertEqual(report["events_moved"], ["ws-c"])
        self.assertIn("aborted", report)
        self.assertTrue((self.fleet_state / refine_global.PAUSED_FILE).is_file())
        self.assertIn("paused", refine_global.run(self.runtime, fleet_state=self.fleet_state)["skipped"])

    def test_three_consecutive_killed_pause_the_loop(self):
        for rid in ("GR0001", "GR0002", "GR0003"):
            improvement._append_row(self.fleet_state, {"id": rid, "status": "provisional", "record": {}})
            improvement.record_outcome(self.fleet_state, rid, "killed")
        self.stub({"proposal": None, "reason": "x", "agenda_findings": []})
        report = refine_global.run(self.runtime, fleet_state=self.fleet_state)
        self.assertIn("three consecutive", report["skipped"])
        self.assertEqual(self.calls, [])

    def test_mechanical_rejection_needs_no_review_spend(self):
        self.stub({"proposal": proposal(["ws-a", "ws-b"]), "reason": "x", "agenda_findings": []}, narrowness(), review())
        report = refine_global.run(self.runtime, fleet_state=self.fleet_state)
        self.assertEqual(report["rejected_by"], "mechanical")
        self.assertEqual([c[0] for c in self.calls], ["refine_proposer"])

    def test_scheduling_and_no_merge_code_path(self):
        self.runtime.refine_every = 4
        self.assertTrue(refine_global.scheduled(self.runtime, 4, False))
        self.assertFalse(refine_global.scheduled(self.runtime, 5, False))
        self.assertTrue(refine_global.scheduled(self.runtime, 5, True))
        self.runtime.global_refine_enabled = False
        self.assertFalse(refine_global.scheduled(self.runtime, 4, True))
        for path in (REPO_ROOT / "harness").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"pr merge|merge_pull|/merge\b|--merge", text), f"merge path in {path.name}")


if __name__ == "__main__":
    unittest.main()
