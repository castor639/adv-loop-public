from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .errors import (
    IdempotencyConflict,
    IntegrityError,
    LegacyWorkspaceError,
    TransitionError,
    ValidationError,
)
from .policy import (
    BARRIER_PROBE_PATTERNS,
    BASIN_CLOSURE_FAILURES,
    BASIN_STALL_CLOSURE,
    CHECKED_RANK_FLOOR,
    ELO_INITIAL,
    ELO_K,
    EVOLVE_TOP_K,
    FORMALIZATION_RANKS,
    HUMAN_INPUT_CLASSIFICATIONS,
    KNOWN_MODES,
    OVERLAY_OPS,
    OVERLAY_REVIEW_VERDICTS,
    OVERLAY_SCHEMA_VERSION,
    POLICY_VERSION,
    RESEARCH_MOVES,
    SEED_MAX,
    SEED_MIN,
    STRATEGY_DIMENSIONS,
    SUPPORTED_POLICY_VERSIONS,
    SURGEON_CLASSIFICATIONS,
    SURGEON_FAULT_THRESHOLD,
    TRIAGE_PROMOTION_CAP,
    criteria_evidence_ready,
    escalation_demand,
    expected_step,
    is_v3,
    is_v4,
    is_v5,
    legal_steps,
    multi_cause_required_for,
    rank_at_least,
    rank_satisfies,
    surgeon_demand,
    top_open_candidates,
    unresolved_critical_contradictions,
    with_directive_id,
)
from .storage import EventStore, atomic_write_text, canonical_json, object_hash, utc_now


PROTOCOL_VERSION = 2
DEFAULT_CRITERION = "The requested outcome is delivered and directly verified"
CHECKER_KEY_FILE = ".checker-key"
# The canonical field set a checker attestation covers, in every producer and
# verifier: extra fields (a verdict's details) never enter the MAC.
ATTESTED_CHECKER_FIELDS = ("accepted", "artifact_hash", "checker_id", "checker_version",
                           "log_hash", "toolchain_hash")
ROLES = {"planner", "researcher", "critic", "verifier", "synthesizer"}
ROLES_V3 = ROLES | {"explorer"}
ROLES_V4 = ROLES_V3 | {"surgeon"}
OUTCOMES = {"progress", "no_progress", "failed", "inconclusive"}
CRITIC_VERDICTS = {"validated_progress", "mixed", "no_progress", "invalid"}
EVIDENCE_QUALITIES = {"direct", "indirect"}
CRITERION_STATUSES = {"open", "partial", "satisfied", "failed_verification"}
HEX_256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]
    return value or "task"


def checker_attestation_mac(key_hex: str, record: Dict[str, Any]) -> str:
    """HMAC-SHA256 over the canonical checker fields, keyed by the workspace secret.

    The runner that actually executed the checker signs its verdict with the
    workspace key; the engine recomputes and compares at evidence submission.
    An adapter that merely *imagines* an accepting verdict cannot produce this
    value without deliberately reading the key file — which is the documented
    remaining boundary, not a hidden one.
    """

    core = {field: record[field] for field in ATTESTED_CHECKER_FIELDS if field in record}
    return hmac.new(bytes.fromhex(key_hex), canonical_json(core).encode("utf-8"),
                    hashlib.sha256).hexdigest()


