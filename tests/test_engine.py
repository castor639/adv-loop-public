import copy
import hashlib
import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.adv_loop.engine import LoopEngine
from src.adv_loop.driver import drive
from src.adv_loop.cli import build_parser
from src.adv_loop.errors import IdempotencyConflict, IntegrityError, LegacyWorkspaceError, LoopError, TransitionError, ValidationError
from src.adv_loop.storage import atomic_write_text, canonical_json, fcntl, object_hash


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def strategy(seed):
    return {
        "decomposition": f"decomposition-{seed}",
        "source_class": f"source-{seed}",
        "retrieval_method": f"retrieval-{seed}",
        "reasoning_method": f"reasoning-{seed}",
        "tool": f"tool-{seed}",
        "verification_method": f"verification-{seed}",
    }


def record_in_process(workspace, payload, queue):
    try:
        LoopEngine(Path(workspace)).record_attempt(payload)
        queue.put("accepted")
    except LoopError as exc:
        queue.put(exc.code)


class Harness:
    def __init__(self, case, criteria=None, budget=None, controls=None):
        self.case = case
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.engine = LoopEngine.create(
            Path(self.temp.name),
            "Solve a difficult task",
            criteria or ["Result works"],
            budget or {},
            task_id="test-task",
            controls=controls,
        )
        self.serial = 0

    def base(self, seed, context=None, outcome="progress", strategy_value=None):
        self.serial += 1
        directive = self.engine.next_instruction()
        self.case.assertEqual(directive["action"], "attempt")
        return {
            "request_id": f"request-{self.serial}-{seed}",
            "directive_id": directive["directive_id"],
            "role": directive["role"],
            "mode": directive["mode"],
            "actor": {
                "agent_id": f"agent-{seed}",
                "model": "test-model",
                "context_id": context or f"context-{self.serial}-{seed}",
            },
            "strategy": strategy_value or strategy(seed),
            "hypothesis": f"falsifiable hypothesis {seed}",
            "action": f"executed action {seed}",
            "observation": f"observed result {seed}",
            "interpretation": f"bounded interpretation {seed}",
            "uncertainties": [f"uncertainty {seed}"],
            "next_step": f"next distinct step {seed}",
            "outcome": outcome,
            "evidence": [],
            "criterion_updates": [],
            "contradictions": [],
            "contradiction_resolutions": [],
            "decisions": [],
        }

    def plan(self, seed="plan", mode=None):
        payload = self.base(seed)
        if mode is not None:
            self.case.assertEqual(payload["mode"], mode)
        payload["plan"] = {
            "assumptions": [f"assumption {seed}"],
            "subproblems": [f"subproblem one {seed}", f"subproblem two {seed}"],
            "candidate_experiments": [f"experiment {seed}"],
            "falsification_tests": [f"falsifier {seed}"],
            "rejected_assumptions": [f"rejected {seed}"] if payload["mode"] == "fresh_replan" else [],
        }
        if payload["mode"] == "fresh_replan":
            lessons = self.engine.next_instruction().get("lessons", [])
            if lessons:
                payload["plan"]["lessons_addressed"] = [
                    {"lesson_id": lesson["id"], "response": f"replan accounts for {lesson['id']}"}
                    for lesson in lessons
                ]
        return self.engine.record_attempt(payload)

    def evidence(self, ref, supports, quality="direct", independence_key=None,
                 rank=None, checker=None, theory_base_hash=None):
        item = {
            "ref": ref,
            "kind": "test",
            "quality": quality,
            "claim": f"claim {ref}",
            "locator": f"artifact://{ref}",
            "method": f"method {ref}",
            "fingerprint": digest(ref),
            "independence_key": independence_key or f"independence-{ref}",
            "supports": supports,
        }
        if rank is not None:
            item["formalization_rank"] = rank
        if checker is not None:
            item["checker"] = checker
            item["fingerprint"] = checker["artifact_hash"]
        if theory_base_hash is not None:
            item["theory_base_hash"] = theory_base_hash
        return item

    def research(self, seed, outcome="progress", satisfy=None, extra_evidence=None,
                 strategy_value=None, evidence_kwargs=None):
        payload = self.base(seed, outcome=outcome, strategy_value=strategy_value)
        directive = self.engine.next_instruction()
        self.case.assertEqual(payload["role"], "researcher")
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = f"basin-{seed}"
        required_move = directive.get("required_move")
        if required_move:
            payload["move"] = required_move
            if required_move == "survey":
                payload["assets_registered"] = [{
                    "name": f"asset-{seed}-{self.serial}",
                    "kind": "technique",
                    "locator": f"artifact://asset-{seed}",
                }]
            elif required_move == "barrier_probe":
                payload["barrier_probe"] = {
                    "new_barrier": {
                        "name": f"barrier-{seed}-{self.serial}",
                        "statement": f"the wall observed during {seed}",
                    },
                    "pattern": "dual",
                    "approach": f"probe the wall from its dual side ({seed})",
                }
        targets = satisfy or [criterion["id"] for criterion in directive["open_criteria"][:1]] or ["C1"]
        payload["criterion_targets"] = targets
        if extra_evidence:
            payload["evidence"].extend(extra_evidence)
        if satisfy:
            for criterion_id in satisfy:
                ref = f"primary-{criterion_id}-{seed}"
                payload["evidence"].append(self.evidence(ref, [criterion_id], **(evidence_kwargs or {})))
                payload["criterion_updates"].append({
                    "id": criterion_id,
                    "status": "satisfied",
                    "evidence_refs": [ref],
                    "reason": f"directly demonstrated {criterion_id}",
                })
        return self.engine.record_attempt(payload)

    def critique(self, verdict="validated_progress", seed="critique", harness_gap=False):
        directive = self.engine.next_instruction()
        self.case.assertEqual(directive["mode"], "attempt_review")
        payload = self.base(seed, context=f"fresh-{seed}-{self.serial}")
        payload["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": verdict,
            "reasons": [f"critic reason {seed}"],
            "uncertainty": f"critic uncertainty {seed}",
        }
        if harness_gap:
            payload["assessment"]["harness_gap"] = True
        if "lens" in directive:
            payload["lens"] = directive["lens"]
            if directive["lens"] == "proves_too_much" and directive.get("controls"):
                payload["assessment"]["control_check"] = {
                    "control_id": directive["controls"][0]["id"],
                    "outcome": "not_applicable" if verdict == "validated_progress" else "rejects_control",
                    "note": f"control comparison for {seed}",
                }
        if directive.get("multi_cause_required") and verdict != "validated_progress":
            payload["assessment"]["candidate_causes"] = [
                {"cause": f"cause A for {seed}", "discriminating_test": f"test A for {seed}"},
                {"cause": f"cause B for {seed}", "discriminating_test": f"test B for {seed}"},
            ]
        return self.engine.record_attempt(payload)

    def ideation(self, seed="ideation", explore=False):
        directive = self.engine.next_instruction(explore=explore)
        self.case.assertEqual(directive["mode"], "ideation")
        self.serial += 1
        seeds = []
        for index in range(3):
            item = {
                "claim": f"speculative claim {seed}-{self.serial}-{index}",
                "basin": f"idea-basin-{seed}-{self.serial}-{index}",
                "first_unjustified_step": f"unjustified step {index}",
                "kill_test": f"kill test {index}",
            }
            if directive["ideation_kind"] == "evolve":
                item["parents"] = [directive["top_candidates"][0]["id"]]
            seeds.append(item)
        payload = {
            "request_id": f"request-{self.serial}-{seed}",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {
                "agent_id": f"agent-{seed}",
                "model": "test-model",
                "context_id": f"minimal-context-{self.serial}-{seed}",
            },
            "context_scope": "minimal",
            "ideation_kind": directive["ideation_kind"],
            "seeds": seeds,
            "decisions": [],
        }
        return self.engine.record_attempt(payload)

    def triage(self, seed="triage", promote=()):
        directive = self.engine.next_instruction()
        self.case.assertEqual(directive["mode"], "triage")
        self.serial += 1
        verdicts = []
        for item in directive["seeds"]:
            index = item["seed_index"]
            verdicts.append({
                "seed_index": index,
                "decision": "promoted" if index in promote else "killed",
                "reason": f"triage reason {seed}-{index}",
            })
        triage_body = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdicts": verdicts,
        }
        if directive.get("open_candidates") and promote:
            triage_body["comparisons"] = [
                {
                    "seed_index": index,
                    "candidate_id": directive["open_candidates"][0]["id"],
                    "winner": "seed",
                    "rationale": f"debate rationale {seed}-{index}",
                }
                for index in promote
            ]
        payload = {
            "request_id": f"request-{self.serial}-{seed}",
            "directive_id": directive["directive_id"],
            "role": "critic",
            "mode": "triage",
            "actor": {
                "agent_id": f"agent-{seed}",
                "model": "test-model",
                "context_id": f"triage-context-{self.serial}-{seed}",
            },
            "triage": triage_body,
            "decisions": [],
        }
        return self.engine.record_attempt(payload)

    def escalation(self, seed="escalation", stop_before_blocker=False):
        directive = self.engine.next_instruction()
        if directive["role"] == "researcher" or directive["role"] == "verifier":
            return False
        if stop_before_blocker and directive["mode"] == "blocker_audit":
            return False
        if directive["role"] == "explorer":
            self.ideation(f"{seed}-ideation")
            return True
        if directive["mode"] == "triage":
            self.triage(f"{seed}-triage")
            return True
        if directive["role"] == "planner":
            self.plan(f"{seed}-{directive['mode']}", directive["mode"])
        else:
            payload = self.base(f"{seed}-{directive['mode']}")
            if directive["mode"] == "contradiction_search":
                payload["contradiction_search"] = {
                    "assumptions_checked": ["the main assumption"],
                    "disconfirming_queries": ["a contradiction query"],
                    "conclusion": "the assumption remains plausible but unproved",
                }
            elif directive["mode"] == "blocker_audit":
                blocker_state = self.engine.load()
                failed_ids = [
                    attempt["id"]
                    for attempt in blocker_state["attempts"]
                    if attempt["role"] == "researcher"
                ][:3]
                direct_id = next(
                    item["id"] for item in blocker_state["evidence"].values() if item["quality"] == "direct"
                )
                payload["blocker_audit"] = {
                    "dependency": "private external API",
                    "dependency_evidence_ids": [direct_id],
                    "safe_alternative_attempt_ids": failed_ids,
                    "unsafe_alternatives_rejected": ["credential theft"],
                    "exhaustion_reason": "all authorized interfaces rejected the request",
                    "human_action": "grant read access",
                    "safe_alternatives_exhausted": True,
                }
            self.engine.record_attempt(payload)
        return True

    def drain_escalations(self, seed="drain"):
        while self.escalation(seed):
            pass

    def fail_cycle(self, seed, dependency_evidence=False):
        while self.engine.next_instruction().get("role") != "researcher":
            self.escalation(f"before-{seed}")
        extra = [self.evidence(f"dependency-{seed}", ["C1"])] if dependency_evidence else None
        self.research(seed, outcome="failed", extra_evidence=extra)
        return self.critique("no_progress", f"critic-{seed}")

    def verify(self, seed="verify", verdicts=None, evidence_kwargs=None):
        directive = self.engine.next_instruction()
        self.case.assertEqual(directive["mode"], "independent_verification")
        payload = self.base(seed, context=f"independent-context-{seed}")
        payload["verification_results"] = []
        for criterion in directive["criteria_to_verify"]:
            criterion_id = criterion["id"]
            ref = f"verification-{criterion_id}-{seed}"
            payload["evidence"].append(
                self.evidence(ref, [criterion_id],
                              independence_key=f"independent-check-{criterion_id}-{seed}",
                              **(evidence_kwargs or {}))
            )
            payload["verification_results"].append({
                "criterion_id": criterion_id,
                "verdict": (verdicts or {}).get(criterion_id, "pass"),
                "evidence_refs": [ref],
                "method": f"independent method {criterion_id}",
                "observation": f"independent observation {criterion_id}",
            })
        return self.engine.record_attempt(payload)

    def synthesize(self, seed="synthesize", disclosed_override=None):
        payload = self.base(seed)
        self.case.assertEqual(payload["role"], "synthesizer")
        state = self.engine.load()
        results = []
        for criterion in state["criteria"]:
            results.append({
                "criterion_id": criterion["id"],
                "conclusion": f"criterion {criterion['id']} passed",
                "primary_evidence_ids": criterion["evidence_ids"],
                "verification_evidence_ids": criterion["verification"]["evidence_ids"],
            })
        first = state["criteria"][0]
        unresolved = [
            item["id"]
            for item in state["contradictions"]
            if item["status"] == "open" and item["severity"] == "noncritical"
        ]
        payload["report"] = {
            "summary": "All criteria passed direct and independent checks.",
            "criterion_results": results,
            "facts": [{"claim": "The tested outcome works.", "evidence_ids": first["evidence_ids"]}],
            "inferences": [],
            "uncertainties": ["External conditions may change."],
            "limitations": ["Process integrity cannot establish truth beyond the recorded observations."],
            "unresolved_noncritical_contradiction_ids": (
                unresolved if disclosed_override is None else disclosed_override
            ),
            "next_actions": ["Monitor for changed inputs."],
        }
        return self.engine.record_attempt(payload)

    def record_fault(self, label="fault", times=1):
        signature = digest(f"signature-{label}")
        for _ in range(times):
            self.serial += 1
            self.engine.record_process_fault({
                "request_id": f"fault-{label}-{self.serial}",
                "signature": signature,
                "source": "test-supervisor",
                "detail": f"repeat {self.serial} of condition {label}",
            })
        return signature

    def surgeon(self, classification="research_failure", delta=None, seed="surgeon"):
        directive = self.engine.next_instruction()
        self.case.assertEqual(directive["mode"], "overlay_diagnosis")
        self.serial += 1
        diagnosis = {
            "raw_detail": f"repeating condition {seed}",
            "classification": classification,
            "kill_test": f"kill test {seed}",
            "next_experiment": f"next experiment {seed}",
        }
        if directive["demand"]["kind"] == "fault":
            diagnosis["fault_signature"] = directive["demand"]["signature"]
        if classification == "harness_gap":
            diagnosis["proposed_delta"] = delta
            diagnosis["delta_fingerprint"] = object_hash(delta)
        payload = {
            "request_id": f"request-{self.serial}-{seed}",
            "directive_id": directive["directive_id"],
            "role": "surgeon",
            "mode": "overlay_diagnosis",
            "actor": {
                "agent_id": f"agent-{seed}",
                "model": "test-model",
                "context_id": f"surgeon-context-{self.serial}-{seed}",
            },
            "diagnosis": diagnosis,
            "decisions": [],
        }
        return self.engine.record_attempt(payload)

    def overlay_review(self, verdict="adopt", narrowed=None, seed="overlay-review"):
        directive = self.engine.next_instruction()
        self.case.assertEqual(directive["mode"], "overlay_review")
        self.serial += 1
        review = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": verdict,
            "reasons": [f"review reason {seed}"],
            "uncertainty": f"review uncertainty {seed}",
        }
        if narrowed is not None:
            review["narrowed_delta"] = narrowed
        payload = {
            "request_id": f"request-{self.serial}-{seed}",
            "directive_id": directive["directive_id"],
            "role": "critic",
            "mode": "overlay_review",
            "actor": {
                "agent_id": f"agent-{seed}",
                "model": "test-model",
                "context_id": f"review-context-{self.serial}-{seed}",
            },
            "overlay_review": review,
            "decisions": [],
        }
        return self.engine.record_attempt(payload)

    def ready_for_completion(self):
        self.plan()
        criterion_ids = [criterion["id"] for criterion in self.engine.load()["criteria"]]
        self.research("success", satisfy=criterion_ids)
        self.critique("validated_progress")
        self.verify()
        self.synthesize()


class CreationAndIntegrityTests(unittest.TestCase):
    def test_create_materializes_complete_workspace_and_valid_chain(self):
        harness = Harness(self, ["One", "Two"])
        workspace = harness.engine.workspace
        for name in ("events.jsonl", "state.json", "task.md", "attempts.jsonl", "evidence.jsonl", "decision-log.md", "report.md"):
            self.assertTrue((workspace / name).exists(), name)
        state = harness.engine.load()
        self.assertEqual(state["protocol_version"], 2)
        self.assertEqual(state["phase"], "initial_plan")
        self.assertTrue(harness.engine.audit()["ok"])

    def test_tampered_projection_is_detected_and_rebuilt(self):
        harness = Harness(self)
        harness.plan()
        (harness.engine.workspace / "state.json").write_text("{}\n", encoding="utf-8")
        audit = harness.engine.audit()
        self.assertFalse(audit["ok"])
        self.assertIn("state.json", audit["projection_mismatches"])
        repaired = harness.engine.recover()
        self.assertTrue(repaired["ok"])
        self.assertTrue(repaired["repaired"])

    def test_tampered_event_is_refused_not_silently_repaired(self):
        harness = Harness(self)
        path = harness.engine.workspace / "events.jsonl"
        rows = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(rows[0])
        event["payload"]["task"] = "forged task"
        path.write_text(canonical_json(event) + "\n", encoding="utf-8")
        audit = harness.engine.audit()
        self.assertFalse(audit["event_log_valid"])
        with self.assertRaises(IntegrityError):
            harness.engine.load()

    def test_hash_valid_but_out_of_order_event_fails_semantic_replay(self):
        harness = Harness(self)
        path = harness.engine.workspace / "events.jsonl"
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        directive = harness.engine.next_instruction()
        forged_attempt = {
            "id": "A000001",
            "request_id": "forged-transition",
            "directive_id": directive["directive_id"],
            "role": "researcher",
            "mode": "experiment",
        }
        unsigned = {
            "seq": 2,
            "at": "2026-08-11T00:00:00+00:00",
            "type": "attempt_recorded",
            "request_id": "forged-transition",
            "payload": {"submission_hash": digest("forged"), "attempt": forged_attempt},
            "prev_hash": first["hash"],
        }
        event = {**unsigned, "hash": object_hash(unsigned)}
        path.write_text(canonical_json(first) + "\n" + canonical_json(event) + "\n", encoding="utf-8")
        audit = harness.engine.audit()
        self.assertFalse(audit["ok"])
        self.assertIn("scheduled transition", audit["issues"][0]["message"])

    def test_pending_journal_recovers_a_torn_append(self):
        harness = Harness(self)
        store = harness.engine.store
        with store.lock():
            events = store.read_unlocked()
            unsigned = {
                "seq": 2,
                "at": "2026-08-11T00:00:00+00:00",
                "type": "task_unsafe",
                "request_id": "simulated-crash",
                "payload": {
                    "decision_hash": digest("decision"),
                    "boundary": "authorization",
                    "risk": "unauthorized access",
                    "halted_action": "stopped request",
                    "evidence_ids": [],
                },
                "prev_hash": events[-1]["hash"],
            }
            event = {**unsigned, "hash": object_hash(unsigned)}
            atomic_write_text(store.pending_path, canonical_json(event) + "\n")
            with store.events_path.open("ab") as handle:
                handle.write((canonical_json(event) + "\n").encode("utf-8")[:37])
        audit = harness.engine.recover()
        self.assertTrue(audit["ok"])
        self.assertEqual(harness.engine.load()["status"], "unsafe")
        self.assertTrue(any((harness.engine.workspace / ".recovery").iterdir()))

    def test_legacy_snapshot_is_never_assumed_to_have_valid_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "legacy"
            workspace.mkdir()
            (workspace / "state.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(LegacyWorkspaceError):
                LoopEngine(workspace).load()

    def test_legacy_migration_preserves_source_and_reopens_proof_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            source.mkdir()
            legacy = {
                "task_id": "old-task", "status": "completed", "task": "Legacy result",
                "criteria": [{"id": "C1", "text": "Old claim", "status": "satisfied", "evidence_ids": ["E1"]}],
            }
            original = json.dumps(legacy, indent=2) + "\n"
            (source / "state.json").write_text(original, encoding="utf-8")
            (source / "attempts.jsonl").write_text('{"id":"A1"}\n', encoding="utf-8")
            engine = LoopEngine.migrate_legacy(source, root, task_id="migrated-task")
            state = engine.load()
            self.assertEqual((source / "state.json").read_text(encoding="utf-8"), original)
            self.assertEqual(state["status"], "active")
            self.assertEqual(state["criteria"][0]["status"], "open")
            self.assertIn("criteria reopened", state["legacy_import"]["disposition"])
            self.assertEqual(engine.next_instruction()["mode"], "initial_plan")
            self.assertTrue(engine.audit()["ok"])


class TransitionPolicyTests(unittest.TestCase):
    def test_drive_cli_parses_supervisor_options_before_adapter_separator(self):
        args = build_parser().parse_args([
            "drive", "/tmp/workspace", "--max-cycles", "1", "--adapter-retries", "2",
            "--timeout", "30", "--", "python3", "adapter.py", "--model", "test",
        ])
        self.assertEqual(args.max_cycles, 1)
        self.assertEqual(args.adapter_retries, 2)
        self.assertEqual(args.timeout, 30.0)
        self.assertEqual(args.adapter, ["python3", "adapter.py", "--model", "test"])

    def test_driver_feeds_validation_feedback_and_recovers_without_losing_state(self):
        harness = Harness(self)
        calls = []

        def adapter(envelope):
            calls.append(envelope)
            directive = envelope["directive"]
            payload = {
                "request_id": f"driver-request-{len(calls)}",
                "directive_id": directive["directive_id"],
                "role": directive["role"],
                "mode": directive["mode"],
                "actor": {"agent_id": "driver-agent", "model": "test-model", "context_id": f"driver-context-{len(calls)}"},
                "strategy": strategy(f"driver-{len(calls)}"),
                "hypothesis": "the plan can isolate the task",
                "action": "constructed the task plan",
                "observation": "criteria can be decomposed",
                "interpretation": "an experiment queue is feasible",
                "uncertainties": ["tools are not yet tested"],
                "next_step": "run the first experiment",
                "outcome": "progress",
                "evidence": [], "criterion_updates": [], "contradictions": [],
                "contradiction_resolutions": [], "decisions": [],
            }
            if len(calls) > 1:
                payload["plan"] = {
                    "assumptions": ["inputs exist"], "subproblems": ["test input"],
                    "candidate_experiments": ["inspect input"], "falsification_tests": ["input is absent"],
                    "rejected_assumptions": [],
                }
            return payload

        result = drive(harness.engine, adapter, max_cycles=1, adapter_retries=2)
        self.assertEqual(result["driver_status"], "paused")
        self.assertEqual(result["accepted_attempts"], 1)
        self.assertEqual(result["adapter_rejections"], 1)
        self.assertEqual(len(calls[1]["rejections"]), 1)
        self.assertEqual(harness.engine.load()["attempt_count"], 1)

    def test_only_scheduled_role_and_mode_can_record(self):
        harness = Harness(self)
        payload = harness.base("wrong-role")
        payload["role"] = "researcher"
        payload["mode"] = "experiment"
        with self.assertRaises(TransitionError):
            harness.engine.record_attempt(payload)

    def test_stale_directive_loses_after_another_writer_advances_state(self):
        harness = Harness(self)
        stale = harness.base("stale")
        winner = copy.deepcopy(stale)
        winner["request_id"] = "winner-request"
        winner["plan"] = {
            "assumptions": ["a"], "subproblems": ["s"], "candidate_experiments": ["e"],
            "falsification_tests": ["f"], "rejected_assumptions": [],
        }
        harness.engine.record_attempt(winner)
        stale["plan"] = winner["plan"]
        with self.assertRaises(TransitionError):
            harness.engine.record_attempt(stale)

    def test_concurrent_writers_are_serialized_and_one_stale_write_fails(self):
        harness = Harness(self)
        first = harness.base("concurrent-a")
        first["plan"] = {
            "assumptions": ["a"], "subproblems": ["s"], "candidate_experiments": ["e"],
            "falsification_tests": ["f"], "rejected_assumptions": [],
        }
        second = copy.deepcopy(first)
        second["request_id"] = "concurrent-second"
        second["actor"]["context_id"] = "concurrent-context-two"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(harness.engine.record_attempt, item) for item in (first, second)]
        successes = 0
        failures = 0
        for future in futures:
            try:
                future.result()
                successes += 1
            except TransitionError:
                failures += 1
        self.assertEqual((successes, failures), (1, 1))
        self.assertEqual(harness.engine.load()["attempt_count"], 1)
        self.assertTrue(harness.engine.audit()["ok"])

    @unittest.skipIf(fcntl is None, "cross-process flock is a POSIX invariant")
    def test_separate_processes_cannot_commit_the_same_directive_twice(self):
        harness = Harness(self)
        first = harness.base("process-a")
        first["plan"] = {
            "assumptions": ["a"], "subproblems": ["s"], "candidate_experiments": ["e"],
            "falsification_tests": ["f"], "rejected_assumptions": [],
        }
        second = copy.deepcopy(first)
        second["request_id"] = "process-second"
        second["actor"]["context_id"] = "process-context-two"
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        processes = [
            context.Process(
                target=record_in_process,
                args=(str(harness.engine.workspace), payload, queue),
            )
            for payload in (first, second)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        outcomes = sorted(queue.get(timeout=2) for _ in processes)
        self.assertEqual(outcomes, ["accepted", "transition_error"])
        self.assertEqual(harness.engine.load()["attempt_count"], 1)
        self.assertTrue(harness.engine.audit()["ok"])

    def test_research_must_follow_active_plan(self):
        harness = Harness(self)
        harness.plan()
        payload = harness.base("research")
        payload["plan_id"] = "A999999"
        payload["basin"] = "basin-research"
        payload["criterion_targets"] = ["C1"]
        with self.assertRaises(TransitionError):
            harness.engine.record_attempt(payload)

    def test_every_research_attempt_requires_a_fresh_critic_context(self):
        harness = Harness(self)
        harness.plan()
        harness.research("research", outcome="failed")
        directive = harness.engine.next_instruction()
        target = harness.engine.load()["attempts"][-1]
        payload = harness.base("critic", context=target["actor"]["context_id"])
        payload["lens"] = directive.get("lens")
        payload["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "no_progress",
            "reasons": ["failed"],
            "uncertainty": "none",
        }
        with self.assertRaises(ValidationError):
            harness.engine.record_attempt(payload)

    def test_failure_is_counted_only_after_critic_rejects_progress(self):
        harness = Harness(self)
        harness.plan()
        state = harness.research("failed", outcome="failed")
        self.assertEqual(state["failure_streak"], 0)
        state = harness.critique("no_progress")
        self.assertEqual(state["failure_streak"], 1)

    def test_empty_attempt_cannot_be_relabelled_as_validated_progress(self):
        harness = Harness(self)
        harness.plan()
        harness.research("empty-progress", outcome="progress")
        directive = harness.engine.next_instruction()
        payload = harness.base("false-critic")
        payload["lens"] = directive.get("lens")
        payload["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "validated_progress",
            "reasons": ["the researcher said progress"],
            "uncertainty": "no proof was attached",
        }
        with self.assertRaisesRegex(ValidationError, "evidence-bearing"):
            harness.engine.record_attempt(payload)

    def test_global_strategy_reuse_is_rejected(self):
        harness = Harness(self)
        harness.plan()
        repeated = strategy("same")
        harness.research("first", outcome="failed", strategy_value=repeated)
        harness.critique("no_progress")
        payload = harness.base("second", strategy_value=repeated)
        payload["plan_id"] = harness.engine.next_instruction()["active_plan_id"]
        payload["basin"] = "basin-second"
        payload["criterion_targets"] = ["C1"]
        with self.assertRaisesRegex(ValidationError, "already attempted"):
            harness.engine.record_attempt(payload)

    def test_cosmetic_strategy_changes_do_not_bypass_global_reuse(self):
        harness = Harness(self)
        harness.plan()
        first = strategy("same")
        harness.research("first", outcome="failed", strategy_value=first)
        harness.critique("no_progress")
        cosmetic = {key: f"  {value.upper()}!!! " for key, value in first.items()}
        payload = harness.base("cosmetic", strategy_value=cosmetic)
        payload["plan_id"] = harness.engine.next_instruction()["active_plan_id"]
        payload["basin"] = "basin-cosmetic"
        payload["criterion_targets"] = ["C1"]
        with self.assertRaisesRegex(ValidationError, "already attempted"):
            harness.engine.record_attempt(payload)

    def test_two_failures_require_two_dimension_changes_from_recent_failures(self):
        harness = Harness(self)
        harness.plan()
        first = strategy("base")
        harness.research("first", outcome="failed", strategy_value=first)
        harness.critique("no_progress")
        second = dict(first)
        second["tool"] = "tool-second"
        harness.research("second", outcome="failed", strategy_value=second)
        harness.critique("no_progress")
        harness.drain_escalations("two-failures")
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["required_strategy_dimension_changes"], 2)
        too_similar = dict(second)
        too_similar["tool"] = "tool-third"
        payload = harness.base("third", strategy_value=too_similar)
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = "basin-third"
        payload["criterion_targets"] = ["C1"]
        with self.assertRaisesRegex(ValidationError, "diversify"):
            harness.engine.record_attempt(payload)

    def test_escalation_ladder_is_mandatory(self):
        harness = Harness(self)
        harness.plan()
        for index in range(1, 4):
            harness.fail_cycle(f"failure-{index}")
        self.assertEqual(harness.engine.next_instruction()["mode"], "contradiction_search")
        harness.escalation("contradiction")
        harness.fail_cycle("failure-4")
        self.assertEqual(harness.engine.next_instruction()["mode"], "decompose")
        harness.escalation("decompose")
        harness.fail_cycle("failure-5")
        self.assertEqual(harness.engine.next_instruction()["mode"], "fresh_replan")

    def test_attempt_contract_rejects_missing_strategy_dimensions_and_unknown_fields(self):
        harness = Harness(self)
        payload = harness.base("invalid")
        payload["strategy"] = {"tool": "only-one-field"}
        payload["plan"] = {}
        with self.assertRaisesRegex(ValidationError, "protocol dimensions"):
            harness.engine.record_attempt(payload)
        payload = harness.base("unknown")
        payload["invented_field"] = True
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            harness.engine.record_attempt(payload)