class LoopEngine:
    """Event-sourced, policy-enforcing persistence controller for agent work."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.store = EventStore(self.workspace)
        self.lease_path = self.workspace / ".pending-directive.json"
        self.config_path = self.workspace / "loop-config.json"
        self.on_record_status_path = self.workspace / ".on-record-status.json"

    @classmethod
    def create(
        cls,
        root: Path,
        task: str,
        criteria: Optional[List[str]] = None,
        budget: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        controls: Optional[List[str]] = None,
        policy_version: Optional[str] = None,
        charter_hash: Optional[str] = None,
    ) -> "LoopEngine":
        policy_version = policy_version or POLICY_VERSION
        if policy_version not in SUPPORTED_POLICY_VERSIONS or policy_version == "2.0":
            raise ValidationError(
                "New workspaces must use a supported, non-frozen policy version",
                {"received": policy_version, "allowed": [v for v in SUPPORTED_POLICY_VERSIONS if v != "2.0"]},
            )
        task = _nonempty_string(task, "task")
        v4 = policy_version not in ("2.0", "3.0")
        v5 = policy_version not in ("2.0", "3.0", "4.0")
        if charter_hash is not None:
            if not v5:
                raise TransitionError(
                    "A charter hash requires policy version 5.0",
                    {"policy_version": policy_version},
                )
            charter_hash = _nonempty_string(charter_hash, "charter_hash").lower()
            if not HEX_256.fullmatch(charter_hash):
                raise ValidationError("charter_hash must be a SHA-256 hex digest")
        criteria = criteria or [DEFAULT_CRITERION]
        criterion_specs: List[Tuple[str, Optional[str]]] = []
        for item in criteria:
            if isinstance(item, str):
                text, rank = item, None
            elif isinstance(item, dict):
                if not v4:
                    raise TransitionError(
                        "Structured criteria require policy version 4.0",
                        {"policy_version": policy_version},
                    )
                _reject_unknown_keys(item, {"text", "min_formalization_rank"}, "criterion")
                text = item.get("text")
                rank = item.get("min_formalization_rank")
                if rank is not None and rank not in FORMALIZATION_RANKS:
                    raise ValidationError(
                        "Unknown formalization rank",
                        {"allowed": list(FORMALIZATION_RANKS), "received": rank},
                    )
            else:
                raise ValidationError("At least one non-empty acceptance criterion is required")
            if not isinstance(text, str) or not text.strip():
                raise ValidationError("At least one non-empty acceptance criterion is required")
            criterion_specs.append((text.strip(), rank))
        if len({text for text, _rank in criterion_specs}) != len(criterion_specs):
            raise ValidationError("Acceptance criteria must be unique")
        normalized_budget = cls._validate_budget(budget or {})
        controls = controls or []
        if any(not isinstance(item, str) or not item.strip() for item in controls):
            raise ValidationError("Every negative control must be a non-empty description")
        if len({item.strip() for item in controls}) != len(controls):
            raise ValidationError("Negative controls must be unique")

        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        chosen_id = task_id or f"{slug(task)}-{uuid.uuid4().hex[:8]}"
        if not SAFE_ID.fullmatch(chosen_id):
            raise ValidationError("task_id contains unsafe characters", {"task_id": chosen_id})
        workspace = root / chosen_id
        try:
            workspace.mkdir()
        except FileExistsError as exc:
            raise ValidationError("Task workspace already exists", {"workspace": str(workspace)}) from exc

        engine = cls(workspace)
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "policy_version": policy_version,
            "task_id": chosen_id,
            "task": task,
            "criteria": [
                {"id": f"C{index + 1}", "text": text, **(
                    {"min_formalization_rank": rank} if rank is not None else {}
                )}
                for index, (text, rank) in enumerate(criterion_specs)
            ],
            "budget": normalized_budget,
            "controls": [
                {"id": f"CTL{index + 1}", "text": control.strip()}
                for index, control in enumerate(controls)
            ],
        }
        if charter_hash is not None:
            payload["charter_hash"] = charter_hash
        with engine.store.lock():
            engine.store.append_unlocked("task_created", payload, f"create:{chosen_id}")
            events = engine.store.read_unlocked()
            state = engine._replay(events)
            engine._materialize_unlocked(events, state)
        return engine

    @classmethod
    def migrate_legacy(
        cls,
        source_workspace: Path,
        target_root: Optional[Path] = None,
        task_id: Optional[str] = None,
    ) -> "LoopEngine":
        """Preserve a v1 workspace and start a proof-clean v2 continuation beside it."""

        source = Path(source_workspace)
        if (source / "events.jsonl").exists():
            raise ValidationError("Workspace already has a v2 event log", {"workspace": str(source)})
        state_path = source / "state.json"
        if not state_path.exists():
            raise ValidationError("Legacy workspace has no state.json", {"workspace": str(source)})
        try:
            legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Legacy state.json is not valid JSON", {"workspace": str(source)}) from exc
        if not isinstance(legacy_state, dict):
            raise ValidationError("Legacy state.json must contain an object")
        task = _nonempty_string(legacy_state.get("task"), "legacy task")
        raw_criteria = legacy_state.get("criteria", [])
        criteria = [
            item["text"].strip()
            for item in raw_criteria
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
        ] or ["Re-establish the legacy result with v2 direct evidence and independent verification"]

        manifest = []
        for name in ("state.json", "task.md", "attempts.jsonl", "evidence.jsonl", "decision-log.md", "report.md"):
            path = source / name
            if path.is_file():
                data = path.read_bytes()
                manifest.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest_hash = object_hash(manifest)
        chosen_id = task_id or f"{slug(source.name)}-v2-{uuid.uuid4().hex[:8]}"
        engine = cls.create(target_root or source.parent, task, criteria, task_id=chosen_id)
        payload = {
            "source_workspace": str(source.resolve()),
            "source_task_id": legacy_state.get("task_id"),
            "source_status": legacy_state.get("status"),
            "manifest": manifest,
            "manifest_hash": manifest_hash,
            "disposition": "preserved_read_only; criteria reopened because v1 claims do not satisfy v2 proof gates",
        }
        with engine.store.lock():
            engine.store.append_unlocked("legacy_imported", payload, f"legacy-import:{manifest_hash}")
            events = engine.store.read_unlocked()
            state = engine._replay(events)
            engine._materialize_unlocked(events, state)
        return engine

    def load(self) -> Dict[str, Any]:
        with self.store.lock():
            self._require_event_workspace_unlocked()
            recovered = self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            state = self._replay(events)
            if recovered:
                self._materialize_unlocked(events, state)
            return state

    def next_instruction(self, explore: bool = False) -> Dict[str, Any]:
        with self.store.lock():
            self._require_event_workspace_unlocked()
            self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            state = self._replay(events)
            events, state = self._apply_system_gates_unlocked(events, state)
            self._repair_projections_if_needed_unlocked(events, state)
            steps = self._legal_steps(state)
            directive = steps[0]
            if explore:
                alternate = next(
                    (step for step in steps[1:] if step.get("mode") == "ideation"),
                    None,
                )
                if directive.get("mode") != "ideation" and alternate is None:
                    raise TransitionError(
                        "Voluntary ideation is not currently legal",
                        {"next": {"action": directive["action"], "role": directive.get("role"), "mode": directive.get("mode")}},
                    )
                directive = alternate or directive
            self._write_lease_unlocked(state, steps[0])
            return directive

    def _legal_steps(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        if is_v3(state):
            return legal_steps(state)
        return [with_directive_id(state, expected_step(state))]

    def record_attempt(self, submission: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(submission, dict):
            raise ValidationError("Attempt submission must be a JSON object")
        request_id = _safe_identifier(submission.get("request_id"), "request_id")
        submission_hash = object_hash(submission)

        with self.store.lock():
            self._require_event_workspace_unlocked()
            recovered = self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            duplicate = self._find_request(events, request_id)
            if duplicate:
                if duplicate["type"] == "attempt_recorded" and duplicate["payload"].get("submission_hash") == submission_hash:
                    state = self._replay(events)
                    if recovered:
                        self._materialize_unlocked(events, state)
                    return state
                raise IdempotencyConflict(
                    f"Request id {request_id!r} was already used for different content",
                    {"existing_event": duplicate["seq"], "existing_type": duplicate["type"]},
                )

            state = self._replay(events)
            events, state = self._apply_system_gates_unlocked(events, state)
            if state["status"] != "active":
                self._materialize_unlocked(events, state)
                raise TransitionError(
                    "Cannot record an attempt in a terminal state",
                    {"status": state["status"], "terminal": state.get("terminal")},
                )

            steps = self._legal_steps(state)
            if steps[0]["action"] != "attempt":
                raise TransitionError("No attempt is currently allowed", {"next": steps[0]})
            directive = next(
                (step for step in steps if step.get("directive_id") == submission.get("directive_id")),
                None,
            )
            if directive is None:
                raise TransitionError(
                    "Directive is missing or stale",
                    {
                        "expected": steps[0]["directive_id"] if len(steps) == 1 else [step["directive_id"] for step in steps],
                        "received": submission.get("directive_id"),
                    },
                )
            attempt = self._validate_and_normalize_attempt(submission, state, directive)
            payload = {"submission_hash": submission_hash, "attempt": attempt}
            self.store.append_unlocked("attempt_recorded", payload, request_id)
            events = self.store.read_unlocked()
            state = self._replay(events)
            events, state = self._apply_system_gates_unlocked(events, state)
            self._materialize_unlocked(events, state)
            self._clear_lease_unlocked()
        self._run_on_record_hook(state)
        return state

    def completion_ready(self) -> bool:
        with self.store.lock():
            self._require_event_workspace_unlocked()
            self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            state = self._replay(events)
            events, state = self._apply_system_gates_unlocked(events, state)
            self._materialize_unlocked(events, state)
            return not self._completion_failures_unlocked(events, state)

    def finalize(self, request_id: Optional[str] = None) -> Dict[str, Any]:
        with self.store.lock():
            self._require_event_workspace_unlocked()
            self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            state = self._replay(events)
            if state["status"] == "completed":
                self._repair_projections_if_needed_unlocked(events, state)
                return state
            events, state = self._apply_system_gates_unlocked(events, state)
            if state["status"] != "active":
                self._materialize_unlocked(events, state)
                raise TransitionError("Only an active task can complete", {"status": state["status"]})
            failures = self._completion_failures_unlocked(events, state)
            if failures:
                raise TransitionError("Completion gate failed", {"failures": failures})
            request_id = request_id or f"finalize:{state['integrity']['event_head']}"
            _safe_identifier(request_id, "request_id")
            payload = {
                "report_hash": state["report"]["content_hash"],
                "verified_criteria": [criterion["id"] for criterion in state["criteria"]],
                "verifier_attempt_id": state["verification"]["attempt_id"],
            }
            self.store.append_unlocked("task_completed", payload, request_id)
            events = self.store.read_unlocked()
            state = self._replay(events)
            self._materialize_unlocked(events, state)
            self._clear_lease_unlocked()
        self._run_on_record_hook(state)
        return state

    def mark_blocked(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(decision, dict):
            raise ValidationError("Blocked decision must be a JSON object")
        request_id = _safe_identifier(decision.get("request_id"), "request_id")
        with self.store.lock():
            self._require_event_workspace_unlocked()
            recovered = self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            state = self._replay(events)
            duplicate = self._find_request(events, request_id)
            if duplicate:
                if duplicate["type"] == "task_blocked" and duplicate["payload"].get("decision_hash") == object_hash(decision):
                    if recovered:
                        self._materialize_unlocked(events, state)
                    return state
                raise IdempotencyConflict(f"Request id {request_id!r} is already in use")
            events, state = self._apply_system_gates_unlocked(events, state)
            if state["status"] != "active":
                self._materialize_unlocked(events, state)
                raise TransitionError("Only an active task can be marked blocked", {"status": state["status"]})
            payload = self._validate_blocked_decision(decision, state)
            self.store.append_unlocked("task_blocked", payload, request_id)
            events = self.store.read_unlocked()
            state = self._replay(events)
            self._materialize_unlocked(events, state)
            self._clear_lease_unlocked()
        self._run_on_record_hook(state)
        return state

    def mark_unsafe(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(decision, dict):
            raise ValidationError("Unsafe decision must be a JSON object")
        request_id = _safe_identifier(decision.get("request_id"), "request_id")
        with self.store.lock():
            self._require_event_workspace_unlocked()
            recovered = self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            state = self._replay(events)
            duplicate = self._find_request(events, request_id)
            if duplicate:
                if duplicate["type"] == "task_unsafe" and duplicate["payload"].get("decision_hash") == object_hash(decision):
                    if recovered:
                        self._materialize_unlocked(events, state)
                    return state
                raise IdempotencyConflict(f"Request id {request_id!r} is already in use")
            if state["status"] != "active":
                raise TransitionError("Only an active task can be marked unsafe", {"status": state["status"]})
            allowed = {"request_id", "boundary", "risk", "halted_action", "evidence_ids"}
            _reject_unknown_keys(decision, allowed, "unsafe decision")
            evidence_ids = _string_list(decision.get("evidence_ids", []), "evidence_ids", allow_empty=True)
            self._require_evidence_ids(evidence_ids, state)
            payload = {
                "decision_hash": object_hash(decision),
                "boundary": _nonempty_string(decision.get("boundary"), "boundary"),
                "risk": _nonempty_string(decision.get("risk"), "risk"),
                "halted_action": _nonempty_string(decision.get("halted_action"), "halted_action"),
                "evidence_ids": evidence_ids,
            }
            self.store.append_unlocked("task_unsafe", payload, request_id)
            events = self.store.read_unlocked()
            state = self._replay(events)
            self._materialize_unlocked(events, state)
            self._clear_lease_unlocked()
        self._run_on_record_hook(state)
        return state

    def _record_control_event(
        self,
        decision: Dict[str, Any],
        event_type: str,
        allowed: Set[str],
        allowed_statuses: Set[str],
        build_payload,
        decision_name: str,
    ) -> Dict[str, Any]:
        """Shared flow for the small operator-facing chain events.

        Validation is purely structural (strings, statuses, identifiers): the
        engine records the operator's stated reasons, it never interprets them.
        """

        if not isinstance(decision, dict):
            raise ValidationError(f"{decision_name} must be a JSON object")
        request_id = _safe_identifier(decision.get("request_id"), "request_id")
        decision_hash = object_hash(decision)
        with self.store.lock():
            self._require_event_workspace_unlocked()
            recovered = self.store.recover_pending_unlocked()
            events = self.store.read_unlocked()
            state = self._replay(events)
            # Adoption only — control events never trip the budget gate, but an
            # adopted overlay must land before any other event can interleave.
            events, state = self._apply_overlay_adoption_unlocked(events, state)
            duplicate = self._find_request(events, request_id)
            if duplicate:
                if duplicate["type"] == event_type and duplicate["payload"].get("decision_hash") == decision_hash:
                    if recovered:
                        self._materialize_unlocked(events, state)
                    return state
                raise IdempotencyConflict(f"Request id {request_id!r} is already in use")
            if state["status"] not in allowed_statuses:
                raise TransitionError(
                    f"{decision_name} requires status in {sorted(allowed_statuses)}",
                    {"status": state["status"]},
                )
            _reject_unknown_keys(decision, allowed, decision_name)
            payload = {"decision_hash": decision_hash, **build_payload(decision, state)}
            self.store.append_unlocked(event_type, payload, request_id)
            events = self.store.read_unlocked()
            state = self._replay(events)
            self._materialize_unlocked(events, state)
            self._clear_lease_unlocked()
        self._run_on_record_hook(state)
        return state

    def request_human_input(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Pause honestly on a recorded question: active -> awaiting_human.

        On 4.0 the question must classify what the human is actually for, and a
        harness gap is refused outright: the loop's own contract is amended by
        surgeon/overlay_diagnosis, never dumped on the operator.
        """

        def build(decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            payload = {"question": _nonempty_string(decision.get("question"), "question")}
            if "context" in decision:
                payload["context"] = _nonempty_string(decision.get("context"), "context")
            if is_v4(state):
                classification = decision.get("classification")
                if classification == "harness_gap":
                    raise TransitionError(
                        "A harness gap is not a human dependency; the legal next step is"
                        " surgeon/overlay_diagnosis",
                        {"classification": classification},
                    )
                if classification not in HUMAN_INPUT_CLASSIFICATIONS:
                    raise ValidationError(
                        "Human input requests must classify what the human is for",
                        {"allowed": list(HUMAN_INPUT_CLASSIFICATIONS), "received": classification},
                    )
                if state.get("pending_overlay_review"):
                    raise TransitionError(
                        "An overlay review is owed; the legal next step is critic/overlay_review",
                        {"pending_overlay_review": state["pending_overlay_review"]},
                    )
                demand = surgeon_demand(state)
                if demand is not None:
                    raise TransitionError(
                        "Harness diagnosis is owed; the legal next step is surgeon/overlay_diagnosis",
                        {"owed": demand["mark"]},
                    )
                payload["classification"] = classification
            elif "classification" in decision:
                raise ValidationError(
                    "human input request contains unknown fields",
                    {"unknown": ["classification"]},
                )
            return payload

        return self._record_control_event(
            decision,
            "human_input_requested",
            {"request_id", "question", "context", "classification"},
            {"active"},
            build,
            "human input request",
        )

    def record_process_fault(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Record one exhausted burst of a repeating process fault (4.0, non-terminal).

        The signature is an opaque hash of the submitter's normalized error
        class; the kernel counts repeats, it never interprets the failure. The
        task stays active — enough repeats of one signature schedule
        surgeon/overlay_diagnosis instead of a human question.
        """

        def build(decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            if not is_v4(state):
                raise TransitionError(
                    "process_fault_recorded requires policy version 4.0",
                    {"policy_version": state.get("policy_version")},
                )
            signature = _nonempty_string(decision.get("signature"), "signature").lower()
            if not HEX_256.fullmatch(signature):
                raise ValidationError("signature must be a SHA-256 hex digest")
            return {
                "signature": signature,
                "source": _nonempty_string(decision.get("source"), "source"),
                "detail": _nonempty_string(decision.get("detail"), "detail"),
            }

        return self._record_control_event(
            decision,
            "process_fault_recorded",
            {"request_id", "signature", "source", "detail"},
            {"active"},
            build,
            "process fault record",
        )

    def provide_human_input(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Resume a paused task with the operator's recorded answer."""

        def build(decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            return {"answer": _nonempty_string(decision.get("answer"), "answer")}

        return self._record_control_event(
            decision,
            "human_input_provided",
            {"request_id", "answer"},
            {"awaiting_human"},
            build,
            "human input answer",
        )

    def unblock(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Revive a blocked task whose blocking premise no longer holds."""

        def build(decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            payload = {"reason": _nonempty_string(decision.get("reason"), "reason")}
            if "note" in decision:
                payload["note"] = _nonempty_string(decision.get("note"), "note")
            evidence_ids = _string_list(decision.get("evidence_ids", []), "evidence_ids", allow_empty=True)
            self._require_evidence_ids(evidence_ids, state)
            if evidence_ids:
                payload["evidence_ids"] = evidence_ids
            return payload

        return self._record_control_event(
            decision,
            "task_unblocked",
            {"request_id", "reason", "note", "evidence_ids"},
            {"blocked"},
            build,
            "unblock decision",
        )

    def record_retest(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Record the outcome of re-checking a blocked task's premise."""

        def build(decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            outcome = decision.get("outcome")
            if outcome not in {"premise_holds", "premise_no_longer_holds"}:
                raise ValidationError(
                    "Retest outcome must be premise_holds or premise_no_longer_holds",
                    {"received": outcome},
                )
            payload = {
                "outcome": outcome,
                "note": _nonempty_string(decision.get("note"), "note"),
            }
            if "recheck_after" in decision:
                recheck_after = _nonempty_string(decision.get("recheck_after"), "recheck_after")
                _parse_time(recheck_after, "recheck_after")
                payload["recheck_after"] = recheck_after
            return payload

        return self._record_control_event(
            decision,
            "task_retested",
            {"request_id", "outcome", "note", "recheck_after"},
            {"blocked"},
            build,
            "retest record",
        )

    def add_criterion(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Add a strictly additive acceptance criterion to an active 3.0 task."""

        def build(decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            if not is_v3(state):
                raise TransitionError(
                    "criterion_added requires policy version 3.0",
                    {"policy_version": state.get("policy_version")},
                )
            text = _nonempty_string(decision.get("text"), "text")
            if text in {criterion["text"] for criterion in state["criteria"]}:
                raise ValidationError("Criterion text already exists", {"text": text})
            criterion: Dict[str, Any] = {"id": f"C{len(state['criteria']) + 1}", "text": text}
            if "min_formalization_rank" in decision:
                if not is_v4(state):
                    raise TransitionError(
                        "Criterion minimum ranks require policy version 4.0",
                        {"policy_version": state.get("policy_version")},
                    )
                rank = decision["min_formalization_rank"]
                if rank not in FORMALIZATION_RANKS:
                    raise ValidationError(
                        "Unknown formalization rank",
                        {"allowed": list(FORMALIZATION_RANKS), "received": rank},
                    )
                criterion["min_formalization_rank"] = rank
            return {
                "criterion": criterion,
                "rationale": _nonempty_string(decision.get("rationale"), "rationale"),
            }

        return self._record_control_event(
            decision,
            "criterion_added",
            {"request_id", "text", "rationale", "min_formalization_rank"},
            {"active"},
            build,
            "criterion addition",
        )

    def fulfill_wish(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Mark a recorded wish as fulfilled: the designed entry point for external progress."""

        def build(decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
            wish_id = _nonempty_string(decision.get("wish_id"), "wish_id")
            wish = next((item for item in state.get("wishes", []) if item["id"] == wish_id), None)
            if wish is None:
                raise ValidationError("Unknown wish id", {"wish_id": wish_id})
            if wish["status"] != "open":
                raise TransitionError("Wish is not open", {"wish_id": wish_id, "status": wish["status"]})
            return {
                "wish_id": wish_id,
                "note": _nonempty_string(decision.get("note"), "note"),
            }

        return self._record_control_event(
            decision,
            "wish_fulfilled",
            {"request_id", "wish_id", "note"},
            {"active"},
            build,
            "wish fulfillment",
        )

    def audit(self, repair: bool = False) -> Dict[str, Any]:
        with self.store.lock():
            try:
                self._require_event_workspace_unlocked()
                recovered_pending = self.store.recover_pending_unlocked() if repair else False
                events = self.store.read_unlocked()
                state = self._replay(events)
            except (IntegrityError, LegacyWorkspaceError) as exc:
                return {
                    "ok": False,
                    "event_log_valid": False,
                    "repaired": False,
                    "issues": [exc.as_dict()],
                }

            expected = self._projection_texts(events, state)
            mismatches = self._projection_mismatches(expected)
            semantic = self._semantic_issues(state)
            repaired = recovered_pending
            if repair and mismatches:
                self._materialize_unlocked(events, state)
                mismatches = self._projection_mismatches(expected)
                repaired = True
            issues: List[Dict[str, Any]] = []
            issues.extend({"kind": "projection_mismatch", "path": path} for path in mismatches)
            issues.extend({"kind": "semantic", "message": message} for message in semantic)
            if self.store.pending_path.exists():
                issues.append({"kind": "pending_transaction", "path": str(self.store.pending_path)})
            return {
                "ok": not issues,
                "event_log_valid": True,
                "event_count": len(events),
                "event_head": events[-1]["hash"],
                "projection_mismatches": mismatches,
                "repaired": repaired,
                "issues": issues,
            }

    def recover(self) -> Dict[str, Any]:
        return self.audit(repair=True)

    # ------------------------------------------------------------------
    # Supervision: the obligation lease, staleness, and the durability hook
    # ------------------------------------------------------------------

    def _write_lease_unlocked(self, state: Dict[str, Any], directive: Dict[str, Any]) -> None:
        if directive["action"] in {"attempt", "finalize"}:
            lease = {
                "issued_at": utc_now(),
                "action": directive["action"],
                "directive_id": directive.get("directive_id"),
                "role": directive.get("role"),
                "mode": directive.get("mode"),
                "required_move": directive.get("required_move"),
                "event_head": state["integrity"]["event_head"],
                "revision": state["revision"],
            }
            atomic_write_text(self.lease_path, canonical_json(lease) + "\n")
        else:
            self._clear_lease_unlocked()

    def _clear_lease_unlocked(self) -> None:
        try:
            self.lease_path.unlink()
        except FileNotFoundError:
            pass

    def _read_lease(self) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(self.lease_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _load_config(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            print(
                f"adv-loop: {self.config_path} is not valid JSON; ignoring the workspace config",
                file=sys.stderr,
            )
            return {}
        return value if isinstance(value, dict) else {}

    def _run_on_record_hook(self, state: Dict[str, Any]) -> None:
        """Best-effort durability hook (e.g. commit+push) after every append.

        Failure never affects the recorded event; it is surfaced loudly and in
        the sidecar so `status`/`guard` can report how many events sit beyond
        the last successful run.
        """

        config = self._load_config()
        command = config.get("on_record")
        if not command:
            return
        if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
            print("adv-loop: loop-config.json on_record must be an argv list; skipping", file=sys.stderr)
            return
        timeout = config.get("on_record_timeout_seconds", 300)
        outcome: Dict[str, Any] = {
            "at": utc_now(),
            "event_head": state["integrity"]["event_head"],
            "revision": state["revision"],
            "command": command,
        }
        try:
            result = subprocess.run(
                command,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            outcome["ok"] = result.returncode == 0
            outcome["returncode"] = result.returncode
            if result.returncode != 0:
                outcome["stderr"] = result.stderr[-2000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            outcome["ok"] = False
            outcome["error"] = str(exc)
        try:
            atomic_write_text(self.on_record_status_path, canonical_json(outcome) + "\n")
        except OSError:
            pass
        if not outcome["ok"]:
            print(
                "adv-loop: on_record durability hook FAILED — the event log may exist only locally: "
                + json.dumps({key: outcome[key] for key in outcome if key not in {"command"}}),
                file=sys.stderr,
            )

    def _read_on_record_status(self) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(self.on_record_status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def supervision(self, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Obligation and staleness report for supervisors; wall-clock aware.

        This is deliberately not part of the byte-audited state projection:
        it mixes replayed state with transient sidecars and the current time.
        """

        if state is None:
            state = self.load()
        now = datetime.now(timezone.utc)
        report: Dict[str, Any] = {"status": state["status"], "task_id": state["task_id"]}
        obligation: Optional[Dict[str, Any]] = None
        if state["status"] == "active":
            steps = self._legal_steps(state)
            step = steps[0]
            obligation = {
                "action": step["action"],
                "role": step.get("role"),
                "mode": step.get("mode"),
                "required_move": step.get("required_move"),
                "directive_id": step.get("directive_id"),
            }
        report["obligation"] = obligation
        if is_v3(state):
            report["criterion_failure_streaks"] = dict(state.get("criterion_failure_streaks", {}))
            demand = escalation_demand(state)
            report["owed_escalation"] = (
                {
                    "mark": demand["mark"],
                    "role": demand["role"],
                    "mode": demand["mode"],
                    "required_move": demand.get("required_move"),
                }
                if demand
                else None
            )
        if is_v4(state):
            owed_diagnosis = surgeon_demand(state)
            report["owed_diagnosis"] = (
                {"kind": owed_diagnosis["kind"], "mark": owed_diagnosis["mark"]}
                if owed_diagnosis
                else None
            )
            report["pending_overlay_review"] = state.get("pending_overlay_review")
            report["overlay_revision"] = state.get("overlay_revision", 0)
        lease = self._read_lease()
        if lease:
            issued = _parse_time(lease.get("issued_at", state["updated_at"]), "lease issued_at")
            lease_report = dict(lease)
            lease_report["age_seconds"] = max(0, int((now - issued).total_seconds()))
            lease_report["events_since"] = state["revision"] - lease.get("revision", state["revision"])
            lease_report["stale"] = lease.get("event_head") != state["integrity"]["event_head"]
            report["pending_directive"] = lease_report
        else:
            report["pending_directive"] = None
        config = self._load_config()
        horizon = config.get("stall_horizon_seconds", 21600)
        last_event = _parse_time(state["updated_at"], "updated_at")
        idle_seconds = max(0, int((now - last_event).total_seconds()))
        report["idle_seconds"] = idle_seconds
        report["stalled"] = bool(state["status"] == "active" and idle_seconds >= horizon)
        report["stall_horizon_seconds"] = horizon
        if state["status"] == "blocked":
            report["retest"] = self._retest_report(state, now)
        if is_v3(state):
            due_wishes = []
            for wish in state.get("wishes", []):
                if wish["status"] != "open":
                    continue
                if now >= _parse_time(wish["recheck_after"], "wish recheck_after"):
                    due_wishes.append({"id": wish["id"], "statement": wish["statement"], "test": wish["test"]})
            report["wishes_due"] = due_wishes
        on_record = self._read_on_record_status()
        if on_record is not None:
            report["on_record"] = {
                "ok": on_record.get("ok"),
                "at": on_record.get("at"),
                "events_since": state["revision"] - on_record.get("revision", state["revision"]),
            }
        elif config.get("on_record"):
            report["on_record"] = {"ok": None, "at": None, "events_since": state["revision"]}
        return report

    def _retest_report(self, state: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        terminal = state.get("terminal") or {}
        spec = terminal.get("retest")
        retests = state.get("retests", [])
        last = retests[-1] if retests else None
        recheck_after = None
        if last and last.get("recheck_after"):
            recheck_after = last["recheck_after"]
        elif spec:
            recheck_after = spec.get("recheck_after")
        due = bool(recheck_after and now >= _parse_time(recheck_after, "recheck_after"))
        premise_no_longer_holds = bool(last and last.get("outcome") == "premise_no_longer_holds")
        return {
            "premise": spec.get("premise") if spec else None,
            "probe": spec.get("probe") if spec else None,
            "recheck_after": recheck_after,
            "due": due,
            "last_outcome": last.get("outcome") if last else None,
            "premise_no_longer_holds": premise_no_longer_holds,
        }

    @staticmethod
    def _validate_budget(budget: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(budget, dict):
            raise ValidationError("budget must be an object")
        _reject_unknown_keys(budget, {"max_attempts", "max_failures", "deadline"}, "budget")
        normalized: Dict[str, Any] = {}
        for name in ("max_attempts", "max_failures"):
            if name in budget:
                value = budget[name]
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise ValidationError(f"{name} must be a positive integer")
                normalized[name] = value
        if "deadline" in budget:
            deadline = _nonempty_string(budget["deadline"], "deadline")
            _parse_time(deadline, "deadline")
            normalized["deadline"] = deadline
        return normalized

    def _validate_and_normalize_attempt(
        self,
        submission: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> Dict[str, Any]:
        v3 = is_v3(state)
        v4 = is_v4(state)
        if v4 and directive["role"] == "surgeon":
            return self._validate_surgeon_attempt(submission, state, directive)
        if v4 and directive["mode"] == "overlay_review":
            return self._validate_overlay_review_attempt(submission, state, directive)
        if v3 and directive["role"] == "explorer":
            return self._validate_explorer_attempt(submission, state, directive)
        if v3 and directive["mode"] == "triage":
            return self._validate_triage_attempt(submission, state, directive)
        allowed = {
            "request_id", "directive_id", "role", "mode", "actor", "strategy", "hypothesis",
            "action", "observation", "interpretation", "uncertainties", "next_step", "outcome",
            "evidence", "criterion_updates", "contradictions", "contradiction_resolutions", "decisions",
            "plan", "plan_id", "criterion_targets", "assessment", "contradiction_search",
            "blocker_audit", "verification_results", "report",
        }
        if v3:
            if directive["role"] == "researcher":
                allowed = allowed | {
                    "basin", "representation_shift", "move", "candidate_id", "assets_registered",
                    "combination", "barrier_probe", "replication_of", "wishes_declared",
                }
            elif directive["role"] == "critic":
                allowed = allowed | {"lens", "wishes_declared"}
            elif directive["role"] == "planner":
                allowed = allowed | {"proposed_criteria"}
        _reject_unknown_keys(submission, allowed, "attempt submission")
        if submission.get("directive_id") != directive["directive_id"]:
            raise TransitionError(
                "Directive is missing or stale",
                {"expected": directive["directive_id"], "received": submission.get("directive_id")},
            )
        role = submission.get("role")
        mode = submission.get("mode")
        if role != directive["role"] or mode != directive["mode"]:
            raise TransitionError(
                "Attempt role or mode does not match the required transition",
                {"expected_role": directive["role"], "expected_mode": directive["mode"]},
            )
        if role not in (ROLES_V4 if v4 else (ROLES_V3 if v3 else ROLES)):
            raise ValidationError("Unknown role", {"role": role})

        actor = self._validate_actor(submission.get("actor"))
        strategy = self._validate_strategy(submission.get("strategy"))
        strategy_fingerprint = object_hash(_strategy_signature(strategy))
        outcome = submission.get("outcome")
        if outcome not in OUTCOMES:
            raise ValidationError("outcome must describe observed progress", {"allowed": sorted(OUTCOMES)})

        attempt_id = f"A{state['attempt_count'] + 1:06d}"
        normalized: Dict[str, Any] = {
            "id": attempt_id,
            "at": utc_now(),
            "request_id": submission["request_id"],
            "directive_id": submission["directive_id"],
            "role": role,
            "mode": mode,
            "actor": actor,
            "strategy": strategy,
            "strategy_fingerprint": strategy_fingerprint,
            "hypothesis": _nonempty_string(submission.get("hypothesis"), "hypothesis"),
            "action": _nonempty_string(submission.get("action"), "action"),
            "observation": _nonempty_string(submission.get("observation"), "observation"),
            "interpretation": _nonempty_string(submission.get("interpretation"), "interpretation"),
            "uncertainties": _string_list(submission.get("uncertainties"), "uncertainties", allow_empty=True),
            "next_step": _nonempty_string(submission.get("next_step"), "next_step"),
            "outcome": outcome,
        }

        contradiction_ids = {
            item.get("id")
            for item in submission.get("contradictions", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        evidence, ref_map = self._normalize_evidence(
            submission.get("evidence", []), state, actor, role, attempt_id, contradiction_ids
        )
        normalized["evidence"] = evidence
        available_evidence = {**state["evidence"], **{item["id"]: item for item in evidence}}

        normalized["criterion_updates"] = self._normalize_criterion_updates(
            submission.get("criterion_updates", []), state, available_evidence, ref_map, role
        )
        normalized["contradictions"] = self._normalize_contradictions(
            submission.get("contradictions", []), state, available_evidence, ref_map
        )
        normalized["contradiction_resolutions"] = self._normalize_resolutions(
            submission.get("contradiction_resolutions", []), state, available_evidence, ref_map,
            {item["id"] for item in evidence},
        )
        normalized["decisions"] = self._normalize_decisions(submission.get("decisions", []))

        if role == "planner":
            normalized["plan"] = self._validate_plan(submission.get("plan"), mode, state)
            if v3:
                proposed = self._validate_proposed_criteria(submission.get("proposed_criteria"), state, mode)
                if proposed:
                    normalized["proposed_criteria"] = proposed
        elif role == "researcher":
            if v3:
                self._prepare_researcher_v3(submission, normalized, state, directive)
            self._validate_researcher(submission, normalized, state, directive)
            normalized["plan_id"] = submission["plan_id"]
            normalized["criterion_targets"] = list(submission["criterion_targets"])
        elif role == "critic":
            self._validate_critic(submission, normalized, state, directive)
        elif role == "verifier":
            normalized["verification_results"] = self._validate_verification(
                submission.get("verification_results"), state, evidence, ref_map, actor
            )
        elif role == "synthesizer":
            normalized["report"] = self._validate_report(submission.get("report"), state)

        if v3 and role in {"researcher", "critic"}:
            wishes = self._validate_wishes_declared(submission.get("wishes_declared"))
            if wishes:
                normalized["wishes_declared"] = wishes

        return normalized

    @staticmethod
    def _validate_actor(actor: Any) -> Dict[str, str]:
        if not isinstance(actor, dict):
            raise ValidationError("actor must be an object")
        required = {"agent_id", "model", "context_id"}
        if not required.issubset(actor):
            raise ValidationError("actor is missing identity fields", {"required": sorted(required)})
        normalized = {key: _nonempty_string(value, f"actor.{key}") for key, value in actor.items()}
        return normalized

    @staticmethod
    def _validate_strategy(strategy: Any) -> Dict[str, str]:
        if not isinstance(strategy, dict):
            raise ValidationError("strategy must be an object")
        missing = set(STRATEGY_DIMENSIONS) - set(strategy)
        unknown = set(strategy) - set(STRATEGY_DIMENSIONS)
        if missing or unknown:
            raise ValidationError(
                "strategy must contain exactly the protocol dimensions",
                {"missing": sorted(missing), "unknown": sorted(unknown)},
            )
        return {dimension: _nonempty_string(strategy[dimension], f"strategy.{dimension}") for dimension in STRATEGY_DIMENSIONS}

    @staticmethod
    def _require_proof_inert(submission: Dict[str, Any], name: str) -> None:
        for field in ("evidence", "criterion_updates", "contradictions", "contradiction_resolutions"):
            if submission.get(field):
                raise ValidationError(
                    f"{name} attempts are proof-inert: speculation may not carry evidence,"
                    " criterion claims, or contradiction bookkeeping",
                    {"field": field},
                )

    def _validate_explorer_attempt(
        self,
        submission: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._require_proof_inert(submission, "Ideation")
        allowed = {
            "request_id", "directive_id", "role", "mode", "actor", "context_scope",
            "ideation_kind", "seeds", "decisions",
            "evidence", "criterion_updates", "contradictions", "contradiction_resolutions",
        }
        _reject_unknown_keys(submission, allowed, "ideation submission")
        if submission.get("directive_id") != directive["directive_id"]:
            raise TransitionError(
                "Directive is missing or stale",
                {"expected": directive["directive_id"], "received": submission.get("directive_id")},
            )
        if submission.get("role") != "explorer" or submission.get("mode") != "ideation":
            raise TransitionError(
                "Attempt role or mode does not match the required transition",
                {"expected_role": "explorer", "expected_mode": "ideation"},
            )
        actor = self._validate_actor(submission.get("actor"))
        if submission.get("context_scope") != "minimal":
            raise ValidationError(
                "Ideation requires the attestation context_scope='minimal': seeds must be"
                " generated from a fresh context, not the accumulated one",
            )
        kind = submission.get("ideation_kind")
        if kind != directive["ideation_kind"]:
            raise ValidationError(
                "ideation_kind must match the directive",
                {"expected": directive["ideation_kind"], "received": kind},
            )
        seeds = submission.get("seeds")
        if not isinstance(seeds, list) or not (SEED_MIN <= len(seeds) <= SEED_MAX):
            raise ValidationError(
                f"Ideation requires between {SEED_MIN} and {SEED_MAX} seeds",
                {"received": len(seeds) if isinstance(seeds, list) else None},
            )
        known_fingerprints = set(state.get("seed_fingerprints", []))
        evolve_parent_ids = (
            {candidate["id"] for candidate in top_open_candidates(state, EVOLVE_TOP_K)}
            if kind == "evolve"
            else set()
        )
        allowed_seed_keys = {
            "claim", "basin", "first_unjustified_step", "kill_test",
            "control_object", "needs", "parents", "representation_shift",
        }
        normalized_seeds: List[Dict[str, Any]] = []
        for index, seed in enumerate(seeds):
            if not isinstance(seed, dict):
                raise ValidationError("Every seed must be an object", {"seed_index": index})
            _reject_unknown_keys(seed, allowed_seed_keys, f"seeds[{index}]")
            claim = _nonempty_string(seed.get("claim"), f"seeds[{index}].claim")
            basin = _nonempty_string(seed.get("basin"), f"seeds[{index}].basin")
            fingerprint = object_hash({"seed_claim": _normalized_strategy_value(claim)})
            if fingerprint in known_fingerprints:
                raise ValidationError(
                    "Seed claim duplicates a seed already proposed in this workspace",
                    {"seed_index": index, "fingerprint": fingerprint},
                )
            known_fingerprints.add(fingerprint)
            normalized_seed: Dict[str, Any] = {
                "claim": claim,
                "basin": basin,
                "first_unjustified_step": _nonempty_string(
                    seed.get("first_unjustified_step"), f"seeds[{index}].first_unjustified_step"
                ),
                "kill_test": _nonempty_string(seed.get("kill_test"), f"seeds[{index}].kill_test"),
                "fingerprint": fingerprint,
            }
            basin_entry = state.get("basins", {}).get(_normalized_strategy_value(basin))
            if "representation_shift" in seed:
                normalized_seed["representation_shift"] = _nonempty_string(
                    seed.get("representation_shift"), f"seeds[{index}].representation_shift"
                )
            elif basin_entry and basin_entry["status"] == "closed":
                raise ValidationError(
                    "A seed re-entering a closed basin must state a representation_shift",
                    {"seed_index": index, "basin": basin},
                )
            if kind == "evolve":
                parents = _string_list(seed.get("parents"), f"seeds[{index}].parents")
                unknown_parents = set(parents) - evolve_parent_ids
                if unknown_parents:
                    raise ValidationError(
                        "Evolve seeds must mutate or recombine the offered top candidates",
                        {"seed_index": index, "unknown_parents": sorted(unknown_parents)},
                    )
                normalized_seed["parents"] = parents
            elif seed.get("parents"):
                raise ValidationError(
                    "Only evolve-kind ideation may cite candidate parents",
                    {"seed_index": index},
                )
            if "control_object" in seed:
                normalized_seed["control_object"] = _nonempty_string(
                    seed.get("control_object"), f"seeds[{index}].control_object"
                )
            if "needs" in seed:
                normalized_seed["needs"] = _string_list(seed.get("needs"), f"seeds[{index}].needs")
            normalized_seeds.append(normalized_seed)

        return {
            "id": f"A{state['attempt_count'] + 1:06d}",
            "at": utc_now(),
            "request_id": submission["request_id"],
            "directive_id": submission["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": actor,
            "context_scope": "minimal",
            "ideation_kind": kind,
            "seeds": normalized_seeds,
            "evidence": [],
            "criterion_updates": [],
            "contradictions": [],
            "contradiction_resolutions": [],
            "decisions": self._normalize_decisions(submission.get("decisions", [])),
        }

    def _validate_triage_attempt(
        self,
        submission: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._require_proof_inert(submission, "Triage")
        allowed = {
            "request_id", "directive_id", "role", "mode", "actor", "triage", "decisions",
            "evidence", "criterion_updates", "contradictions", "contradiction_resolutions",
        }
        _reject_unknown_keys(submission, allowed, "triage submission")
        if submission.get("directive_id") != directive["directive_id"]:
            raise TransitionError(
                "Directive is missing or stale",
                {"expected": directive["directive_id"], "received": submission.get("directive_id")},
            )
        if submission.get("role") != "critic" or submission.get("mode") != "triage":
            raise TransitionError(
                "Attempt role or mode does not match the required transition",
                {"expected_role": "critic", "expected_mode": "triage"},
            )
        actor = self._validate_actor(submission.get("actor"))
        triage = submission.get("triage")
        if not isinstance(triage, dict):
            raise ValidationError("Triage requires a triage object")
        _reject_unknown_keys(triage, {"target_attempt_id", "verdicts", "comparisons"}, "triage")
        target_id = triage.get("target_attempt_id")
        if target_id != directive["target_attempt_id"]:
            raise TransitionError(
                "Triage targets the wrong ideation attempt",
                {"expected": directive["target_attempt_id"]},
            )
        explorer = next(attempt for attempt in state["attempts"] if attempt["id"] == target_id)
        if actor["context_id"] == explorer["actor"]["context_id"]:
            raise ValidationError("Triage must use a fresh context from the ideation attempt")
        seeds = explorer.get("seeds", [])
        verdicts = triage.get("verdicts")
        if not isinstance(verdicts, list) or len(verdicts) != len(seeds):
            raise ValidationError(
                "Triage must judge every seed exactly once",
                {"expected_count": len(seeds)},
            )
        seen_indexes: Set[int] = set()
        normalized_verdicts: List[Dict[str, Any]] = []
        promoted_indexes: Set[int] = set()
        for item in verdicts:
            if not isinstance(item, dict):
                raise ValidationError("Every triage verdict must be an object")
            _reject_unknown_keys(item, {"seed_index", "decision", "reason"}, "triage verdict")
            seed_index = item.get("seed_index")
            if not isinstance(seed_index, int) or isinstance(seed_index, bool) or not (0 <= seed_index < len(seeds)):
                raise ValidationError("Triage verdict has an invalid seed_index", {"seed_index": seed_index})
            if seed_index in seen_indexes:
                raise ValidationError("Triage judged a seed twice", {"seed_index": seed_index})
            seen_indexes.add(seed_index)
            decision = item.get("decision")
            if decision not in {"killed", "promoted"}:
                raise ValidationError("Triage decision must be killed or promoted", {"seed_index": seed_index})
            if decision == "promoted":
                promoted_indexes.add(seed_index)
            normalized_verdicts.append({
                "seed_index": seed_index,
                "decision": decision,
                "reason": _nonempty_string(item.get("reason"), "triage verdict reason"),
            })
        if len(promoted_indexes) > TRIAGE_PROMOTION_CAP:
            raise ValidationError(
                "Triage promoted more seeds than the cap allows",
                {"cap": TRIAGE_PROMOTION_CAP, "promoted": len(promoted_indexes)},
            )
        normalized_verdicts.sort(key=lambda item: item["seed_index"])

        open_candidates = {
            candidate["id"]: candidate
            for candidate in state.get("candidates", [])
            if candidate["status"] == "open"
        }
        comparisons = triage.get("comparisons", [])
        if not isinstance(comparisons, list):
            raise ValidationError("triage.comparisons must be a list")
        normalized_comparisons: List[Dict[str, Any]] = []
        compared_indexes: Set[int] = set()
        for item in comparisons:
            if not isinstance(item, dict):
                raise ValidationError("Every triage comparison must be an object")
            _reject_unknown_keys(item, {"seed_index", "candidate_id", "winner", "rationale"}, "triage comparison")
            seed_index = item.get("seed_index")
            if seed_index not in promoted_indexes:
                raise ValidationError(
                    "Comparisons are recorded for promoted seeds only",
                    {"seed_index": seed_index},
                )
            candidate_id = item.get("candidate_id")
            if candidate_id not in open_candidates:
                raise ValidationError("Comparison references a candidate that is not open", {"candidate_id": candidate_id})
            winner = item.get("winner")
            if winner not in {"seed", "candidate"}:
                raise ValidationError("Comparison winner must be seed or candidate")
            compared_indexes.add(seed_index)
            normalized_comparisons.append({
                "seed_index": seed_index,
                "candidate_id": candidate_id,
                "winner": winner,
                "rationale": _nonempty_string(item.get("rationale"), "comparison rationale"),
            })
        if open_candidates and promoted_indexes - compared_indexes:
            raise ValidationError(
                "Every promoted seed must be compared pairwise against at least one open candidate",
                {"uncompared": sorted(promoted_indexes - compared_indexes)},
            )

        return {
            "id": f"A{state['attempt_count'] + 1:06d}",
            "at": utc_now(),
            "request_id": submission["request_id"],
            "directive_id": submission["directive_id"],
            "role": "critic",
            "mode": "triage",
            "actor": actor,
            "triage": {
                "target_attempt_id": target_id,
                "verdicts": normalized_verdicts,
                "comparisons": normalized_comparisons,
            },
            "evidence": [],
            "criterion_updates": [],
            "contradictions": [],
            "contradiction_resolutions": [],
            "decisions": self._normalize_decisions(submission.get("decisions", [])),
        }

    def _validate_surgeon_attempt(
        self,
        submission: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> Dict[str, Any]:
        """A harness diagnosis: proof-inert, bound to the owed demand.

        The proposed delta (harness_gap only) is validated structurally here so
        an illegal amendment is refused before it can ever reach a review; the
        diagnosis itself still records — the failure is information.
        """

        self._require_proof_inert(submission, "Diagnosis")
        allowed = {
            "request_id", "directive_id", "role", "mode", "actor", "diagnosis", "decisions",
            "evidence", "criterion_updates", "contradictions", "contradiction_resolutions",
        }
        _reject_unknown_keys(submission, allowed, "diagnosis submission")
        if submission.get("directive_id") != directive["directive_id"]:
            raise TransitionError(
                "Directive is missing or stale",
                {"expected": directive["directive_id"], "received": submission.get("directive_id")},
            )
        if submission.get("role") != "surgeon" or submission.get("mode") != "overlay_diagnosis":
            raise TransitionError(
                "Attempt role or mode does not match the required transition",
                {"expected_role": "surgeon", "expected_mode": "overlay_diagnosis"},
            )
        actor = self._validate_actor(submission.get("actor"))
        diagnosis = submission.get("diagnosis")
        if not isinstance(diagnosis, dict):
            raise ValidationError("Diagnosis requires a diagnosis object")
        _reject_unknown_keys(
            diagnosis,
            {"raw_detail", "classification", "kill_test", "next_experiment",
             "fault_signature", "proposed_delta", "delta_fingerprint"},
            "diagnosis",
        )
        classification = diagnosis.get("classification")
        if classification not in SURGEON_CLASSIFICATIONS:
            raise ValidationError(
                "Diagnosis classification is unknown",
                {"allowed": list(SURGEON_CLASSIFICATIONS), "received": classification},
            )
        demand = directive["demand"]
        normalized_diagnosis: Dict[str, Any] = {
            "raw_detail": _nonempty_string(diagnosis.get("raw_detail"), "diagnosis.raw_detail"),
            "classification": classification,
            "kill_test": _nonempty_string(diagnosis.get("kill_test"), "diagnosis.kill_test"),
            "next_experiment": _nonempty_string(diagnosis.get("next_experiment"), "diagnosis.next_experiment"),
        }
        if demand["kind"] == "fault":
            signature = _nonempty_string(diagnosis.get("fault_signature"), "diagnosis.fault_signature").lower()
            if signature != demand["signature"]:
                raise ValidationError(
                    "Diagnosis must address the demanded fault signature",
                    {"expected": demand["signature"], "received": signature},
                )
            normalized_diagnosis["fault_signature"] = signature
        elif diagnosis.get("fault_signature") is not None:
            raise ValidationError("fault_signature is only recorded for a fault demand")
        if classification == "harness_gap":
            delta = self._validate_overlay_delta(diagnosis.get("proposed_delta"), state)
            fingerprint = _nonempty_string(diagnosis.get("delta_fingerprint"), "diagnosis.delta_fingerprint").lower()
            if not HEX_256.fullmatch(fingerprint):
                raise ValidationError("delta_fingerprint must be a SHA-256 hex digest")
            normalized_diagnosis["proposed_delta"] = delta
            normalized_diagnosis["delta_fingerprint"] = fingerprint
        elif diagnosis.get("proposed_delta") is not None or diagnosis.get("delta_fingerprint") is not None:
            raise ValidationError(
                "Only a harness_gap diagnosis proposes an overlay delta",
                {"classification": classification},
            )
        return {
            "id": f"A{state['attempt_count'] + 1:06d}",
            "at": utc_now(),
            "request_id": submission["request_id"],
            "directive_id": submission["directive_id"],
            "role": "surgeon",
            "mode": "overlay_diagnosis",
            "actor": actor,
            "diagnosis": normalized_diagnosis,
            "demand": {key: demand[key] for key in demand if key != "fault"},
            "evidence": [],
            "criterion_updates": [],
            "contradictions": [],
            "contradiction_resolutions": [],
            "decisions": self._normalize_decisions(submission.get("decisions", [])),
        }

    def _validate_overlay_review_attempt(
        self,
        submission: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Fresh-context judgement of a proposed overlay: adopt, reject, or narrow.

        The effective delta (proposed on adopt, narrowed subset on narrow) is
        re-validated tighten-only here, so an adoption can never fail later.
        """

        self._require_proof_inert(submission, "Overlay review")
        allowed = {
            "request_id", "directive_id", "role", "mode", "actor", "overlay_review", "decisions",
            "evidence", "criterion_updates", "contradictions", "contradiction_resolutions",
        }
        _reject_unknown_keys(submission, allowed, "overlay review submission")
        if submission.get("directive_id") != directive["directive_id"]:
            raise TransitionError(
                "Directive is missing or stale",
                {"expected": directive["directive_id"], "received": submission.get("directive_id")},
            )
        if submission.get("role") != "critic" or submission.get("mode") != "overlay_review":
            raise TransitionError(
                "Attempt role or mode does not match the required transition",
                {"expected_role": "critic", "expected_mode": "overlay_review"},
            )
        actor = self._validate_actor(submission.get("actor"))
        review = submission.get("overlay_review")
        if not isinstance(review, dict):
            raise ValidationError("Overlay review requires an overlay_review object")
        _reject_unknown_keys(
            review,
            {"target_attempt_id", "verdict", "reasons", "uncertainty", "narrowed_delta"},
            "overlay_review",
        )
        target_id = review.get("target_attempt_id")
        if target_id != directive["target_attempt_id"]:
            raise TransitionError(
                "Overlay review targets the wrong diagnosis",
                {"expected": directive["target_attempt_id"]},
            )
        surgeon = next(attempt for attempt in state["attempts"] if attempt["id"] == target_id)
        if actor["context_id"] == surgeon["actor"]["context_id"]:
            raise ValidationError("Overlay review must use a fresh context from the diagnosis")
        verdict = review.get("verdict")
        if verdict not in OVERLAY_REVIEW_VERDICTS:
            raise ValidationError(
                "Unknown overlay review verdict",
                {"allowed": list(OVERLAY_REVIEW_VERDICTS)},
            )
        normalized_review: Dict[str, Any] = {
            "target_attempt_id": target_id,
            "verdict": verdict,
            "reasons": _string_list(review.get("reasons"), "overlay_review.reasons"),
            "uncertainty": _nonempty_string(review.get("uncertainty"), "overlay_review.uncertainty"),
        }
        proposed = surgeon["diagnosis"].get("proposed_delta")
        if verdict == "narrow":
            narrowed = self._validate_overlay_delta(review.get("narrowed_delta"), state)
            proposed_ops = proposed["ops"] if proposed else []
            for op in narrowed["ops"]:
                if op not in proposed_ops:
                    raise ValidationError(
                        "A narrowed delta must be a subset of the proposed operations",
                        {"op": op.get("op")},
                    )
            normalized_review["narrowed_delta"] = narrowed
        else:
            if review.get("narrowed_delta") is not None:
                raise ValidationError("Only a narrow verdict supplies narrowed_delta")
            if verdict == "adopt":
                if proposed is None:
                    raise ValidationError("There is no proposed delta to adopt", {"target": target_id})
                # Re-validate against the current state: adoption must never fail.
                self._validate_overlay_delta(proposed, state)
        return {
            "id": f"A{state['attempt_count'] + 1:06d}",
            "at": utc_now(),
            "request_id": submission["request_id"],
            "directive_id": submission["directive_id"],
            "role": "critic",
            "mode": "overlay_review",
            "actor": actor,
            "overlay_review": normalized_review,
            "evidence": [],
            "criterion_updates": [],
            "contradictions": [],
            "contradiction_resolutions": [],
            "decisions": self._normalize_decisions(submission.get("decisions", [])),
        }

    def _validate_overlay_delta(self, delta: Any, state: Dict[str, Any]) -> Dict[str, Any]:
        """Allowlist validation of an overlay delta: every op adds or tightens.

        Anything outside the allowlist — skipping a review, waiving evidence,
        reusing fingerprints, reopening terminals, lowering ranks, declaring a
        human unnecessary, touching another workspace — is rejected by name
        here, because no such operation exists to fold.
        """

        if not isinstance(delta, dict):
            raise ValidationError("Overlay delta must be an object")
        _reject_unknown_keys(delta, {"schema_version", "ops"}, "overlay delta")
        if delta.get("schema_version") != OVERLAY_SCHEMA_VERSION:
            raise ValidationError(
                "Overlay delta schema_version is unsupported",
                {"expected": OVERLAY_SCHEMA_VERSION, "received": delta.get("schema_version")},
            )
        ops = delta.get("ops")
        if not isinstance(ops, list) or not ops:
            raise ValidationError("Overlay delta requires a non-empty ops list")
        # Working views so ops within one delta compose and cannot collide.
        kinds = set(state.get("evidence_kind_registry", {}))
        hooks = {hook["hook_id"] for hook in state.get("validator_hooks", [])}
        boxes = {box["sandbox_id"] for box in state.get("sandboxes", [])}
        stall_signatures = set(state.get("stall_classes", {}))
        pins = set(state.get("toolchain_pins", {}))
        criterion_ranks = {
            criterion["id"]: criterion.get("min_formalization_rank")
            for criterion in state["criteria"]
        }
        criterion_texts = {criterion["text"] for criterion in state["criteria"]}
        next_criterion = len(state["criteria"]) + 1
        normalized_ops: List[Dict[str, Any]] = []
        for index, op in enumerate(ops):
            if not isinstance(op, dict):
                raise ValidationError("Every overlay op must be an object", {"index": index})
            name = op.get("op")
            if name not in OVERLAY_OPS:
                raise ValidationError(
                    "Overlay operation is not in the allowlist",
                    {"allowed": list(OVERLAY_OPS), "received": name},
                )
            label = f"overlay op[{index}] {name}"
            if name == "register_evidence_kind":
                _reject_unknown_keys(op, {"op", "kind", "required_fields", "min_rank"}, label)
                kind = _nonempty_string(op.get("kind"), f"{label}.kind")
                if kind in kinds:
                    raise ValidationError(
                        "Evidence kind is already registered; redefinition could loosen it",
                        {"kind": kind},
                    )
                kinds.add(kind)
                required_fields = _string_list(op.get("required_fields", []), f"{label}.required_fields", allow_empty=True)
                allowed_fields = {"checker", "theory_base_hash", "formalization_rank"}
                unknown_fields = set(required_fields) - allowed_fields
                if unknown_fields:
                    raise ValidationError(
                        "Evidence kinds may only require the structural formalization fields",
                        {"allowed": sorted(allowed_fields), "unknown": sorted(unknown_fields)},
                    )
                min_rank = op.get("min_rank")
                if min_rank is not None and min_rank not in FORMALIZATION_RANKS:
                    raise ValidationError("Unknown formalization rank", {"received": min_rank})
                normalized_ops.append({"op": name, "kind": kind,
                                       "required_fields": required_fields, "min_rank": min_rank})
            elif name == "register_validator_hook":
                _reject_unknown_keys(op, {"op", "hook_id", "rank", "command", "timeout_seconds"}, label)
                hook_id = _safe_identifier(op.get("hook_id"), f"{label}.hook_id")
                if hook_id in hooks:
                    raise ValidationError("Validator hook is already registered", {"hook_id": hook_id})
                hooks.add(hook_id)
                rank = op.get("rank")
                if rank not in FORMALIZATION_RANKS:
                    raise ValidationError("Unknown formalization rank", {"received": rank})
                command = op.get("command")
                if (not isinstance(command, list) or not command
                        or any(not isinstance(item, str) or not item.strip() for item in command)):
                    raise ValidationError(f"{label}.command must be a non-empty argv list")
                timeout = op.get("timeout_seconds")
                if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                    raise ValidationError(f"{label}.timeout_seconds must be a positive integer")
                normalized_ops.append({"op": name, "hook_id": hook_id, "rank": rank,
                                       "command": list(command), "timeout_seconds": timeout})
            elif name == "register_sandbox":
                _reject_unknown_keys(op, {"op", "sandbox_id", "description", "mechanism_locator"}, label)
                sandbox_id = _safe_identifier(op.get("sandbox_id"), f"{label}.sandbox_id")
                if sandbox_id in boxes:
                    raise ValidationError("Sandbox is already registered", {"sandbox_id": sandbox_id})
                boxes.add(sandbox_id)
                normalized_ops.append({
                    "op": name,
                    "sandbox_id": sandbox_id,
                    "description": _nonempty_string(op.get("description"), f"{label}.description"),
                    "mechanism_locator": _nonempty_string(op.get("mechanism_locator"), f"{label}.mechanism_locator"),
                })
            elif name == "add_stall_class":
                _reject_unknown_keys(op, {"op", "signature", "label", "threshold"}, label)
                signature = _nonempty_string(op.get("signature"), f"{label}.signature").lower()
                if not HEX_256.fullmatch(signature):
                    raise ValidationError(f"{label}.signature must be a SHA-256 hex digest")
                if signature in stall_signatures:
                    raise ValidationError("Stall class already exists for this signature", {"signature": signature})
                stall_signatures.add(signature)
                threshold = op.get("threshold")
                if (not isinstance(threshold, int) or isinstance(threshold, bool)
                        or not 2 <= threshold <= SURGEON_FAULT_THRESHOLD):
                    raise ValidationError(
                        "A stall class may only demand diagnosis sooner",
                        {"allowed": [2, SURGEON_FAULT_THRESHOLD], "received": threshold},
                    )
                normalized_ops.append({"op": name, "signature": signature,
                                       "label": _nonempty_string(op.get("label"), f"{label}.label"),
                                       "threshold": threshold})
            elif name == "add_role_instructions":
                _reject_unknown_keys(op, {"op", "role", "mode", "instructions"}, label)
                role = op.get("role")
                if role not in ROLES_V4:
                    raise ValidationError("Unknown role for instruction overlay", {"allowed": sorted(ROLES_V4)})
                mode = op.get("mode")
                if mode is not None and mode not in KNOWN_MODES:
                    raise ValidationError("Unknown mode for instruction overlay", {"received": mode})
                normalized_ops.append({"op": name, "role": role, "mode": mode,
                                       "instructions": _string_list(op.get("instructions"), f"{label}.instructions")})
            elif name == "add_criterion":
                _reject_unknown_keys(op, {"op", "text", "min_formalization_rank", "rationale"}, label)
                text = _nonempty_string(op.get("text"), f"{label}.text")
                if text in criterion_texts:
                    raise ValidationError("Criterion text already exists", {"text": text})
                criterion_texts.add(text)
                rank = op.get("min_formalization_rank")
                if rank is not None and rank not in FORMALIZATION_RANKS:
                    raise ValidationError("Unknown formalization rank", {"received": rank})
                criterion_ranks[f"C{next_criterion}"] = rank
                next_criterion += 1
                normalized_ops.append({"op": name, "text": text, "min_formalization_rank": rank,
                                       "rationale": _nonempty_string(op.get("rationale"), f"{label}.rationale")})
            elif name in {"require_min_rank", "raise_criterion_rank"}:
                _reject_unknown_keys(op, {"op", "criterion_id", "min_formalization_rank"}, label)
                criterion_id = op.get("criterion_id")
                if criterion_id not in criterion_ranks:
                    raise ValidationError("Unknown criterion id", {"criterion_id": criterion_id})
                rank = op.get("min_formalization_rank")
                if rank not in FORMALIZATION_RANKS:
                    raise ValidationError("Unknown formalization rank", {"received": rank})
                current = criterion_ranks[criterion_id]
                if name == "require_min_rank" and current is not None:
                    raise ValidationError(
                        "require_min_rank only applies to a criterion with no rank; use raise_criterion_rank",
                        {"criterion_id": criterion_id, "current": current},
                    )
                if name == "raise_criterion_rank" and (
                    current is None or not rank_at_least(rank, current) or rank == current
                ):
                    raise ValidationError(
                        "An overlay may only raise a criterion's rank, never lower or restate it",
                        {"criterion_id": criterion_id, "current": current, "received": rank},
                    )
                criterion_ranks[criterion_id] = rank
                normalized_ops.append({"op": name, "criterion_id": criterion_id,
                                       "min_formalization_rank": rank})
            elif name == "pin_toolchain_hash":
                _reject_unknown_keys(op, {"op", "toolchain_id", "artifact_hash"}, label)
                toolchain_id = _safe_identifier(op.get("toolchain_id"), f"{label}.toolchain_id")
                if toolchain_id in pins:
                    raise ValidationError(
                        "Toolchain is already pinned; a re-pin could swap in a loosened toolchain",
                        {"toolchain_id": toolchain_id},
                    )
                pins.add(toolchain_id)
                artifact_hash = _nonempty_string(op.get("artifact_hash"), f"{label}.artifact_hash").lower()
                if not HEX_256.fullmatch(artifact_hash):
                    raise ValidationError(f"{label}.artifact_hash must be a SHA-256 hex digest")
                normalized_ops.append({"op": name, "toolchain_id": toolchain_id,
                                       "artifact_hash": artifact_hash})
        return {"schema_version": OVERLAY_SCHEMA_VERSION, "ops": normalized_ops}

    def _prepare_researcher_v3(
        self,
        submission: Dict[str, Any],
        normalized: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> None:
        basin = _nonempty_string(submission.get("basin"), "basin")
        basin_key = _normalized_strategy_value(basin)
        shift = submission.get("representation_shift")
        if shift is not None:
            shift = _nonempty_string(shift, "representation_shift")
        basin_entry = state.get("basins", {}).get(basin_key)
        if basin_entry and basin_entry["status"] == "closed" and not shift:
            raise ValidationError(
                "This basin is closed after repeated confirmed failures; re-entry requires a"
                " non-empty representation_shift stating how the problem is being re-represented",
                {"basin": basin, "failures": basin_entry["failures"]},
            )
        move = submission.get("move", "test")
        if move not in RESEARCH_MOVES:
            raise ValidationError("Unknown research move", {"allowed": list(RESEARCH_MOVES), "received": move})
        required_move = directive.get("required_move")
        if required_move and move != required_move:
            raise TransitionError(
                "The escalation ladder demands a specific move for this experiment",
                {"required_move": required_move, "received": move},
            )

        signature = _strategy_signature(normalized["strategy"])
        signature["representation_shift"] = _normalized_strategy_value(shift) if shift else ""
        if move == "replicate":
            replication_of = _nonempty_string(submission.get("replication_of"), "replication_of")
            target = next((attempt for attempt in state["attempts"] if attempt["id"] == replication_of), None)
            if target is None or target["role"] != "researcher":
                raise ValidationError(
                    "replication_of must reference a prior research attempt",
                    {"replication_of": replication_of},
                )
            if target.get("review", {}).get("verdict") != "validated_progress":
                raise ValidationError(
                    "Only a validated-progress attempt can be replicated",
                    {"replication_of": replication_of},
                )
            signature["replication_of"] = replication_of
            normalized["replication_of"] = replication_of
        elif submission.get("replication_of") is not None:
            raise ValidationError("replication_of requires move=replicate")
        normalized["strategy_fingerprint"] = object_hash(signature)
        normalized["basin"] = basin
        if shift:
            normalized["representation_shift"] = shift
        normalized["move"] = move

        candidate_id = submission.get("candidate_id")
        if candidate_id is not None:
            candidate_id = _nonempty_string(candidate_id, "candidate_id")
            candidate = next(
                (item for item in state.get("candidates", []) if item["id"] == candidate_id),
                None,
            )
            if candidate is None:
                raise ValidationError("Unknown candidate id", {"candidate_id": candidate_id})
            if candidate["status"] != "open":
                raise ValidationError(
                    "Candidate is not open; consumed candidates are never resurrected",
                    {"candidate_id": candidate_id, "status": candidate["status"]},
                )
            normalized["candidate_id"] = candidate_id

        registered = self._validate_assets_registered(submission.get("assets_registered"), state)
        if move == "survey" and not registered:
            raise ValidationError(
                "A survey move must register at least one reusable asset with a locator",
            )
        if registered:
            normalized["assets_registered"] = registered

        if move == "combine":
            combination = submission.get("combination")
            if not isinstance(combination, dict):
                raise ValidationError("A combine move requires a combination object")
            _reject_unknown_keys(combination, {"asset_ids"}, "combination")
            asset_ids = _string_list(combination.get("asset_ids"), "combination.asset_ids")
            if len(asset_ids) != 2:
                raise ValidationError("A combination cites exactly two distinct assets", {"received": asset_ids})
            known_assets = {asset["id"] for asset in state.get("assets", [])}
            unknown = set(asset_ids) - known_assets
            if unknown:
                raise ValidationError("Combination references unknown assets", {"unknown": sorted(unknown)})
            pair = sorted(asset_ids)
            used = {tuple(item) for item in state.get("combined_asset_pairs", [])}
            if tuple(pair) in used:
                raise ValidationError(
                    "This asset pair was already combined; a combine move must join a pair"
                    " no attempt has ever put together",
                    {"asset_ids": pair},
                )
            normalized["combination"] = {"asset_ids": pair}
        elif submission.get("combination") is not None:
            raise ValidationError("combination requires move=combine")

        if move == "barrier_probe":
            probe = submission.get("barrier_probe")
            if not isinstance(probe, dict):
                raise ValidationError("A barrier_probe move requires a barrier_probe object")
            _reject_unknown_keys(probe, {"barrier_id", "new_barrier", "pattern", "approach"}, "barrier_probe")
            pattern = probe.get("pattern")
            if pattern not in BARRIER_PROBE_PATTERNS:
                raise ValidationError(
                    "Barrier probe pattern must name how the wall is attacked",
                    {"allowed": list(BARRIER_PROBE_PATTERNS), "received": pattern},
                )
            normalized_probe: Dict[str, Any] = {
                "pattern": pattern,
                "approach": _nonempty_string(probe.get("approach"), "barrier_probe.approach"),
            }
            has_existing = probe.get("barrier_id") is not None
            has_new = probe.get("new_barrier") is not None
            if has_existing == has_new:
                raise ValidationError("A barrier probe cites exactly one of barrier_id or new_barrier")
            if has_existing:
                barrier_id = _nonempty_string(probe.get("barrier_id"), "barrier_probe.barrier_id")
                if barrier_id not in {barrier["id"] for barrier in state.get("barriers", [])}:
                    raise ValidationError("Unknown barrier id", {"barrier_id": barrier_id})
                normalized_probe["barrier_id"] = barrier_id
            else:
                new_barrier = probe.get("new_barrier")
                if not isinstance(new_barrier, dict):
                    raise ValidationError("new_barrier must be an object")
                _reject_unknown_keys(new_barrier, {"name", "statement"}, "new_barrier")
                name = _nonempty_string(new_barrier.get("name"), "new_barrier.name")
                if name in {barrier["name"] for barrier in state.get("barriers", [])}:
                    raise ValidationError("Barrier name already exists", {"name": name})
                normalized_probe["new_barrier"] = {
                    "name": name,
                    "statement": _nonempty_string(new_barrier.get("statement"), "new_barrier.statement"),
                }
            normalized["barrier_probe"] = normalized_probe
        elif submission.get("barrier_probe") is not None:
            raise ValidationError("barrier_probe requires move=barrier_probe")

    @staticmethod
    def _validate_assets_registered(assets: Any, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        if assets is None:
            return []
        if not isinstance(assets, list):
            raise ValidationError("assets_registered must be a list")
        known_names = {asset["name"] for asset in state.get("assets", [])}
        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(assets):
            if not isinstance(item, dict):
                raise ValidationError("Every registered asset must be an object", {"index": index})
            _reject_unknown_keys(item, {"name", "kind", "locator", "note"}, f"assets_registered[{index}]")
            name = _nonempty_string(item.get("name"), f"assets_registered[{index}].name")
            if name in known_names:
                raise ValidationError("Asset name already exists", {"name": name})
            known_names.add(name)
            normalized_item = {
                "name": name,
                "kind": _nonempty_string(item.get("kind"), f"assets_registered[{index}].kind"),
                "locator": _nonempty_string(item.get("locator"), f"assets_registered[{index}].locator"),
            }
            if "note" in item:
                normalized_item["note"] = _nonempty_string(item.get("note"), f"assets_registered[{index}].note")
            normalized.append(normalized_item)
        return normalized

    @staticmethod
    def _validate_wishes_declared(wishes: Any) -> List[Dict[str, Any]]:
        if wishes is None:
            return []
        if not isinstance(wishes, list):
            raise ValidationError("wishes_declared must be a list")
        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(wishes):
            if not isinstance(item, dict):
                raise ValidationError("Every wish must be an object", {"index": index})
            _reject_unknown_keys(item, {"statement", "would_open", "test", "recheck_after"}, f"wishes_declared[{index}]")
            recheck_after = _nonempty_string(item.get("recheck_after"), f"wishes_declared[{index}].recheck_after")
            _parse_time(recheck_after, f"wishes_declared[{index}].recheck_after")
            normalized.append({
                "statement": _nonempty_string(item.get("statement"), f"wishes_declared[{index}].statement"),
                "would_open": _nonempty_string(item.get("would_open"), f"wishes_declared[{index}].would_open"),
                "test": _nonempty_string(item.get("test"), f"wishes_declared[{index}].test"),
                "recheck_after": recheck_after,
            })
        return normalized

    @staticmethod
    def _validate_proposed_criteria(proposed: Any, state: Dict[str, Any], mode: str) -> List[Dict[str, Any]]:
        if proposed is None:
            return []
        if not isinstance(proposed, list):
            raise ValidationError("proposed_criteria must be a list")
        if not proposed:
            # an empty list is "nothing proposed"; the harness schema pins the field to [] outside decompose/fresh_replan
            return []
        if mode not in {"decompose", "fresh_replan"}:
            raise ValidationError("Only decompose or fresh_replan planning may propose additional criteria")
        existing_texts = {criterion["text"] for criterion in state["criteria"]}
        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(proposed):
            if not isinstance(item, dict):
                raise ValidationError("Every proposed criterion must be an object", {"index": index})
            _reject_unknown_keys(item, {"text", "rationale"}, f"proposed_criteria[{index}]")
            text = _nonempty_string(item.get("text"), f"proposed_criteria[{index}].text")
            if text in existing_texts:
                raise ValidationError("Proposed criterion duplicates an existing criterion", {"text": text})
            existing_texts.add(text)
            normalized.append({
                "text": text,
                "rationale": _nonempty_string(item.get("rationale"), f"proposed_criteria[{index}].rationale"),
            })
        return normalized

    def _normalize_evidence(
        self,
        raw_evidence: Any,
        state: Dict[str, Any],
        actor: Dict[str, str],
        role: str,
        attempt_id: str,
        new_contradiction_ids: Set[Optional[str]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        if not isinstance(raw_evidence, list):
            raise ValidationError("evidence must be a list")
        known_supports = {criterion["id"] for criterion in state["criteria"]}
        known_supports.update(contradiction["id"] for contradiction in state["contradictions"])
        known_supports.update(item for item in new_contradiction_ids if item)
        refs: Set[str] = set()
        normalized: List[Dict[str, Any]] = []
        ref_map: Dict[str, str] = {}
        next_number = len(state["evidence"]) + 1
        v4 = is_v4(state)
        allowed = {"ref", "kind", "quality", "claim", "locator", "method", "fingerprint", "independence_key", "supports"}
        if v4:
            allowed = allowed | {"formalization_rank", "checker", "theory_base_hash"}
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, dict):
                raise ValidationError("Every evidence entry must be an object", {"index": index})
            _reject_unknown_keys(item, allowed, f"evidence[{index}]")
            ref = _safe_identifier(item.get("ref"), f"evidence[{index}].ref")
            if ref in refs:
                raise ValidationError("Evidence refs must be unique within an attempt", {"ref": ref})
            refs.add(ref)
            fingerprint = _nonempty_string(item.get("fingerprint"), f"evidence[{index}].fingerprint").lower()
            if not HEX_256.fullmatch(fingerprint):
                raise ValidationError("Evidence fingerprint must be a lowercase SHA-256 hex digest", {"ref": ref})
            quality = item.get("quality")
            if quality not in EVIDENCE_QUALITIES:
                raise ValidationError("Evidence quality must be direct or indirect", {"ref": ref})
            supports = _string_list(item.get("supports"), f"evidence[{index}].supports")
            unknown_supports = set(supports) - known_supports
            if unknown_supports:
                raise ValidationError("Evidence references unknown criteria or contradictions", {"unknown": sorted(unknown_supports)})
            evidence_id = f"E{next_number + index:06d}"
            ref_map[ref] = evidence_id
            entry = {
                "id": evidence_id,
                "at": utc_now(),
                "kind": _nonempty_string(item.get("kind"), f"evidence[{index}].kind"),
                "quality": quality,
                "claim": _nonempty_string(item.get("claim"), f"evidence[{index}].claim"),
                "locator": _nonempty_string(item.get("locator"), f"evidence[{index}].locator"),
                "method": _nonempty_string(item.get("method"), f"evidence[{index}].method"),
                "fingerprint": fingerprint,
                "independence_key": _nonempty_string(item.get("independence_key"), f"evidence[{index}].independence_key"),
                "supports": supports,
                "producer_attempt_id": attempt_id,
                "producer_role": role,
                "actor": actor,
            }
            if v4:
                self._apply_formalization_fields(entry, item, state, ref)
            normalized.append(entry)
        return normalized, ref_map

    def _apply_formalization_fields(self, entry: Dict[str, Any], item: Dict[str, Any],
                                    state: Dict[str, Any], ref: str) -> None:
        """Validate and attach the 4.0 evidence fields: rank, checker record, base hash.

        The checker record is attested by the submitter, like every fingerprint;
        the engine enforces its structure and internal consistency, never its
        truth. A rank at or above the checked floor is a claim that a mechanical
        check accepted the artifact, so it must carry an accepting record whose
        artifact hash is the evidence fingerprint itself.
        """

        rank = item.get("formalization_rank")
        if rank is not None and rank not in FORMALIZATION_RANKS:
            raise ValidationError(
                "Unknown formalization rank",
                {"ref": ref, "allowed": list(FORMALIZATION_RANKS)},
            )
        checker = item.get("checker")
        if checker is not None:
            if not isinstance(checker, dict):
                raise ValidationError("Evidence checker must be an object", {"ref": ref})
            allowed_checker = {"checker_id", "checker_version", "accepted", "artifact_hash", "log_hash", "toolchain_hash"}
            if is_v5(state):
                allowed_checker = allowed_checker | {"attestation"}
            _reject_unknown_keys(checker, allowed_checker, f"evidence checker ({ref})")
            normalized_checker: Dict[str, Any] = {
                "checker_id": _safe_identifier(checker.get("checker_id"), "checker.checker_id"),
                "checker_version": _nonempty_string(checker.get("checker_version"), "checker.checker_version"),
            }
            if not isinstance(checker.get("accepted"), bool):
                raise ValidationError("checker.accepted must be a boolean", {"ref": ref})
            normalized_checker["accepted"] = checker["accepted"]
            artifact_hash = _nonempty_string(checker.get("artifact_hash"), "checker.artifact_hash").lower()
            if not HEX_256.fullmatch(artifact_hash):
                raise ValidationError("checker.artifact_hash must be a SHA-256 hex digest", {"ref": ref})
            normalized_checker["artifact_hash"] = artifact_hash
            for optional in ("log_hash", "toolchain_hash"):
                if optional in checker:
                    value = _nonempty_string(checker.get(optional), f"checker.{optional}").lower()
                    if not HEX_256.fullmatch(value):
                        raise ValidationError(f"checker.{optional} must be a SHA-256 hex digest", {"ref": ref})
                    normalized_checker[optional] = value
            if "attestation" in checker:
                value = _nonempty_string(checker.get("attestation"), "checker.attestation").lower()
                if not HEX_256.fullmatch(value):
                    raise ValidationError("checker.attestation must be an HMAC-SHA256 hex digest", {"ref": ref})
                normalized_checker["attestation"] = value
            if entry["fingerprint"] != artifact_hash:
                raise ValidationError(
                    "Evidence fingerprint must equal the checker artifact hash",
                    {"ref": ref},
                )
            if checker["accepted"] is not True and rank is not None:
                raise ValidationError(
                    "Evidence whose checker did not accept cannot carry a formalization rank",
                    {"ref": ref},
                )
            entry["checker"] = normalized_checker
        if rank is not None and rank_at_least(rank, CHECKED_RANK_FLOOR):
            if checker is None or checker.get("accepted") is not True:
                raise ValidationError(
                    "A checked formalization rank requires an accepting checker record",
                    {"ref": ref, "rank": rank, "checked_floor": CHECKED_RANK_FLOOR},
                )
        if rank is not None:
            entry["formalization_rank"] = rank
        theory_base_hash = item.get("theory_base_hash")
        if theory_base_hash is not None:
            value = _nonempty_string(theory_base_hash, "theory_base_hash").lower()
            if not HEX_256.fullmatch(value):
                raise ValidationError("theory_base_hash must be a SHA-256 hex digest", {"ref": ref})
            entry["theory_base_hash"] = value
        if "checker" in entry:
            self._enforce_checker_attestation(entry["checker"], state, ref)
        registry_entry = state.get("evidence_kind_registry", {}).get(entry["kind"])
        if registry_entry:
            missing = [field for field in registry_entry["required_fields"] if field not in entry]
            if missing:
                raise ValidationError(
                    "Evidence of a registered kind is missing its required fields",
                    {"ref": ref, "kind": entry["kind"], "missing": missing},
                )
            if registry_entry.get("min_rank") is not None and not rank_satisfies(entry, registry_entry["min_rank"]):
                raise ValidationError(
                    "Evidence of a registered kind must meet the kind's minimum rank",
                    {"ref": ref, "kind": entry["kind"], "min_rank": registry_entry["min_rank"]},
                )
        if "checker" in entry:
            pin = state.get("toolchain_pins", {}).get(entry["checker"]["checker_id"])
            if pin and entry["checker"].get("toolchain_hash") != pin["artifact_hash"]:
                raise ValidationError(
                    "Checker toolchain does not match the pinned hash",
                    {"ref": ref, "checker_id": entry["checker"]["checker_id"]},
                )

    def _enforce_checker_attestation(self, checker: Dict[str, Any],
                                     state: Dict[str, Any], ref: str) -> None:
        """When the workspace requires attestations, every checker record must
        carry a valid HMAC from this machine's key (submission-time gate; the
        attested value itself is audited into the chain)."""

        if not is_v5(state):
            return
        requirement = self._load_config().get("checker_attestation")
        if not isinstance(requirement, dict) or requirement.get("require") is not True:
            return
        key_path = self.workspace / CHECKER_KEY_FILE
        try:
            key_hex = key_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise ValidationError(
                "Checker attestation is required but the workspace has no attestation key",
                {"key_file": CHECKER_KEY_FILE},
            )
        if not HEX_256.fullmatch(key_hex):
            raise ValidationError(
                "The workspace attestation key is not a 64-hex secret",
                {"key_file": CHECKER_KEY_FILE},
            )
        provided = checker.get("attestation")
        expected = checker_attestation_mac(key_hex, checker)
        if not provided or not hmac.compare_digest(provided, expected):
            raise ValidationError(
                "Checker attestation is missing or invalid: obtain the verdict through"
                " adv-loop validate on this machine",
                {"ref": ref, "checker_id": checker.get("checker_id")},
            )

    def _normalize_criterion_updates(
        self,
        updates: Any,
        state: Dict[str, Any],
        evidence: Dict[str, Dict[str, Any]],
        ref_map: Dict[str, str],
        role: str,
    ) -> List[Dict[str, Any]]:
        if not isinstance(updates, list):
            raise ValidationError("criterion_updates must be a list")
        if role in {"planner", "verifier", "synthesizer"} and updates:
            raise ValidationError(f"{role} attempts cannot directly update criterion claim status")
        criteria = {criterion["id"]: criterion for criterion in state["criteria"]}
        known = set(criteria)
        seen: Set[str] = set()
        normalized: List[Dict[str, Any]] = []
        for item in updates:
            if not isinstance(item, dict):
                raise ValidationError("Every criterion update must be an object")
            _reject_unknown_keys(item, {"id", "status", "evidence_refs", "evidence_ids", "reason"}, "criterion update")
            criterion_id = item.get("id")
            if criterion_id not in known or criterion_id in seen:
                raise ValidationError("Criterion update id is unknown or repeated", {"id": criterion_id})
            seen.add(criterion_id)
            status = item.get("status")
            if status not in CRITERION_STATUSES:
                raise ValidationError("Unknown criterion status", {"status": status})
            if role == "researcher" and status == "failed_verification":
                raise ValidationError("Only verification or critique can mark failed_verification")
            evidence_ids = self._resolve_evidence_references(item, ref_map)
            self._require_evidence_ids(evidence_ids, {"evidence": evidence})
            if status == "satisfied":
                direct = [
                    evidence[evidence_id]
                    for evidence_id in evidence_ids
                    if evidence[evidence_id]["quality"] == "direct" and criterion_id in evidence[evidence_id]["supports"]
                ]
                if not direct:
                    raise ValidationError(
                        "A satisfied criterion requires direct evidence that explicitly supports it",
                        {"criterion_id": criterion_id},
                    )
                minimum = criteria[criterion_id].get("min_formalization_rank")
                if minimum is not None and not any(rank_satisfies(item, minimum) for item in direct):
                    raise ValidationError(
                        "A rank-gated criterion requires direct supporting evidence at or above its minimum rank",
                        {"criterion_id": criterion_id, "min_formalization_rank": minimum},
                    )
            normalized.append({
                "id": criterion_id,
                "status": status,
                "evidence_ids": evidence_ids,
                "reason": _nonempty_string(item.get("reason"), "criterion update reason"),
            })
        return normalized

    def _normalize_contradictions(
        self,
        contradictions: Any,
        state: Dict[str, Any],
        evidence: Dict[str, Dict[str, Any]],
        ref_map: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        if not isinstance(contradictions, list):
            raise ValidationError("contradictions must be a list")
        known = {item["id"] for item in state["contradictions"]}
        normalized: List[Dict[str, Any]] = []
        for item in contradictions:
            if not isinstance(item, dict):
                raise ValidationError("Every contradiction must be an object")
            _reject_unknown_keys(item, {"id", "claim", "severity", "evidence_refs", "evidence_ids"}, "contradiction")
            contradiction_id = _safe_identifier(item.get("id"), "contradiction id")
            if contradiction_id in known:
                raise ValidationError("Contradiction id already exists", {"id": contradiction_id})
            known.add(contradiction_id)
            severity = item.get("severity")
            if severity not in {"critical", "noncritical"}:
                raise ValidationError("Contradiction severity must be critical or noncritical")
            evidence_ids = self._resolve_evidence_references(item, ref_map)
            self._require_evidence_ids(evidence_ids, {"evidence": evidence})
            if not evidence_ids:
                raise ValidationError("A contradiction requires evidence", {"id": contradiction_id})
            normalized.append({
                "id": contradiction_id,
                "claim": _nonempty_string(item.get("claim"), "contradiction claim"),
                "severity": severity,
                "status": "open",
                "evidence_ids": evidence_ids,
                "resolution": None,
                "resolution_evidence_ids": [],
            })
        return normalized

    def _normalize_resolutions(
        self,
        resolutions: Any,
        state: Dict[str, Any],
        evidence: Dict[str, Dict[str, Any]],
        ref_map: Dict[str, str],
        current_evidence_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        if not isinstance(resolutions, list):
            raise ValidationError("contradiction_resolutions must be a list")
        open_ids = {item["id"] for item in state["contradictions"] if item["status"] == "open"}
        normalized = []
        for item in resolutions:
            if not isinstance(item, dict):
                raise ValidationError("Every contradiction resolution must be an object")
            _reject_unknown_keys(item, {"id", "resolution", "evidence_refs", "evidence_ids"}, "contradiction resolution")
            contradiction_id = item.get("id")
            if contradiction_id not in open_ids:
                raise ValidationError("Resolution references a contradiction that is not open", {"id": contradiction_id})
            evidence_ids = self._resolve_evidence_references(item, ref_map)
            self._require_evidence_ids(evidence_ids, {"evidence": evidence})
            if not set(evidence_ids).intersection(current_evidence_ids):
                raise ValidationError("Resolving a contradiction requires new evidence from this attempt")
            normalized.append({
                "id": contradiction_id,
                "resolution": _nonempty_string(item.get("resolution"), "resolution"),
                "evidence_ids": evidence_ids,
            })
        return normalized

    @staticmethod
    def _normalize_decisions(decisions: Any) -> List[Dict[str, Any]]:
        if not isinstance(decisions, list):
            raise ValidationError("decisions must be a list")
        normalized = []
        for item in decisions:
            if not isinstance(item, dict):
                raise ValidationError("Every decision must be an object")
            _reject_unknown_keys(item, {"decision", "rationale", "rejected_alternatives"}, "decision")
            normalized.append({
                "decision": _nonempty_string(item.get("decision"), "decision"),
                "rationale": _nonempty_string(item.get("rationale"), "decision rationale"),
                "rejected_alternatives": _string_list(item.get("rejected_alternatives", []), "rejected alternatives", allow_empty=True),
            })
        return normalized

    @staticmethod
    def _validate_plan(plan: Any, mode: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not isinstance(plan, dict):
            raise ValidationError("Planner attempt requires a plan object")
        v3 = bool(state) and is_v3(state)
        allowed = {"assumptions", "subproblems", "candidate_experiments", "falsification_tests", "rejected_assumptions"}
        if v3:
            allowed = allowed | {"lessons_addressed"}
        _reject_unknown_keys(plan, allowed, "plan")
        normalized = {
            "assumptions": _string_list(plan.get("assumptions"), "plan.assumptions"),
            "subproblems": _string_list(plan.get("subproblems"), "plan.subproblems"),
            "candidate_experiments": _string_list(plan.get("candidate_experiments"), "plan.candidate_experiments"),
            "falsification_tests": _string_list(plan.get("falsification_tests"), "plan.falsification_tests"),
            "rejected_assumptions": _string_list(plan.get("rejected_assumptions", []), "plan.rejected_assumptions", allow_empty=True),
        }
        if mode == "decompose" and len(normalized["subproblems"]) < 2:
            raise ValidationError("Decomposition must create at least two independently testable subproblems")
        if mode == "fresh_replan" and not normalized["rejected_assumptions"]:
            raise ValidationError("Fresh replanning must explicitly reject at least one stale assumption")
        if v3 and mode == "fresh_replan" and state.get("lessons"):
            addressed = plan.get("lessons_addressed")
            if not isinstance(addressed, list):
                raise ValidationError(
                    "Fresh replanning must address every recorded lesson item by item",
                    {"lesson_ids": [lesson["id"] for lesson in state["lessons"]]},
                )
            normalized_addressed = []
            covered: Set[str] = set()
            for index, item in enumerate(addressed):
                if not isinstance(item, dict):
                    raise ValidationError("Every lessons_addressed entry must be an object", {"index": index})
                _reject_unknown_keys(item, {"lesson_id", "response"}, f"lessons_addressed[{index}]")
                lesson_id = item.get("lesson_id")
                if lesson_id in covered:
                    raise ValidationError("Lesson addressed twice", {"lesson_id": lesson_id})
                covered.add(lesson_id)
                normalized_addressed.append({
                    "lesson_id": lesson_id,
                    "response": _nonempty_string(item.get("response"), f"lessons_addressed[{index}].response"),
                })
            missing = {lesson["id"] for lesson in state["lessons"]} - covered
            unknown = covered - {lesson["id"] for lesson in state["lessons"]}
            if missing or unknown:
                raise ValidationError(
                    "lessons_addressed must cover exactly the recorded lessons",
                    {"missing": sorted(missing), "unknown": sorted(unknown)},
                )
            normalized["lessons_addressed"] = normalized_addressed
        return normalized

    def _validate_researcher(
        self,
        submission: Dict[str, Any],
        normalized: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> None:
        v4 = is_v4(state)
        if submission.get("plan_id") != state["active_plan_id"]:
            raise TransitionError("Research attempt must execute the active plan", {"active_plan_id": state["active_plan_id"]})
        targets = _string_list(submission.get("criterion_targets"), "criterion_targets")
        known = {criterion["id"] for criterion in state["criteria"]}
        if set(targets) - known:
            raise ValidationError("Research attempt targets unknown criteria", {"unknown": sorted(set(targets) - known)})
        fingerprint = normalized["strategy_fingerprint"]
        research_attempts = [attempt for attempt in state["attempts"] if attempt["role"] == "researcher"]
        if fingerprint in {attempt["strategy_fingerprint"] for attempt in research_attempts}:
            raise ValidationError("Research strategy was already attempted", {"strategy_fingerprint": fingerprint})
        required_changes = directive["required_strategy_dimension_changes"]
        # A replication is meant to repeat a strategy, so it is never asked to diversify away from one.
        widen = v4 and submission.get("move", "test") != "replicate"
        failed = [
            attempt for attempt in research_attempts
            if attempt.get("review", {}).get("verdict") != "validated_progress"
            or (widen and not _targets_closed(state, attempt.get("criterion_targets", [])))
        ][-3:]
        for previous in failed:
            changes = _strategy_distance(normalized["strategy"], previous["strategy"])
            if changes < required_changes:
                raise ValidationError(
                    "Strategy does not diversify enough from a recent failed attempt",
                    {"previous_attempt": previous["id"], "required_changes": required_changes, "actual_changes": changes},
                )

    def _validate_critic(
        self,
        submission: Dict[str, Any],
        normalized: Dict[str, Any],
        state: Dict[str, Any],
        directive: Dict[str, Any],
    ) -> None:
        mode = normalized["mode"]
        v3 = is_v3(state)
        v4 = is_v4(state)
        if mode == "attempt_review":
            assessment = submission.get("assessment")
            if not isinstance(assessment, dict):
                raise ValidationError("Attempt review requires an assessment object")
            allowed_assessment = {"target_attempt_id", "verdict", "reasons", "uncertainty"}
            if v3:
                allowed_assessment = allowed_assessment | {"candidate_causes", "control_check"}
            if v4:
                allowed_assessment = allowed_assessment | {"harness_gap"}
            _reject_unknown_keys(assessment, allowed_assessment, "assessment")
            target_id = assessment.get("target_attempt_id")
            if target_id != directive["target_attempt_id"]:
                raise TransitionError("Critic assessment targets the wrong attempt", {"expected": directive["target_attempt_id"]})
            verdict = assessment.get("verdict")
            if verdict not in CRITIC_VERDICTS:
                raise ValidationError("Unknown critic verdict", {"allowed": sorted(CRITIC_VERDICTS)})
            target = next(attempt for attempt in state["attempts"] if attempt["id"] == target_id)
            if normalized["actor"]["context_id"] == target["actor"]["context_id"]:
                raise ValidationError("Critic must use a fresh context from the research attempt")
            if verdict == "validated_progress":
                proof_of_progress = any((
                    target["evidence"],
                    target["criterion_updates"],
                    target["contradictions"],
                    target["contradiction_resolutions"],
                ))
                if target["outcome"] != "progress" or not proof_of_progress:
                    raise ValidationError(
                        "Validated progress requires a progress outcome and a recorded evidence-bearing state change",
                        {"target_attempt_id": target_id},
                    )
            normalized["assessment"] = {
                "target_attempt_id": target_id,
                "verdict": verdict,
                "reasons": _string_list(assessment.get("reasons"), "assessment.reasons"),
                "uncertainty": _nonempty_string(assessment.get("uncertainty"), "assessment.uncertainty"),
            }
            if v4 and "harness_gap" in assessment:
                if assessment["harness_gap"] is not True:
                    raise ValidationError(
                        "harness_gap is a structural flag: present means the literal true",
                        {"received": assessment["harness_gap"]},
                    )
                normalized["assessment"]["harness_gap"] = True
            if v3:
                lens = submission.get("lens")
                if lens != directive.get("lens"):
                    raise ValidationError(
                        "The review must be conducted through the assigned lens",
                        {"expected": directive.get("lens"), "received": lens},
                    )
                normalized["lens"] = lens
                if lens == "proves_too_much" and state.get("controls"):
                    control_check = assessment.get("control_check")
                    if not isinstance(control_check, dict):
                        raise ValidationError(
                            "A proves_too_much review must run the claim against a negative control",
                            {"controls": [item["id"] for item in state["controls"]]},
                        )
                    _reject_unknown_keys(control_check, {"control_id", "outcome", "note"}, "control_check")
                    control_id = control_check.get("control_id")
                    if control_id not in {item["id"] for item in state["controls"]}:
                        raise ValidationError("control_check references an unknown control", {"control_id": control_id})
                    outcome = control_check.get("outcome")
                    if outcome not in {"rejects_control", "endorses_control", "not_applicable"}:
                        raise ValidationError(
                            "control_check outcome must be rejects_control, endorses_control, or not_applicable",
                            {"received": outcome},
                        )
                    if outcome == "endorses_control" and verdict not in {"invalid", "no_progress"}:
                        raise ValidationError(
                            "A mechanism that endorses a negative control proves too much and cannot be"
                            " validated or left mixed",
                            {"control_id": control_id},
                        )
                    normalized["assessment"]["control_check"] = {
                        "control_id": control_id,
                        "outcome": outcome,
                        "note": _nonempty_string(control_check.get("note"), "control_check.note"),
                    }
                if verdict != "validated_progress" and multi_cause_required_for(state, target):
                    causes = assessment.get("candidate_causes")
                    if not isinstance(causes, list) or len(causes) < 2:
                        raise ValidationError(
                            "This criterion has failed repeatedly: the review must record at least two"
                            " distinct candidate causes, each with a discriminating test",
                        )
                    normalized_causes: List[Dict[str, str]] = []
                    seen_causes: Set[str] = set()
                    for index, cause in enumerate(causes):
                        if not isinstance(cause, dict):
                            raise ValidationError("Every candidate cause must be an object", {"index": index})
                        _reject_unknown_keys(cause, {"cause", "discriminating_test"}, f"candidate_causes[{index}]")
                        cause_text = _nonempty_string(cause.get("cause"), f"candidate_causes[{index}].cause")
                        if cause_text in seen_causes:
                            raise ValidationError("Candidate causes must be distinct", {"cause": cause_text})
                        seen_causes.add(cause_text)
                        normalized_causes.append({
                            "cause": cause_text,
                            "discriminating_test": _nonempty_string(
                                cause.get("discriminating_test"), f"candidate_causes[{index}].discriminating_test"
                            ),
                        })
                    normalized["assessment"]["candidate_causes"] = normalized_causes
                elif assessment.get("candidate_causes"):
                    normalized_causes = []
                    for index, cause in enumerate(assessment["candidate_causes"]):
                        if not isinstance(cause, dict):
                            raise ValidationError("Every candidate cause must be an object", {"index": index})
                        _reject_unknown_keys(cause, {"cause", "discriminating_test"}, f"candidate_causes[{index}]")
                        normalized_causes.append({
                            "cause": _nonempty_string(cause.get("cause"), f"candidate_causes[{index}].cause"),
                            "discriminating_test": _nonempty_string(
                                cause.get("discriminating_test"), f"candidate_causes[{index}].discriminating_test"
                            ),
                        })
                    normalized["assessment"]["candidate_causes"] = normalized_causes
        elif mode == "contradiction_search":
            audit = submission.get("contradiction_search")
            if not isinstance(audit, dict):
                raise ValidationError("Contradiction search requires an audit object")
            _reject_unknown_keys(audit, {"assumptions_checked", "disconfirming_queries", "conclusion"}, "contradiction search")
            normalized["contradiction_search"] = {
                "assumptions_checked": _string_list(audit.get("assumptions_checked"), "assumptions_checked"),
                "disconfirming_queries": _string_list(audit.get("disconfirming_queries"), "disconfirming_queries"),
                "conclusion": _nonempty_string(audit.get("conclusion"), "contradiction search conclusion"),
            }
        elif mode == "blocker_audit":
            audit = submission.get("blocker_audit")
            if not isinstance(audit, dict):
                raise ValidationError("Blocker audit requires a blocker_audit object")
            _reject_unknown_keys(
                audit,
                {
                    "dependency", "dependency_evidence_ids", "safe_alternative_attempt_ids",
                    "unsafe_alternatives_rejected", "exhaustion_reason", "human_action",
                    "safe_alternatives_exhausted",
                },
                "blocker audit",
            )
            attempt_ids = _string_list(audit.get("safe_alternative_attempt_ids"), "safe_alternative_attempt_ids")
            if len(set(attempt_ids)) < 3:
                raise ValidationError("Blocker audit requires at least three distinct safe alternative attempts")
            attempts = {attempt["id"]: attempt for attempt in state["attempts"]}
            selected = []
            for attempt_id in attempt_ids:
                attempted = attempts.get(attempt_id)
                if not attempted or attempted["role"] != "researcher":
                    raise ValidationError("Blocker audit alternatives must reference research attempts", {"attempt_id": attempt_id})
                if attempted.get("review", {}).get("verdict") == "validated_progress":
                    raise ValidationError("Validated progress is not an exhausted alternative", {"attempt_id": attempt_id})
                selected.append(attempted)
            if _varied_strategy_dimensions(selected) < 2:
                raise ValidationError("Blocker audit alternatives must vary at least two strategy dimensions")
            dependency_evidence_ids = _string_list(
                audit.get("dependency_evidence_ids"), "dependency_evidence_ids"
            )
            self._require_evidence_ids(dependency_evidence_ids, state)
            if not any(state["evidence"][item]["quality"] == "direct" for item in dependency_evidence_ids):
                raise ValidationError("Blocker audit requires direct dependency evidence")
            if audit.get("safe_alternatives_exhausted") is not True:
                raise ValidationError("Blocker audit must explicitly establish that safe alternatives are exhausted")
            normalized["blocker_audit"] = {
                "dependency": _nonempty_string(audit.get("dependency"), "dependency"),
                "dependency_evidence_ids": dependency_evidence_ids,
                "safe_alternative_attempt_ids": attempt_ids,
                "unsafe_alternatives_rejected": _string_list(audit.get("unsafe_alternatives_rejected", []), "unsafe alternatives", allow_empty=True),
                "exhaustion_reason": _nonempty_string(audit.get("exhaustion_reason"), "exhaustion reason"),
                "human_action": _nonempty_string(audit.get("human_action"), "human action"),
                "safe_alternatives_exhausted": True,
            }

    def _validate_verification(
        self,
        results: Any,
        state: Dict[str, Any],
        current_evidence: List[Dict[str, Any]],
        ref_map: Dict[str, str],
        actor: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        if not isinstance(results, list):
            raise ValidationError("Verifier requires verification_results")
        expected = {criterion["id"] for criterion in state["criteria"]}
        if {item.get("criterion_id") for item in results if isinstance(item, dict)} != expected or len(results) != len(expected):
            raise ValidationError("Verifier must return exactly one result for every criterion", {"expected": sorted(expected)})
        current = {item["id"]: item for item in current_evidence}
        normalized = []
        criteria = {criterion["id"]: criterion for criterion in state["criteria"]}
        for item in results:
            if not isinstance(item, dict):
                raise ValidationError("Every verification result must be an object")
            _reject_unknown_keys(item, {"criterion_id", "verdict", "evidence_refs", "method", "observation"}, "verification result")
            criterion_id = item["criterion_id"]
            verdict = item.get("verdict")
            if verdict not in {"pass", "fail"}:
                raise ValidationError("Verification verdict must be pass or fail")
            evidence_ids = [ref_map[ref] for ref in _string_list(item.get("evidence_refs"), "verification evidence refs") if ref in ref_map]
            if len(evidence_ids) != len(item.get("evidence_refs", [])):
                raise ValidationError("Verification result must reference evidence from the current verifier attempt")
            primary = [state["evidence"][evidence_id] for evidence_id in criteria[criterion_id]["evidence_ids"]]
            primary_keys = {evidence["independence_key"] for evidence in primary}
            primary_fingerprints = {evidence["fingerprint"] for evidence in primary}
            primary_contexts = {evidence["actor"]["context_id"] for evidence in primary}
            if actor["context_id"] in primary_contexts:
                raise ValidationError("Verifier context must be independent from primary evidence", {"criterion_id": criterion_id})
            minimum = criteria[criterion_id].get("min_formalization_rank")
            for evidence_id in evidence_ids:
                evidence = current[evidence_id]
                if evidence["quality"] != "direct" or criterion_id not in evidence["supports"]:
                    raise ValidationError("Verification requires direct evidence supporting its criterion", {"evidence_id": evidence_id})
                if evidence["independence_key"] in primary_keys or evidence["fingerprint"] in primary_fingerprints:
                    raise ValidationError("Verification evidence is not independent from primary evidence", {"evidence_id": evidence_id})
                if minimum is not None and not rank_satisfies(evidence, minimum):
                    raise ValidationError(
                        "Verification evidence for a rank-gated criterion must meet its minimum rank",
                        {"evidence_id": evidence_id, "min_formalization_rank": minimum},
                    )
            normalized.append({
                "criterion_id": criterion_id,
                "verdict": verdict,
                "evidence_ids": evidence_ids,
                "method": _nonempty_string(item.get("method"), "verification method"),
                "observation": _nonempty_string(item.get("observation"), "verification observation"),
            })
        return normalized

    def _validate_report(self, report: Any, state: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(report, dict):
            raise ValidationError("Synthesizer requires a report object")
        allowed = {
            "summary", "criterion_results", "facts", "inferences", "uncertainties", "limitations",
            "unresolved_noncritical_contradiction_ids", "next_actions",
        }
        _reject_unknown_keys(report, allowed, "report")
        criterion_results = report.get("criterion_results")
        if not isinstance(criterion_results, list):
            raise ValidationError("report.criterion_results must be a list")
        expected = {criterion["id"] for criterion in state["criteria"]}
        if {item.get("criterion_id") for item in criterion_results if isinstance(item, dict)} != expected or len(criterion_results) != len(expected):
            raise ValidationError("Report must contain exactly one result for every criterion")
        criteria = {criterion["id"]: criterion for criterion in state["criteria"]}
        normalized_results = []
        for item in criterion_results:
            if not isinstance(item, dict):
                raise ValidationError("Every report criterion result must be an object")
            _reject_unknown_keys(item, {"criterion_id", "conclusion", "primary_evidence_ids", "verification_evidence_ids"}, "report criterion result")
            criterion = criteria[item["criterion_id"]]
            primary = _string_list(item.get("primary_evidence_ids"), "primary_evidence_ids")
            verification = _string_list(item.get("verification_evidence_ids"), "verification_evidence_ids")
            if set(primary) != set(criterion["evidence_ids"]) or set(verification) != set(criterion["verification"]["evidence_ids"]):
                raise ValidationError("Report evidence map must exactly match the verified state", {"criterion_id": criterion["id"]})
            normalized_results.append({
                "criterion_id": criterion["id"],
                "conclusion": _nonempty_string(item.get("conclusion"), "criterion conclusion"),
                "primary_evidence_ids": primary,
                "verification_evidence_ids": verification,
            })

        facts = self._validate_report_claims(report.get("facts"), state, inference=False)
        inferences = self._validate_report_claims(report.get("inferences", []), state, inference=True)
        limitations = _string_list(report.get("limitations"), "report.limitations")
        unresolved = _string_list(
            report.get("unresolved_noncritical_contradiction_ids", []),
            "report.unresolved_noncritical_contradiction_ids",
            allow_empty=True,
        )
        expected_unresolved = {
            contradiction["id"]
            for contradiction in state["contradictions"]
            if contradiction["status"] == "open" and contradiction["severity"] == "noncritical"
        }
        if set(unresolved) != expected_unresolved:
            raise ValidationError(
                "Report must disclose the exact set of unresolved noncritical contradictions",
                {"expected": sorted(expected_unresolved)},
            )
        return {
            "summary": _nonempty_string(report.get("summary"), "report.summary"),
            "criterion_results": normalized_results,
            "facts": facts,
            "inferences": inferences,
            "uncertainties": _string_list(report.get("uncertainties", []), "report.uncertainties", allow_empty=True),
            "limitations": limitations,
            "unresolved_noncritical_contradiction_ids": unresolved,
            "next_actions": _string_list(report.get("next_actions", []), "report.next_actions", allow_empty=True),
        }

    def _validate_report_claims(self, claims: Any, state: Dict[str, Any], inference: bool) -> List[Dict[str, Any]]:
        if not isinstance(claims, list) or (not inference and not claims):
            raise ValidationError("Report facts must be a non-empty list" if not inference else "Report inferences must be a list")
        normalized = []
        for item in claims:
            if not isinstance(item, dict):
                raise ValidationError("Every report claim must be an object")
            if inference:
                _reject_unknown_keys(item, {"claim", "basis_evidence_ids", "confidence"}, "report inference")
                ids = _string_list(item.get("basis_evidence_ids"), "basis_evidence_ids")
                confidence = item.get("confidence")
                if confidence not in {"low", "medium", "high"}:
                    raise ValidationError("Inference confidence must be low, medium, or high")
                normalized_item = {"claim": _nonempty_string(item.get("claim"), "inference claim"), "basis_evidence_ids": ids, "confidence": confidence}
            else:
                _reject_unknown_keys(item, {"claim", "evidence_ids"}, "report fact")
                ids = _string_list(item.get("evidence_ids"), "fact evidence_ids")
                if not any(state["evidence"].get(evidence_id, {}).get("quality") == "direct" for evidence_id in ids):
                    raise ValidationError("A report fact requires at least one direct evidence item")
                normalized_item = {"claim": _nonempty_string(item.get("claim"), "fact claim"), "evidence_ids": ids}
            self._require_evidence_ids(ids, state)
            normalized.append(normalized_item)
        return normalized

    def _validate_blocked_decision(self, decision: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        v3 = is_v3(state)
        allowed = {"request_id", "dependency", "attempt_ids", "evidence_ids", "alternatives_exhausted", "human_action"}
        if v3:
            allowed = allowed | {"retest"}
        _reject_unknown_keys(decision, allowed, "blocked decision")
        if not _blocker_audit_completed(state):
            raise TransitionError("Blocked gate requires the scheduled blocker audit to complete")
        if state["pending_critique"] is not None:
            raise TransitionError("Blocked gate cannot skip the pending critic review")
        if v3 and state.get("pending_triage") is not None:
            raise TransitionError("Blocked gate cannot skip the pending seed triage")
        if v3:
            demand = escalation_demand(state)
            if demand is not None:
                raise TransitionError(
                    "Blocked gate refused: the escalation ladder still owes a demand",
                    {"owed": {"mark": demand["mark"], "role": demand["role"], "mode": demand["mode"], "required_move": demand.get("required_move")}},
                )
            retest = self._validate_retest_spec(decision.get("retest"))
        else:
            retest = None
        if is_v4(state):
            if state.get("pending_overlay_review"):
                raise TransitionError(
                    "Blocked gate refused: an overlay review is still owed",
                    {"pending_overlay_review": state["pending_overlay_review"]},
                )
            owed_diagnosis = surgeon_demand(state)
            if owed_diagnosis is not None:
                raise TransitionError(
                    "Blocked gate refused: harness diagnosis is still owed",
                    {"owed": owed_diagnosis["mark"]},
                )
        audit_attempt = next(
            attempt for attempt in reversed(state["attempts"])
            if attempt["role"] == "critic" and attempt["mode"] == "blocker_audit"
        )
        blocker_audit = audit_attempt["blocker_audit"]
        attempt_ids = _string_list(decision.get("attempt_ids"), "attempt_ids")
        if len(set(attempt_ids)) < 3:
            raise ValidationError("Blocked gate requires at least three distinct failed safe attempts")
        attempts = {attempt["id"]: attempt for attempt in state["attempts"]}
        selected = []
        for attempt_id in attempt_ids:
            attempt = attempts.get(attempt_id)
            if not attempt or attempt["role"] != "researcher":
                raise ValidationError("Blocked alternative must reference a research attempt", {"attempt_id": attempt_id})
            if attempt.get("review", {}).get("verdict") == "validated_progress":
                raise ValidationError("A validated-progress attempt does not prove a blocked alternative", {"attempt_id": attempt_id})
            selected.append(attempt)
        if len({attempt["strategy_fingerprint"] for attempt in selected}) != len(selected):
            raise ValidationError("Blocked alternatives must use distinct strategies")
        dimensions_varied = _varied_strategy_dimensions(selected)
        if dimensions_varied < 2:
            raise ValidationError("Blocked alternatives must vary at least two strategy dimensions")
        evidence_ids = _string_list(decision.get("evidence_ids"), "evidence_ids")
        self._require_evidence_ids(evidence_ids, state)
        if not any(state["evidence"][item]["quality"] == "direct" for item in evidence_ids):
            raise ValidationError("Blocked dependency requires direct evidence")
        alternatives = _nonempty_string(decision.get("alternatives_exhausted"), "alternatives_exhausted")
        dependency = _nonempty_string(decision.get("dependency"), "dependency")
        human_action = _nonempty_string(decision.get("human_action"), "human_action")
        if dependency != blocker_audit["dependency"]:
            raise ValidationError("Blocked decision dependency must match the blocker audit")
        if not set(attempt_ids).issubset(blocker_audit["safe_alternative_attempt_ids"]):
            raise ValidationError("Blocked decision attempts must be covered by the blocker audit")
        if not set(evidence_ids).issubset(blocker_audit["dependency_evidence_ids"]):
            raise ValidationError("Blocked dependency evidence must be covered by the blocker audit")
        if human_action != blocker_audit["human_action"]:
            raise ValidationError("Blocked decision human action must match the blocker audit")
        payload = {
            "decision_hash": object_hash(decision),
            "dependency": dependency,
            "attempt_ids": attempt_ids,
            "evidence_ids": evidence_ids,
            "alternatives_exhausted": alternatives,
            "human_action": human_action,
        }
        if retest is not None:
            payload["retest"] = retest
        return payload

    @staticmethod
    def _validate_retest_spec(retest: Any) -> Dict[str, Any]:
        if not isinstance(retest, dict):
            raise ValidationError(
                "A blocked decision requires a retest object so the blocking premise stays falsifiable",
                {"required": {"premise": "the statement that, if false, unblocks the task", "recheck_after": "ISO-8601 time with timezone", "probe": "(optional) how to check it"}},
            )
        _reject_unknown_keys(retest, {"premise", "probe", "recheck_after"}, "retest")
        normalized = {
            "premise": _nonempty_string(retest.get("premise"), "retest.premise"),
            "recheck_after": _nonempty_string(retest.get("recheck_after"), "retest.recheck_after"),
        }
        _parse_time(normalized["recheck_after"], "retest.recheck_after")
        if "probe" in retest:
            normalized["probe"] = _nonempty_string(retest.get("probe"), "retest.probe")
        return normalized

    @staticmethod
    def _resolve_evidence_references(item: Dict[str, Any], ref_map: Dict[str, str]) -> List[str]:
        refs = _string_list(item.get("evidence_refs", []), "evidence_refs", allow_empty=True)
        unknown = set(refs) - set(ref_map)
        if unknown:
            raise ValidationError("Evidence ref is unknown in this attempt", {"unknown": sorted(unknown)})
        ids = _string_list(item.get("evidence_ids", []), "evidence_ids", allow_empty=True)
        return list(dict.fromkeys(ids + [ref_map[ref] for ref in refs]))

    @staticmethod
    def _require_evidence_ids(evidence_ids: Sequence[str], state: Dict[str, Any]) -> None:
        unknown = set(evidence_ids) - set(state["evidence"])
        if unknown:
            raise ValidationError("Unknown evidence id", {"unknown": sorted(unknown)})

    def _replay(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not events or events[0]["type"] != "task_created":
            raise IntegrityError("Event log must begin with task_created")
        created = events[0]
        payload = created["payload"]
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise IntegrityError("Unsupported protocol version", {"version": payload.get("protocol_version")})
        if payload.get("policy_version") not in SUPPORTED_POLICY_VERSIONS:
            raise IntegrityError("Unsupported policy version", {"version": payload.get("policy_version")})
        v3 = payload["policy_version"] != "2.0"
        v4 = payload["policy_version"] not in ("2.0", "3.0")
        v5 = payload["policy_version"] not in ("2.0", "3.0", "4.0")
        required_created = {"protocol_version", "policy_version", "task_id", "task", "criteria", "budget"}
        if v3:
            required_created = required_created | {"controls"}
        allowed_created = [required_created]
        if v5:
            allowed_created.append(required_created | {"charter_hash"})
        if set(payload) not in allowed_created or not isinstance(payload.get("criteria"), list) or not payload["criteria"]:
            raise IntegrityError("task_created payload violates the v2 contract")
        if v5 and "charter_hash" in payload and (
            not isinstance(payload["charter_hash"], str) or not HEX_256.fullmatch(payload["charter_hash"])
        ):
            raise IntegrityError("task_created charter_hash violates the contract")
        if v3:
            controls = payload.get("controls")
            if not isinstance(controls, list) or any(
                not isinstance(item, dict) or set(item) != {"id", "text"} for item in controls
            ):
                raise IntegrityError("task_created controls violate the contract")
        criterion_ids = []
        criterion_texts = []
        for item in payload["criteria"]:
            allowed_shapes = [{"id", "text"}]
            if v4:
                allowed_shapes.append({"id", "text", "min_formalization_rank"})
            if not isinstance(item, dict) or set(item) not in allowed_shapes:
                raise IntegrityError("task_created contains an invalid criterion")
            if v4 and "min_formalization_rank" in item and item["min_formalization_rank"] not in FORMALIZATION_RANKS:
                raise IntegrityError("task_created contains an invalid criterion rank")
            if not isinstance(item.get("id"), str) or not item["id"].strip():
                raise IntegrityError("task_created contains an invalid criterion id")
            if not isinstance(item.get("text"), str) or not item["text"].strip():
                raise IntegrityError("task_created contains an invalid criterion text")
            criterion_ids.append(item["id"].strip())
            criterion_texts.append(item["text"].strip())
        if len(set(criterion_ids)) != len(criterion_ids) or len(set(criterion_texts)) != len(criterion_texts):
            raise IntegrityError("task_created criteria are not unique")
        try:
            _safe_identifier(payload.get("task_id"), "task_id")
            _nonempty_string(payload.get("task"), "task")
            self._validate_budget(payload.get("budget"))
        except ValidationError as exc:
            raise IntegrityError("task_created payload violates the v2 contract") from exc
        state: Dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "policy_version": payload["policy_version"],
            "task_id": payload["task_id"],
            "status": "active",
            "phase": "planning",
            "task": payload["task"],
            "criteria": [
                {
                    "id": item["id"],
                    "text": item["text"],
                    "status": "open",
                    "evidence_ids": [],
                    "verification": {"status": "unverified", "evidence_ids": [], "attempt_id": None},
                }
                for item in payload["criteria"]
            ],
            "attempt_count": 0,
            "failure_count": 0,
            "failure_streak": 0,
            "stagnation_epoch": 0,
            "active_plan_id": None,
            "pending_critique": None,
            "latest_research_attempt_id": None,
            "latest_verifier_attempt_id": None,
            "attempts": [],
            "evidence": {},
            "contradictions": [],
            "escalations_completed": [],
            "verification": {"passed": False, "attempt_id": None},
            "report": {"ready": False, "attempt_id": None, "content_hash": None, "structured": None},
            "budget": payload.get("budget", {}),
            "terminal": None,
            "legacy_import": None,
            "created_at": created["at"],
            "updated_at": created["at"],
            "revision": 1,
            "integrity": {"event_count": 1, "event_head": created["hash"]},
        }
        if v3:
            state.update({
                "criterion_failure_streaks": {item["id"]: 0 for item in payload["criteria"]},
                "pending_triage": None,
                "seed_fingerprints": [],
                "candidates": [],
                "basins": {},
                "controls": copy.deepcopy(payload["controls"]),
                "assets": [],
                "combined_asset_pairs": [],
                "barriers": [],
                "wishes": [],
                "lessons": [],
                "human_input": None,
                "human_exchanges": [],
                "revivals": [],
                "retests": [],
            })
        if v4:
            for criterion, item in zip(state["criteria"], payload["criteria"]):
                criterion["min_formalization_rank"] = item.get("min_formalization_rank")
            state.update({
                "overlays": [],
                "overlay_revision": 0,
                "evidence_kind_registry": {},
                "validator_hooks": [],
                "sandboxes": [],
                "stall_classes": {},
                "role_instruction_overlays": [],
                "toolchain_pins": {},
                "process_faults": {},
                "pending_overlay_review": None,
                "pending_overlay_adoption": None,
                "surgeon_marks": [],
            })
        if v5:
            state["charter_hash"] = payload.get("charter_hash")

        for event in events[1:]:
            event_type = event["type"]
            if state["status"] == "active":
                pass
            elif state["status"] == "awaiting_human" and event_type == "human_input_provided":
                pass
            elif state["status"] == "blocked" and event_type in {"task_unblocked", "task_retested"}:
                pass
            else:
                raise IntegrityError("Event exists after a terminal transition", {"event": event["seq"], "status": state["status"]})
            try:
                if event_type == "attempt_recorded":
                    attempt = event["payload"]["attempt"]
                    steps = self._legal_steps(state)
                    if steps[0]["action"] != "attempt":
                        raise IntegrityError("Attempt event exists where policy permits no attempt", {"event": event["seq"]})
                    expected = next(
                        (
                            step for step in steps
                            if step.get("directive_id") == attempt["directive_id"]
                            and step["role"] == attempt["role"]
                            and step["mode"] == attempt["mode"]
                        ),
                        None,
                    )
                    if (
                        expected is None
                        or attempt["id"] != f"A{state['attempt_count'] + 1:06d}"
                        or attempt["request_id"] != event["request_id"]
                    ):
                        raise IntegrityError("Attempt event violates the scheduled transition", {"event": event["seq"]})
                    self._replay_attempt(state, attempt)
                elif event_type == "legacy_imported":
                    if state["legacy_import"] is not None or state["attempt_count"]:
                        raise IntegrityError("legacy_imported must occur once before any attempts", {"event": event["seq"]})
                    state["legacy_import"] = event["payload"]
                elif event_type == "task_completed":
                    if expected_step(state)["action"] != "finalize":
                        raise IntegrityError("Completion event bypasses the report or verification gate", {"event": event["seq"]})
                    if event["payload"].get("report_hash") != state["report"]["content_hash"]:
                        raise IntegrityError("Completion event report hash does not match replayed report", {"event": event["seq"]})
                    if set(event["payload"].get("verified_criteria", [])) != {
                        criterion["id"] for criterion in state["criteria"]
                    } or event["payload"].get("verifier_attempt_id") != state["verification"]["attempt_id"]:
                        raise IntegrityError("Completion event does not bind the verified criterion set", {"event": event["seq"]})
                    state["status"] = "completed"
                    state["terminal"] = {"kind": "completed", **event["payload"]}
                elif event_type == "task_blocked":
                    if not _blocker_audit_completed(state) or state["pending_critique"]:
                        raise IntegrityError("Blocked event bypasses the blocker gate", {"event": event["seq"]})
                    if is_v3(state) and (state.get("pending_triage") or escalation_demand(state) is not None):
                        raise IntegrityError("Blocked event bypasses an owed escalation demand", {"event": event["seq"]})
                    if is_v3(state) and not isinstance(event["payload"].get("retest"), dict):
                        raise IntegrityError("Blocked event lacks the required retest premise", {"event": event["seq"]})
                    if is_v4(state) and (
                        state.get("pending_overlay_review") or surgeon_demand(state) is not None
                    ):
                        raise IntegrityError("Blocked event bypasses an owed harness diagnosis", {"event": event["seq"]})
                    state["status"] = "blocked"
                    state["terminal"] = {"kind": "blocked", **event["payload"]}
                elif event_type == "task_unsafe":
                    state["status"] = "unsafe"
                    state["terminal"] = {"kind": "unsafe", **event["payload"]}
                elif event_type == "budget_exhausted":
                    actual_reason = self._budget_reason(state, _parse_time(event["at"], "event timestamp"))
                    if not actual_reason or event["payload"].get("reason") != actual_reason:
                        raise IntegrityError("Budget event exists before its configured hard limit", {"event": event["seq"]})
                    state["status"] = "budget_exhausted"
                    state["terminal"] = {"kind": "budget_exhausted", **event["payload"]}
                elif event_type == "task_unblocked":
                    state["status"] = "active"
                    state["terminal"] = None
                    state.setdefault("revivals", []).append({
                        "at": event["at"],
                        "reason": event["payload"]["reason"],
                        "note": event["payload"].get("note"),
                        "evidence_ids": event["payload"].get("evidence_ids", []),
                    })
                elif event_type == "task_retested":
                    state.setdefault("retests", []).append({
                        "at": event["at"],
                        "outcome": event["payload"]["outcome"],
                        "note": event["payload"]["note"],
                        "recheck_after": event["payload"].get("recheck_after"),
                    })
                elif event_type == "human_input_requested":
                    if is_v4(state):
                        if event["payload"].get("classification") not in HUMAN_INPUT_CLASSIFICATIONS:
                            raise IntegrityError(
                                "human_input_requested lacks a valid classification",
                                {"event": event["seq"]},
                            )
                        if state.get("pending_overlay_review") or surgeon_demand(state) is not None:
                            raise IntegrityError(
                                "human_input_requested bypasses an owed harness diagnosis",
                                {"event": event["seq"]},
                            )
                    state["status"] = "awaiting_human"
                    state["human_input"] = {
                        "question": event["payload"]["question"],
                        "context": event["payload"].get("context"),
                        "requested_at": event["at"],
                    }
                    if is_v4(state):
                        state["human_input"]["classification"] = event["payload"]["classification"]
                elif event_type == "human_input_provided":
                    pending = state.get("human_input") or {}
                    state["status"] = "active"
                    exchange = {
                        "question": pending.get("question"),
                        "requested_at": pending.get("requested_at"),
                        "answer": event["payload"]["answer"],
                        "answered_at": event["at"],
                    }
                    if "classification" in pending:
                        exchange["classification"] = pending["classification"]
                    state.setdefault("human_exchanges", []).append(exchange)
                    state["human_input"] = None
                elif event_type == "criterion_added":
                    if not is_v3(state):
                        raise IntegrityError("criterion_added requires policy version 3.0", {"event": event["seq"]})
                    criterion = event["payload"]["criterion"]
                    allowed_criterion_keys = {"id", "text"}
                    if is_v4(state):
                        allowed_criterion_keys = allowed_criterion_keys | {"min_formalization_rank"}
                    if set(criterion) - allowed_criterion_keys or (
                        "min_formalization_rank" in criterion
                        and criterion["min_formalization_rank"] not in FORMALIZATION_RANKS
                    ):
                        raise IntegrityError("criterion_added carries an invalid criterion shape", {"event": event["seq"]})
                    if criterion["id"] != f"C{len(state['criteria']) + 1}" or criterion["text"] in {
                        item["text"] for item in state["criteria"]
                    }:
                        raise IntegrityError("criterion_added violates additive numbering", {"event": event["seq"]})
                    self._append_criterion(state, criterion)
                elif event_type == "wish_fulfilled":
                    if not is_v3(state):
                        raise IntegrityError("wish_fulfilled requires policy version 3.0", {"event": event["seq"]})
                    wish = next(
                        item for item in state["wishes"]
                        if item["id"] == event["payload"]["wish_id"] and item["status"] == "open"
                    )
                    wish["status"] = "fulfilled"
                    wish["fulfilled"] = {"at": event["at"], "note": event["payload"]["note"]}
                elif event_type == "process_fault_recorded":
                    if not is_v4(state):
                        raise IntegrityError("process_fault_recorded requires policy version 4.0", {"event": event["seq"]})
                    signature = event["payload"]["signature"]
                    if not isinstance(signature, str) or not HEX_256.fullmatch(signature):
                        raise IntegrityError("process_fault_recorded carries an invalid signature", {"event": event["seq"]})
                    fault = state["process_faults"].get(signature)
                    if fault is None:
                        state["process_faults"][signature] = {
                            "count": 1,
                            "source": event["payload"]["source"],
                            "detail": event["payload"]["detail"],
                            "first_at": event["at"],
                            "last_at": event["at"],
                        }
                    else:
                        fault["count"] += 1
                        fault["source"] = event["payload"]["source"]
                        fault["detail"] = event["payload"]["detail"]
                        fault["last_at"] = event["at"]
                elif event_type == "contract_overlay_adopted":
                    if not is_v4(state):
                        raise IntegrityError("contract_overlay_adopted requires policy version 4.0", {"event": event["seq"]})
                    self._replay_overlay_adoption(state, event)
                else:
                    raise IntegrityError("Unknown event type", {"event": event["seq"], "type": event_type})
            except IntegrityError:
                raise
            except (KeyError, TypeError, StopIteration, ValueError) as exc:
                raise IntegrityError(
                    "Event payload violates the v2 protocol",
                    {"event": event["seq"], "type": event_type},
                ) from exc
            state["updated_at"] = event["at"]
            state["revision"] = event["seq"]
            state["integrity"] = {"event_count": event["seq"], "event_head": event["hash"]}

        state["failure_count"] = state["failure_streak"]
        if state["status"] != "active":
            state["phase"] = "terminal"
        else:
            step = expected_step(state)
            state["phase"] = step.get("mode", step["action"])
        return state

    def _replay_attempt(self, state: Dict[str, Any], attempt: Dict[str, Any]) -> None:
        attempt = copy.deepcopy(attempt)
        v3 = is_v3(state)
        v4 = is_v4(state)
        # The demand owed at the moment this attempt landed decides whether it
        # earns an escalation mark; computed before any mutation so replay is
        # deterministic.
        demand = escalation_demand(state) if v3 else None
        owed_diagnosis = surgeon_demand(state) if v4 else None
        state["attempt_count"] += 1
        state["attempts"].append(attempt)
        for evidence in attempt["evidence"]:
            if evidence["id"] in state["evidence"]:
                raise IntegrityError("Duplicate evidence id in event history", {"evidence_id": evidence["id"]})
            state["evidence"][evidence["id"]] = evidence

        criteria = {criterion["id"]: criterion for criterion in state["criteria"]}
        for update in attempt["criterion_updates"]:
            criterion = criteria[update["id"]]
            criterion["status"] = update["status"]
            criterion["evidence_ids"] = list(dict.fromkeys(criterion["evidence_ids"] + update["evidence_ids"]))
            criterion["verification"] = {"status": "unverified", "evidence_ids": [], "attempt_id": None}

        for contradiction in attempt["contradictions"]:
            state["contradictions"].append(contradiction)
        contradictions = {item["id"]: item for item in state["contradictions"]}
        for resolution in attempt["contradiction_resolutions"]:
            contradiction = contradictions[resolution["id"]]
            contradiction["status"] = "resolved"
            contradiction["resolution"] = resolution["resolution"]
            contradiction["resolution_evidence_ids"] = resolution["evidence_ids"]

        role = attempt["role"]
        mode = attempt["mode"]
        if role == "planner":
            state["active_plan_id"] = attempt["id"]
            if v3:
                if demand and demand["role"] == "planner" and demand["mode"] == mode:
                    self._append_mark(state, demand["mark"])
                for proposed in attempt.get("proposed_criteria", []):
                    self._append_criterion(
                        state,
                        {"id": f"C{len(state['criteria']) + 1}", "text": proposed["text"]},
                    )
            elif mode != "initial_plan" and mode not in state["escalations_completed"]:
                state["escalations_completed"].append(mode)
        elif role == "surgeon":
            if owed_diagnosis is None or attempt["demand"]["mark"] != owed_diagnosis["mark"]:
                raise IntegrityError(
                    "Diagnosis event does not match the owed demand",
                    {"attempt": attempt["id"]},
                )
            if owed_diagnosis["mark"] not in state["surgeon_marks"]:
                state["surgeon_marks"].append(owed_diagnosis["mark"])
            if attempt["diagnosis"]["classification"] == "harness_gap":
                state["pending_overlay_review"] = attempt["id"]
        elif role == "explorer":
            state["pending_triage"] = attempt["id"]
            for seed in attempt.get("seeds", []):
                state["seed_fingerprints"].append(seed["fingerprint"])
            if demand and demand["role"] == "explorer":
                self._append_mark(state, demand["mark"])
        elif role == "researcher":
            state["pending_critique"] = attempt["id"]
            state["latest_research_attempt_id"] = attempt["id"]
            state["verification"] = {"passed": False, "attempt_id": None}
            state["report"] = {"ready": False, "attempt_id": None, "content_hash": None, "structured": None}
            for criterion in state["criteria"]:
                criterion["verification"] = {"status": "unverified", "evidence_ids": [], "attempt_id": None}
            if v3:
                self._apply_researcher_v3(state, attempt, demand)
        elif role == "critic":
            if mode == "attempt_review":
                assessment = attempt["assessment"]
                target = next(item for item in state["attempts"] if item["id"] == assessment["target_attempt_id"])
                target["review"] = {
                    "critic_attempt_id": attempt["id"],
                    "verdict": assessment["verdict"],
                    "reasons": assessment["reasons"],
                    "uncertainty": assessment["uncertainty"],
                }
                if v3 and attempt.get("lens"):
                    target["review"]["lens"] = attempt["lens"]
                if v4 and assessment.get("harness_gap") is True:
                    target["review"]["harness_gap"] = True
                state["pending_critique"] = None
                if assessment["verdict"] == "validated_progress":
                    state["failure_streak"] = 0
                    state["stagnation_epoch"] += 1
                    if v3:
                        self._apply_review_v3(state, attempt, target, validated=True)
                    else:
                        state["escalations_completed"] = []
                else:
                    state["failure_streak"] += 1
                    if v3:
                        self._apply_review_v3(state, attempt, target, validated=False)
            elif mode == "triage":
                self._apply_triage(state, attempt)
            elif v4 and mode == "overlay_review":
                review = attempt["overlay_review"]
                target = next(item for item in state["attempts"] if item["id"] == review["target_attempt_id"])
                target["review"] = {
                    "critic_attempt_id": attempt["id"],
                    "verdict": review["verdict"],
                    "reasons": review["reasons"],
                    "uncertainty": review["uncertainty"],
                }
                state["pending_overlay_review"] = None
                # Deliberately streak-neutral: judging an overlay is harness
                # governance, not scientific progress or failure.
                if review["verdict"] in {"adopt", "narrow"}:
                    delta = review["narrowed_delta"] if review["verdict"] == "narrow" else (
                        target["diagnosis"]["proposed_delta"]
                    )
                    state["pending_overlay_adoption"] = {
                        "justification_attempt_id": target["id"],
                        "critic_attempt_id": attempt["id"],
                        "verdict": review["verdict"],
                        "delta": copy.deepcopy(delta),
                        "fingerprint": target["diagnosis"]["delta_fingerprint"],
                    }
            elif v3:
                if demand and demand["role"] == "critic" and demand["mode"] == mode:
                    self._append_mark(state, demand["mark"])
            elif mode not in state["escalations_completed"]:
                state["escalations_completed"].append(mode)
        elif role == "verifier":
            passed = True
            for result in attempt["verification_results"]:
                criterion = criteria[result["criterion_id"]]
                criterion["verification"] = {
                    "status": result["verdict"],
                    "evidence_ids": result["evidence_ids"],
                    "attempt_id": attempt["id"],
                }
                if result["verdict"] == "fail":
                    criterion["status"] = "failed_verification"
                    passed = False
                    if v3:
                        streaks = state["criterion_failure_streaks"]
                        streaks[result["criterion_id"]] = streaks.get(result["criterion_id"], 0) + 1
            state["verification"] = {"passed": passed, "attempt_id": attempt["id"]}
            state["latest_verifier_attempt_id"] = attempt["id"]
            if not passed:
                state["failure_streak"] += 1
        elif role == "synthesizer":
            rendered = self._render_report(attempt["report"], state)
            state["report"] = {
                "ready": True,
                "attempt_id": attempt["id"],
                "content_hash": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                "structured": attempt["report"],
            }
        if v3 and role in {"researcher", "critic"}:
            for wish in attempt.get("wishes_declared", []):
                state["wishes"].append({
                    "id": f"W{len(state['wishes']) + 1:04d}",
                    "statement": wish["statement"],
                    "would_open": wish["would_open"],
                    "test": wish["test"],
                    "recheck_after": wish["recheck_after"],
                    "status": "open",
                    "declared_by": attempt["id"],
                })

    @staticmethod
    def _append_mark(state: Dict[str, Any], mark: str) -> None:
        if mark not in state["escalations_completed"]:
            state["escalations_completed"].append(mark)

    @staticmethod
    def _append_criterion(state: Dict[str, Any], criterion: Dict[str, str]) -> None:
        entry: Dict[str, Any] = {
            "id": criterion["id"],
            "text": criterion["text"],
            "status": "open",
            "evidence_ids": [],
            "verification": {"status": "unverified", "evidence_ids": [], "attempt_id": None},
        }
        if is_v4(state):
            entry["min_formalization_rank"] = criterion.get("min_formalization_rank")
        state["criteria"].append(entry)
        state["criterion_failure_streaks"][criterion["id"]] = 0
        # A new goal reopens the proof: whatever was verified no longer covers
        # the whole criterion set.
        state["verification"] = {"passed": False, "attempt_id": None}
        state["report"] = {"ready": False, "attempt_id": None, "content_hash": None, "structured": None}

    def _apply_researcher_v3(
        self,
        state: Dict[str, Any],
        attempt: Dict[str, Any],
        demand: Optional[Dict[str, Any]],
    ) -> None:
        basin_key = _normalized_strategy_value(attempt["basin"])
        entry = state["basins"].get(basin_key)
        if entry is None:
            entry = {"name": attempt["basin"], "status": "open", "failures": 0}
            if is_v4(state):
                entry["stalls"] = 0
            state["basins"][basin_key] = entry
        candidate_id = attempt.get("candidate_id")
        if candidate_id:
            candidate = next(item for item in state["candidates"] if item["id"] == candidate_id)
            candidate["status"] = "consumed"
            candidate["consumed_by"] = attempt["id"]
        for registered in attempt.get("assets_registered", []):
            state["assets"].append({
                "id": f"AS{len(state['assets']) + 1:04d}",
                **registered,
                "producer_attempt_id": attempt["id"],
            })
        combination = attempt.get("combination")
        if combination:
            state["combined_asset_pairs"].append(list(combination["asset_ids"]))
        probe = attempt.get("barrier_probe")
        if probe:
            if "new_barrier" in probe:
                barrier = {
                    "id": f"BR{len(state['barriers']) + 1:04d}",
                    "name": probe["new_barrier"]["name"],
                    "statement": probe["new_barrier"]["statement"],
                    "declared_by": attempt["id"],
                    "probes": [],
                }
                state["barriers"].append(barrier)
            else:
                barrier = next(item for item in state["barriers"] if item["id"] == probe["barrier_id"])
            barrier["probes"].append({"attempt_id": attempt["id"], "pattern": probe["pattern"]})
        if (
            demand
            and demand["role"] == "researcher"
            and demand["mode"] == "experiment"
            and attempt.get("move", "test") == demand.get("required_move")
        ):
            self._append_mark(state, demand["mark"])

    @staticmethod
    def _stall_basin(entry: Dict[str, Any]) -> None:
        """Record a near-miss in a basin and close it once they accumulate."""

        entry["stalls"] = entry.get("stalls", 0) + 1
        if entry["stalls"] >= BASIN_STALL_CLOSURE:
            entry["status"] = "closed"

    def _apply_review_v3(
        self,
        state: Dict[str, Any],
        review_attempt: Dict[str, Any],
        target: Dict[str, Any],
        validated: bool,
    ) -> None:
        streaks = state["criterion_failure_streaks"]
        targets = target.get("criterion_targets", [])
        verdict = review_attempt["assessment"]["verdict"]
        basin_key = _normalized_strategy_value(target["basin"]) if target.get("basin") else None
        candidate_id = target.get("candidate_id")
        candidate = (
            next((item for item in state["candidates"] if item["id"] == candidate_id), None)
            if candidate_id
            else None
        )
        v4 = is_v4(state)
        if validated:
            for criterion_id in targets:
                streaks[criterion_id] = 0
            # Only the validated criteria's ladders reset; demands owed to other
            # stuck criteria survive, so a validated side-quest cannot silence
            # the alarm on the criterion that is actually stuck.
            state["escalations_completed"] = [
                mark for mark in state["escalations_completed"]
                if not any(f"@{criterion_id}#" in mark for criterion_id in targets)
            ]
            if basin_key and basin_key in state["basins"]:
                entry = state["basins"][basin_key]
                if not v4 or _targets_closed(state, targets):
                    entry["failures"] = 0
                    entry["status"] = "open"
                else:
                    # 4.0: the critic endorsed the work but the criteria it aimed at are still open.
                    # Wiping the basin here is what let a researcher edit one baseline forever, so the
                    # history stands and the near-miss counts toward closing the basin instead.
                    self._stall_basin(entry)
            if candidate is not None:
                candidate["status"] = "validated"
        else:
            for criterion_id in targets:
                streaks[criterion_id] = streaks.get(criterion_id, 0) + 1
            if verdict in {"no_progress", "invalid"}:
                if basin_key and basin_key in state["basins"]:
                    entry = state["basins"][basin_key]
                    entry["failures"] += 1
                    if entry["failures"] >= BASIN_CLOSURE_FAILURES:
                        entry["status"] = "closed"
                if candidate is not None:
                    candidate["status"] = "dead"
            elif v4 and basin_key and basin_key in state["basins"]:
                # `mixed` is not arrival either. It does not condemn the basin the way a confirmed
                # failure does, but it cannot leave the basin untouched or the ladder starves.
                self._stall_basin(state["basins"][basin_key])
            for cause in review_attempt["assessment"].get("candidate_causes", []):
                state["lessons"].append({
                    "id": f"L{len(state['lessons']) + 1:04d}",
                    "attempt_id": target["id"],
                    "review_attempt_id": review_attempt["id"],
                    "criterion_ids": list(targets),
                    "cause": cause["cause"],
                    "discriminating_test": cause["discriminating_test"],
                })

    def _apply_triage(self, state: Dict[str, Any], attempt: Dict[str, Any]) -> None:
        state["pending_triage"] = None
        triage = attempt["triage"]
        explorer = next(item for item in state["attempts"] if item["id"] == triage["target_attempt_id"])
        seeds = explorer.get("seeds", [])
        candidates_by_id = {item["id"]: item for item in state["candidates"]}
        seed_scores: Dict[int, float] = {}
        for comparison in triage["comparisons"]:
            seed_index = comparison["seed_index"]
            candidate = candidates_by_id[comparison["candidate_id"]]
            seed_score = seed_scores.get(seed_index, ELO_INITIAL)
            expected_seed = 1.0 / (1.0 + 10.0 ** ((candidate["score"] - seed_score) / 400.0))
            actual_seed = 1.0 if comparison["winner"] == "seed" else 0.0
            seed_scores[seed_index] = round(seed_score + ELO_K * (actual_seed - expected_seed), 6)
            candidate["score"] = round(candidate["score"] + ELO_K * ((1.0 - actual_seed) - (1.0 - expected_seed)), 6)
        for verdict in triage["verdicts"]:
            if verdict["decision"] != "promoted":
                continue
            seed = seeds[verdict["seed_index"]]
            candidate = {
                "id": f"Q{len(state['candidates']) + 1:04d}",
                "claim": seed["claim"],
                "basin": seed["basin"],
                "first_unjustified_step": seed["first_unjustified_step"],
                "kill_test": seed["kill_test"],
                "seed_fingerprint": seed["fingerprint"],
                "source_attempt_id": explorer["id"],
                "promoted_by": attempt["id"],
                "status": "open",
                "score": seed_scores.get(verdict["seed_index"], ELO_INITIAL),
                "consumed_by": None,
            }
            for optional in ("control_object", "needs", "parents", "representation_shift"):
                if optional in seed:
                    candidate[optional] = seed[optional]
            state["candidates"].append(candidate)

    def _replay_overlay_adoption(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:
        """Fail-closed replay of an adoption: it must bind the recorded review
        exactly, keep revisions monotonic, and still be tighten-only against
        the state it landed on."""

        payload = event["payload"]
        required = {
            "overlay_id", "content_hash", "from_revision", "to_revision", "schema_version",
            "delta", "justification_attempt_id", "critic_attempt_id", "verdict", "fingerprint",
        }
        if set(payload) != required:
            raise IntegrityError("contract_overlay_adopted payload violates the contract", {"event": event["seq"]})
        pending = state.get("pending_overlay_adoption")
        if pending is None:
            raise IntegrityError("contract_overlay_adopted has no adopted review behind it", {"event": event["seq"]})
        if (
            payload["justification_attempt_id"] != pending["justification_attempt_id"]
            or payload["critic_attempt_id"] != pending["critic_attempt_id"]
            or payload["verdict"] != pending["verdict"]
            or payload["delta"] != pending["delta"]
            or payload["fingerprint"] != pending["fingerprint"]
        ):
            raise IntegrityError("contract_overlay_adopted does not match the recorded review", {"event": event["seq"]})
        if (
            payload["from_revision"] != state["overlay_revision"]
            or payload["to_revision"] != state["overlay_revision"] + 1
            or payload["overlay_id"] != f"OV{state['overlay_revision'] + 1:04d}"
            or payload["schema_version"] != OVERLAY_SCHEMA_VERSION
            or payload["content_hash"] != object_hash(payload["delta"])
        ):
            raise IntegrityError("contract_overlay_adopted violates monotonic overlay numbering", {"event": event["seq"]})
        try:
            self._validate_overlay_delta(payload["delta"], state)
        except ValidationError as exc:
            raise IntegrityError(
                "contract_overlay_adopted delta violates the tighten-only contract",
                {"event": event["seq"], "cause": str(exc)},
            ) from exc
        self._fold_overlay(state, payload)

    def _fold_overlay(self, state: Dict[str, Any], payload: Dict[str, Any]) -> None:
        state["overlays"].append(copy.deepcopy(payload))
        state["overlay_revision"] = payload["to_revision"]
        overlay_id = payload["overlay_id"]
        for op in payload["delta"]["ops"]:
            name = op["op"]
            if name == "register_evidence_kind":
                state["evidence_kind_registry"][op["kind"]] = {
                    "required_fields": list(op["required_fields"]),
                    "min_rank": op.get("min_rank"),
                    "overlay_id": overlay_id,
                }
            elif name == "register_validator_hook":
                state["validator_hooks"].append({
                    "hook_id": op["hook_id"],
                    "rank": op["rank"],
                    "command": list(op["command"]),
                    "timeout_seconds": op["timeout_seconds"],
                    "overlay_id": overlay_id,
                })
            elif name == "register_sandbox":
                state["sandboxes"].append({
                    "sandbox_id": op["sandbox_id"],
                    "description": op["description"],
                    "mechanism_locator": op["mechanism_locator"],
                    "overlay_id": overlay_id,
                })
            elif name == "add_stall_class":
                state["stall_classes"][op["signature"]] = {
                    "label": op["label"],
                    "threshold": op["threshold"],
                    "overlay_id": overlay_id,
                }
            elif name == "add_role_instructions":
                state["role_instruction_overlays"].append({
                    "role": op["role"],
                    "mode": op["mode"],
                    "instructions": list(op["instructions"]),
                    "overlay_id": overlay_id,
                })
            elif name == "add_criterion":
                self._append_criterion(state, {
                    "id": f"C{len(state['criteria']) + 1}",
                    "text": op["text"],
                    "min_formalization_rank": op.get("min_formalization_rank"),
                })
            elif name in {"require_min_rank", "raise_criterion_rank"}:
                criterion = next(
                    item for item in state["criteria"] if item["id"] == op["criterion_id"]
                )
                criterion["min_formalization_rank"] = op["min_formalization_rank"]
                # A tightened goal reopens the proof, exactly like a new one.
                state["verification"] = {"passed": False, "attempt_id": None}
                state["report"] = {"ready": False, "attempt_id": None, "content_hash": None, "structured": None}
            elif name == "pin_toolchain_hash":
                state["toolchain_pins"][op["toolchain_id"]] = {
                    "artifact_hash": op["artifact_hash"],
                    "overlay_id": overlay_id,
                }
        state["pending_overlay_adoption"] = None

    def _apply_overlay_adoption_unlocked(
        self,
        events: List[Dict[str, Any]],
        state: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Materialize an adopted review as the engine-authored adoption event.

        Runs at every writer entry, so review -> adoption is effectively atomic:
        a crash between the two appends is finished by the next entry before any
        other event can land.
        """

        pending = state.get("pending_overlay_adoption")
        if not pending or state["status"] != "active":
            return events, state
        payload = {
            "overlay_id": f"OV{state['overlay_revision'] + 1:04d}",
            "content_hash": object_hash(pending["delta"]),
            "from_revision": state["overlay_revision"],
            "to_revision": state["overlay_revision"] + 1,
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "delta": pending["delta"],
            "justification_attempt_id": pending["justification_attempt_id"],
            "critic_attempt_id": pending["critic_attempt_id"],
            "verdict": pending["verdict"],
            "fingerprint": pending["fingerprint"],
        }
        request_id = f"system:overlay:{state['integrity']['event_head']}"
        self.store.append_unlocked("contract_overlay_adopted", payload, request_id)
        events = self.store.read_unlocked()
        return events, self._replay(events)

    def _apply_system_gates_unlocked(
        self,
        events: List[Dict[str, Any]],
        state: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        events, state = self._apply_overlay_adoption_unlocked(events, state)
        return self._apply_budget_gate_unlocked(events, state)

    def _apply_budget_gate_unlocked(
        self,
        events: List[Dict[str, Any]],
        state: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if state["status"] != "active":
            return events, state
        reason = self._budget_reason(state)
        if not reason:
            return events, state
        payload = {"reason": reason, "budget": state["budget"], "usage": {
            "attempts": state["attempt_count"], "failure_streak": state["failure_streak"], "checked_at": utc_now(),
        }}
        request_id = f"system:budget:{state['integrity']['event_head']}"
        self.store.append_unlocked("budget_exhausted", payload, request_id)
        events = self.store.read_unlocked()
        return events, self._replay(events)

    @staticmethod
    def _budget_reason(state: Dict[str, Any], checked_at: Optional[datetime] = None) -> Optional[str]:
        budget = state["budget"]
        if "max_attempts" in budget and state["attempt_count"] >= budget["max_attempts"]:
            return "max_attempts reached"
        if "max_failures" in budget and state["failure_streak"] >= budget["max_failures"]:
            return "max_failures reached"
        checked_at = checked_at or datetime.now(timezone.utc)
        if "deadline" in budget and checked_at >= _parse_time(budget["deadline"], "deadline"):
            return "deadline reached"
        return None

    def _completion_failures_unlocked(self, events: List[Dict[str, Any]], state: Dict[str, Any]) -> List[str]:
        failures: List[str] = []
        if state["status"] != "active":
            failures.append(f"task status is {state['status']}")
        if not criteria_evidence_ready(state):
            failures.append("one or more criteria lack satisfied, criterion-linked direct evidence")
        if unresolved_critical_contradictions(state):
            failures.append("critical contradictions remain unresolved")
        if not state["verification"]["passed"]:
            failures.append("independent verification has not passed every criterion")
        if not state["report"]["ready"]:
            failures.append("structured final report is not ready")
        if state["verification"]["attempt_id"] and state["latest_research_attempt_id"]:
            verifier_number = int(state["verification"]["attempt_id"][1:])
            research_number = int(state["latest_research_attempt_id"][1:])
            if verifier_number <= research_number:
                failures.append("verifier attempt is not later than the latest research attempt")
        if state["report"]["attempt_id"] and state["verification"]["attempt_id"]:
            if int(state["report"]["attempt_id"][1:]) <= int(state["verification"]["attempt_id"][1:]):
                failures.append("report was not synthesized after verification")
        mismatches = self._projection_mismatches(self._projection_texts(events, state))
        if mismatches:
            failures.append("workspace projections fail integrity audit: " + ", ".join(mismatches))
        failures.extend(self._semantic_issues(state))
        return list(dict.fromkeys(failures))

    def _repair_projections_if_needed_unlocked(self, events: List[Dict[str, Any]], state: Dict[str, Any]) -> None:
        expected = self._projection_texts(events, state)
        if self._projection_mismatches(expected):
            self._materialize_unlocked(events, state)

    def _materialize_unlocked(self, events: List[Dict[str, Any]], state: Dict[str, Any]) -> None:
        for path, text in self._projection_texts(events, state).items():
            atomic_write_text(self.workspace / path, text)

    def _projection_texts(self, events: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, str]:
        task_lines = ["# Task", "", state["task"], "", "## Acceptance criteria", ""]
        for criterion in state["criteria"]:
            checked = "x" if criterion["status"] == "satisfied" and criterion["verification"]["status"] == "pass" else " "
            task_lines.append(f"- [{checked}] {criterion['id']}: {criterion['text']}")
        task_text = "\n".join(task_lines) + "\n"

        attempts_text = "".join(canonical_json(attempt) + "\n" for attempt in state["attempts"])
        evidence_text = "".join(canonical_json(item) + "\n" for item in state["evidence"].values())
        decision_lines = ["# Decision log", ""]
        for attempt in state["attempts"]:
            for decision in attempt["decisions"]:
                decision_lines.extend([
                    f"## {attempt['at']} — {decision['decision']}", "", decision["rationale"], "",
                ])
                if decision["rejected_alternatives"]:
                    decision_lines.append("Rejected alternatives:")
                    decision_lines.extend(f"- {item}" for item in decision["rejected_alternatives"])
                    decision_lines.append("")
        if len(decision_lines) == 2:
            decision_lines.extend(["No decisions recorded yet.", ""])
        decision_text = "\n".join(decision_lines)
        report_text = (
            self._render_report(state["report"]["structured"], state)
            if state["report"]["ready"]
            else "# Report\n\nNot complete.\n"
        )
        return {
            "task.md": task_text,
            "state.json": json.dumps(state, indent=2, sort_keys=True) + "\n",
            "attempts.jsonl": attempts_text,
            "evidence.jsonl": evidence_text,
            "decision-log.md": decision_text,
            "report.md": report_text,
        }

    def _projection_mismatches(self, expected: Dict[str, str]) -> List[str]:
        mismatches = []
        for relative, text in expected.items():
            path = self.workspace / relative
            try:
                actual = path.read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeDecodeError):
                mismatches.append(relative)
                continue
            if actual != text:
                mismatches.append(relative)
        return mismatches

    @staticmethod
    def _render_report(report: Dict[str, Any], state: Dict[str, Any]) -> str:
        criteria = {criterion["id"]: criterion for criterion in state["criteria"]}
        lines = ["# Report", "", report["summary"], "", "## Acceptance criteria", ""]
        for result in report["criterion_results"]:
            criterion = criteria[result["criterion_id"]]
            lines.extend([
                f"### {criterion['id']}: {criterion['text']}", "", result["conclusion"], "",
                f"Primary evidence: {', '.join(result['primary_evidence_ids'])}", "",
                f"Verification evidence: {', '.join(result['verification_evidence_ids'])}", "",
            ])
        lines.extend(["## Facts", ""])
        lines.extend(f"- {item['claim']} [{', '.join(item['evidence_ids'])}]" for item in report["facts"])
        lines.extend(["", "## Inferences", ""])
        if report["inferences"]:
            lines.extend(
                f"- {item['claim']} (confidence: {item['confidence']}; basis: {', '.join(item['basis_evidence_ids'])})"
                for item in report["inferences"]
            )
        else:
            lines.append("- None.")
        lines.extend(["", "## Uncertainties", ""])
        lines.extend(f"- {item}" for item in report["uncertainties"] or ["None identified."])
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {item}" for item in report["limitations"])
        lines.extend(["", "## Unresolved noncritical contradictions", ""])
        lines.extend(
            f"- {item}"
            for item in report["unresolved_noncritical_contradiction_ids"] or ["None."]
        )
        lines.extend(["", "## Next actions", ""])
        lines.extend(f"- {item}" for item in report["next_actions"] or ["None."])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _semantic_issues(state: Dict[str, Any]) -> List[str]:
        issues = []
        expected_attempts = [f"A{index + 1:06d}" for index in range(len(state["attempts"]))]
        actual_attempts = [attempt["id"] for attempt in state["attempts"]]
        if actual_attempts != expected_attempts:
            issues.append("attempt ids are not contiguous")
        expected_evidence = [f"E{index + 1:06d}" for index in range(len(state["evidence"]))]
        if list(state["evidence"]) != expected_evidence:
            issues.append("evidence ids are not contiguous")
        for criterion in state["criteria"]:
            if set(criterion["evidence_ids"]) - set(state["evidence"]):
                issues.append(f"criterion {criterion['id']} references missing evidence")
        if "overlays" in state and state["overlay_revision"] != len(state["overlays"]):
            issues.append("overlay revision does not match the adopted overlay count")
        return issues

    def _require_event_workspace_unlocked(self) -> None:
        if self.store.events_path.exists():
            return
        if (self.workspace / "state.json").exists():
            raise LegacyWorkspaceError(
                "This is a v1 snapshot workspace; v2 refuses to invent an event history",
                {"workspace": str(self.workspace), "action": "create a v2 task or migrate with explicit provenance"},
            )
        raise IntegrityError("Workspace has no event log", {"workspace": str(self.workspace)})

    @staticmethod
    def _find_request(events: Iterable[Dict[str, Any]], request_id: str) -> Optional[Dict[str, Any]]:
        return next((event for event in events if event["request_id"] == request_id), None)


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _safe_identifier(value: Any, name: str) -> str:
    value = _nonempty_string(value, name)
    if not SAFE_ID.fullmatch(value):
        raise ValidationError(f"{name} contains unsafe characters", {"value": value})
    return value


def _string_list(value: Any, name: str, allow_empty: bool = False) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{name} must be a list of non-empty strings")
    normalized = [item.strip() for item in value]
    if not allow_empty and not normalized:
        raise ValidationError(f"{name} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValidationError(f"{name} must not contain duplicates")
    return normalized


def _reject_unknown_keys(value: Dict[str, Any], allowed: Set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValidationError(f"{name} contains unknown fields", {"unknown": sorted(unknown)})


def _strategy_distance(left: Dict[str, str], right: Dict[str, str]) -> int:
    return sum(
        _normalized_strategy_value(left[dimension]) != _normalized_strategy_value(right[dimension])
        for dimension in STRATEGY_DIMENSIONS
    )


def _blocker_audit_completed(state: Dict[str, Any]) -> bool:
    if is_v3(state):
        return any(mark.startswith("blocker_audit@") for mark in state["escalations_completed"])
    return "blocker_audit" in state["escalations_completed"]


def _varied_strategy_dimensions(attempts: Sequence[Dict[str, Any]]) -> int:
    return sum(
        1
        for dimension in STRATEGY_DIMENSIONS
        if len({_normalized_strategy_value(attempt["strategy"][dimension]) for attempt in attempts}) > 1
    )


def _strategy_signature(strategy: Dict[str, str]) -> Dict[str, str]:
    return {dimension: _normalized_strategy_value(strategy[dimension]) for dimension in STRATEGY_DIMENSIONS}


def _targets_closed(state: Dict[str, Any], targets: List[str]) -> bool:
    """True when every criterion an attempt aimed at has left `open`.

    This is the difference between arriving and merely advancing. A critic can honestly call an
    attempt validated progress while the criterion it targeted is still open, and the loop has to
    tell those two apart or it will keep rewarding motion in a basin that is going nowhere.
    """

    if not targets:
        return True
    by_id = {item["id"]: item for item in state["criteria"]}
    return all(by_id[criterion_id]["status"] != "open" for criterion_id in targets if criterion_id in by_id)


def _normalized_strategy_value(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)