class EvidenceAndCompletionTests(unittest.TestCase):
    def test_satisfied_criterion_requires_direct_explicit_support(self):
        harness = Harness(self)
        harness.plan()
        payload = harness.base("indirect")
        directive = harness.engine.next_instruction()
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = "basin-indirect"
        payload["criterion_targets"] = ["C1"]
        payload["evidence"] = [harness.evidence("weak", ["C1"], quality="indirect")]
        payload["criterion_updates"] = [{
            "id": "C1", "status": "satisfied", "evidence_refs": ["weak"], "reason": "claimed",
        }]
        with self.assertRaisesRegex(ValidationError, "direct evidence"):
            harness.engine.record_attempt(payload)

    def test_evidence_fingerprint_and_references_are_strict(self):
        harness = Harness(self)
        harness.plan()
        payload = harness.base("bad-evidence")
        directive = harness.engine.next_instruction()
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = "basin-bad-evidence"
        payload["criterion_targets"] = ["C1"]
        item = harness.evidence("bad", ["C1"])
        item["fingerprint"] = "not-a-hash"
        payload["evidence"] = [item]
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            harness.engine.record_attempt(payload)

    def test_verifier_must_cover_every_criterion(self):
        harness = Harness(self, ["One", "Two"])
        harness.plan()
        harness.research("success", satisfy=["C1", "C2"])
        harness.critique()
        payload = harness.base("partial-verifier")
        payload["verification_results"] = []
        with self.assertRaisesRegex(ValidationError, "every criterion"):
            harness.engine.record_attempt(payload)

    def test_verifier_rejects_same_context_and_reused_evidence_provenance(self):
        harness = Harness(self)
        harness.plan()
        harness.research("success", satisfy=["C1"])
        harness.critique()
        state = harness.engine.load()
        primary = state["evidence"][state["criteria"][0]["evidence_ids"][0]]
        payload = harness.base("dependent-verifier", context=primary["actor"]["context_id"])
        reused = harness.evidence("new-ref", ["C1"], independence_key=primary["independence_key"])
        payload["evidence"] = [reused]
        payload["verification_results"] = [{
            "criterion_id": "C1", "verdict": "pass", "evidence_refs": ["new-ref"],
            "method": "same check", "observation": "same observation",
        }]
        with self.assertRaisesRegex(ValidationError, "context must be independent"):
            harness.engine.record_attempt(payload)

    def test_critical_contradiction_blocks_verification_until_new_evidence_resolves_it(self):
        harness = Harness(self)
        harness.plan()
        payload = harness.base("contradicted")
        directive = harness.engine.next_instruction()
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = "basin-contradicted"
        payload["criterion_targets"] = ["C1"]
        payload["evidence"] = [
            harness.evidence("primary", ["C1"]),
            harness.evidence("counter", ["X1"]),
        ]
        payload["criterion_updates"] = [{
            "id": "C1", "status": "satisfied", "evidence_refs": ["primary"], "reason": "primary test passed",
        }]
        payload["contradictions"] = [{
            "id": "X1", "claim": "counterexample may invalidate the result", "severity": "critical", "evidence_refs": ["counter"],
        }]
        harness.engine.record_attempt(payload)
        harness.critique()
        self.assertEqual(harness.engine.next_instruction()["role"], "researcher")

        resolution = harness.base("resolution")
        directive = harness.engine.next_instruction()
        resolution["plan_id"] = directive["active_plan_id"]
        resolution["basin"] = "basin-resolution"
        resolution["criterion_targets"] = ["C1"]
        resolution["evidence"] = [harness.evidence("resolution-proof", ["X1"])]
        resolution["contradiction_resolutions"] = [{
            "id": "X1", "resolution": "controlled reproduction disproved the counterexample", "evidence_refs": ["resolution-proof"],
        }]
        harness.engine.record_attempt(resolution)
        harness.critique()
        self.assertEqual(harness.engine.next_instruction()["role"], "verifier")

    def test_noncritical_contradiction_must_be_disclosed_in_report(self):
        harness = Harness(self)
        harness.plan()
        payload = harness.base("noncritical")
        directive = harness.engine.next_instruction()
        payload["plan_id"] = directive["active_plan_id"]
        payload["basin"] = "basin-noncritical"
        payload["criterion_targets"] = ["C1"]
        payload["evidence"] = [
            harness.evidence("primary-noncritical", ["C1"]),
            harness.evidence("caveat", ["X-NONCRITICAL"]),
        ]
        payload["criterion_updates"] = [{
            "id": "C1", "status": "satisfied", "evidence_refs": ["primary-noncritical"],
            "reason": "primary behavior is directly established",
        }]
        payload["contradictions"] = [{
            "id": "X-NONCRITICAL", "claim": "an edge case remains uncertain", "severity": "noncritical",
            "evidence_refs": ["caveat"],
        }]
        harness.engine.record_attempt(payload)
        harness.critique()
        harness.verify()
        with self.assertRaisesRegex(ValidationError, "exact set"):
            harness.synthesize("omitted-caveat", disclosed_override=[])
        state = harness.synthesize("disclosed-caveat")
        self.assertEqual(state["report"]["structured"]["unresolved_noncritical_contradiction_ids"], ["X-NONCRITICAL"])

    def test_completion_requires_verification_and_structured_report(self):
        harness = Harness(self)
        harness.plan()
        harness.research("success", satisfy=["C1"])
        harness.critique()
        self.assertFalse(harness.engine.completion_ready())
        harness.verify()
        self.assertFalse(harness.engine.completion_ready())
        harness.synthesize()
        self.assertTrue(harness.engine.completion_ready())
        state = harness.engine.finalize()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(harness.engine.next_instruction()["action"], "stop")
        report = (harness.engine.workspace / "report.md").read_text(encoding="utf-8")
        for heading in ("## Facts", "## Inferences", "## Uncertainties", "## Limitations"):
            self.assertIn(heading, report)

    def test_tampering_after_report_prevents_completion_until_recovery(self):
        harness = Harness(self)
        harness.ready_for_completion()
        (harness.engine.workspace / "report.md").write_text("fake success\n", encoding="utf-8")
        with self.assertRaisesRegex(TransitionError, "Completion gate failed"):
            harness.engine.finalize()
        harness.engine.recover()
        self.assertEqual(harness.engine.finalize()["status"], "completed")

    def test_failed_verification_reopens_criterion(self):
        harness = Harness(self)
        harness.plan()
        harness.research("success", satisfy=["C1"])
        harness.critique()
        state = harness.verify(verdicts={"C1": "fail"})
        self.assertFalse(state["verification"]["passed"])
        self.assertEqual(state["criteria"][0]["status"], "failed_verification")
        self.assertEqual(harness.engine.next_instruction()["role"], "researcher")

    def test_same_request_is_idempotent_but_conflicting_reuse_is_rejected(self):
        harness = Harness(self)
        payload = harness.base("idempotent")
        payload["plan"] = {
            "assumptions": ["a"], "subproblems": ["s"], "candidate_experiments": ["e"],
            "falsification_tests": ["f"], "rejected_assumptions": [],
        }
        first = harness.engine.record_attempt(payload)
        second = harness.engine.record_attempt(payload)
        self.assertEqual(first["attempt_count"], second["attempt_count"])
        conflict = copy.deepcopy(payload)
        conflict["observation"] = "different content"
        with self.assertRaises(IdempotencyConflict):
            harness.engine.record_attempt(conflict)


class TerminalGateTests(unittest.TestCase):
    def test_hard_attempt_budget_auto_transitions_without_claiming_completion(self):
        harness = Harness(self, budget={"max_attempts": 1})
        state = harness.plan()
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertNotEqual(state["status"], "completed")
        self.assertEqual(harness.engine.next_instruction()["action"], "stop")

    def test_expired_deadline_is_a_hard_terminal_gate(self):
        harness = Harness(self, budget={"deadline": "2000-01-01T00:00:00Z"})
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["action"], "stop")
        self.assertEqual(harness.engine.load()["status"], "budget_exhausted")

    def test_unsafe_can_stop_immediately_at_an_authorization_boundary(self):
        harness = Harness(self)
        stale_attempt = harness.base("would-have-run")
        stale_attempt["plan"] = {
            "assumptions": ["a"], "subproblems": ["s"], "candidate_experiments": ["e"],
            "falsification_tests": ["f"], "rejected_assumptions": [],
        }
        state = harness.engine.mark_unsafe({
            "request_id": "unsafe-decision",
            "boundary": "authorization",
            "risk": "would access data without permission",
            "halted_action": "credential extraction",
            "evidence_ids": [],
        })
        self.assertEqual(state["status"], "unsafe")
        with self.assertRaises(TransitionError):
            harness.engine.record_attempt(stale_attempt)

    def test_blocked_cannot_be_used_before_the_full_audit_ladder(self):
        harness = Harness(self)
        with self.assertRaises(TransitionError):
            harness.engine.mark_blocked({
                "request_id": "premature-block", "dependency": "API", "attempt_ids": ["A1", "A2", "A3"],
                "evidence_ids": [], "alternatives_exhausted": "none", "human_action": "grant access",
            })

    def test_blocked_requires_seven_failures_audit_and_three_diverse_attempts(self):
        harness = Harness(self)
        harness.plan()
        for index in range(1, 8):
            harness.fail_cycle(f"blocked-{index}", dependency_evidence=index == 1)
        self.assertEqual(harness.engine.next_instruction()["mode"], "blocker_audit")
        harness.escalation("final-blocker")
        state = harness.engine.load()
        research_ids = [attempt["id"] for attempt in state["attempts"] if attempt["role"] == "researcher"][:3]
        direct_evidence = next(item["id"] for item in state["evidence"].values() if item["quality"] == "direct")
        decision = {
            "request_id": "proved-block", "dependency": "private external API",
            "attempt_ids": research_ids, "evidence_ids": [direct_evidence],
            "alternatives_exhausted": "direct API, documented fallback, and local cache were all unavailable",
            "human_action": "grant read access",
            "retest": {
                "premise": "the private external API rejects every authorized credential",
                "probe": "retry the documented read endpoint with the granted credential",
                "recheck_after": "2026-09-01T00:00:00Z",
            },
        }
        harness.research("post-audit-check", outcome="failed")
        with self.assertRaisesRegex(TransitionError, "pending critic"):
            harness.engine.mark_blocked(decision)
        harness.critique("no_progress", "post-audit-critic")
        # At streak 8 the cyclic ladder owes barrier_probe; conceding while the
        # loop still has demands is refused.
        with self.assertRaisesRegex(TransitionError, "still owes a demand"):
            harness.engine.mark_blocked(decision)
        harness.fail_cycle("blocked-probe")     # barrier_probe rung, then streak 9
        harness.fail_cycle("blocked-combine")   # combine rung (falls back to survey), streak 10
        harness.drain_escalations("cycle-two")  # ideation@1 + triage
        blocked = harness.engine.mark_blocked(decision)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["terminal"]["human_action"], "grant read access")
        self.assertEqual(
            blocked["terminal"]["retest"]["premise"],
            "the private external API rejects every authorized credential",
        )


class EncouragementTest(unittest.TestCase):
    def test_directive_carries_mode_specific_encouragement(self):
        harness = Harness(self)
        directive = harness.engine.next_instruction()
        self.assertEqual(directive["mode"], "initial_plan")
        text = " ".join(directive["encouragement"])
        self.assertIn("keep going", text.lower())
        self.assertIn("genuinely testable", text)

    def test_streak_encouragement_appears_only_after_repeated_failure(self):
        harness = Harness(self)
        harness.plan()
        self.assertFalse(
            any("failures are" in line for line in harness.engine.next_instruction()["encouragement"])
        )
        harness.fail_cycle("streak-1")
        harness.fail_cycle("streak-2")
        harness.drain_escalations("streak")
        directive = harness.engine.next_instruction()
        self.assertGreaterEqual(directive["failure_streak"], 2)
        self.assertTrue(any("failures are" in line for line in directive["encouragement"]))

    def test_encouragement_always_anchors_honest_termination(self):
        from src.adv_loop.policy import _ENCOURAGEMENT_BY_MODE, encouragement_for

        for mode in _ENCOURAGEMENT_BY_MODE:
            for streak in (0, 5):
                lines = encouragement_for(mode, streak)
                self.assertTrue(
                    any("unearned completion is not" in line for line in lines),
                    f"missing honesty anchor for {mode} at streak {streak}",
                )

    def test_encouragement_does_not_change_directive_id(self):
        harness = Harness(self)
        before = harness.engine.next_instruction()["directive_id"]
        import src.adv_loop.policy as policy

        original = policy._ENCOURAGEMENT_BASE
        try:
            policy._ENCOURAGEMENT_BASE = "Totally different wording."
            after = harness.engine.next_instruction()
            self.assertEqual(after["directive_id"], before)
            self.assertIn("Totally different wording.", after["encouragement"])
        finally:
            policy._ENCOURAGEMENT_BASE = original


if __name__ == "__main__":
    unittest.main()
