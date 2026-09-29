"""The local self-improvement loop for one workspace.

Triggered by recorded failures, never by failure streaks. A fresh-session
proposer returns one refinement or an honest null; mechanical validation
and a baseline check follow; a fresh-session reviewer on a different model
names every anti-pattern and returns adopt, reject, or narrow. Apply is a
snapshot, an append-only write, and a provisional record. Every supervisor
pass evaluates pending kill tests; a killed refinement rolls back exactly
what it added, leaves a lesson memory, and records one process-fault event
with a constant signature so three of them summon the surgeon.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adv_loop.engine import LoopEngine
from adv_loop.errors import LoopError

from . import architecture, assembler, budget, improvement, killtests, provision, routing, transcripts
from .backends import make_backend
from .backends.base import AssembledPrompt, SessionResult, WorkspaceHandle
from .improvement import ANTI_PATTERNS, KILL_TEST_KINDS, LOCAL_STATE_DIR, Refinement

REFINE_ROLES = ("refine_proposer", "narrowness_critic", "adoption_critic")
KILLED_SIGNATURE = hashlib.sha256(b"harness_refinement_killed").hexdigest()
CURSOR_FILE = ".refine-cursor.json"
REJECTION_TRIGGER_COUNT = 3
READ_ONLY_TOOLS = {"read_file", "hash_artifact"}

STRING = {"type": "string", "minLength": 1}
ANTI_PATTERN_OBJECT = {"type": "object", "additionalProperties": False, "required": list(ANTI_PATTERNS),
                       "properties": {name: STRING for name in ANTI_PATTERNS}}
KILL_TEST_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind", "horizon", "horizon_unit"],
    "properties": {
        "kind": {"enum": list(KILL_TEST_KINDS)}, "horizon": {"type": "integer", "minimum": 1},
        "horizon_unit": {"enum": list(improvement.HORIZON_EVENT_KINDS)},
        "mode": {"type": "string"}, "criterion_id": {"type": "string"}, "min_rank": {"type": "string"},
        "signature": {"type": "string"}, "hook": {"type": "string"}, "rejection_class": {"type": "string"},
        "lesson_id": {"type": "string"}, "basin_id": {"type": "string"}, "status": {"type": "string"},
        "control_input": {"type": "object"},
    },
}
REFINEMENT_OBJECT = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "rationale", "expected_outcome", "target", "edits", "does_not_change", "modes_affected",
                 "other_modes_unaffected_because", "alternatives_considered", "kill_test", "anti_patterns_checked", "layer"],
    "properties": {
        "summary": STRING, "rationale": STRING, "expected_outcome": STRING,
        "layer": {"enum": ["supplemental", "kernel"]},
        "target": {"type": "object", "additionalProperties": False, "required": ["kind", "refs"],
                   "properties": {"kind": {"enum": list(improvement.TARGET_KINDS)},
                                  "refs": {"type": "array", "minItems": 1, "items": STRING}}},
        "edits": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["action", "kind", "path", "content"],
                                             "properties": {"action": {"enum": list(improvement.EDIT_ACTIONS)},
                                                            "kind": {"enum": list(improvement.EDIT_KINDS)},
                                                            "path": STRING, "content": STRING, "title": {"type": "string"}}}},
        "does_not_change": {"type": "array", "minItems": 1, "items": STRING},
        "modes_affected": {"type": "array", "minItems": 1, "items": STRING},
        "other_modes_unaffected_because": STRING,
        "alternatives_considered": {"type": "object", "additionalProperties": False,
                                    "required": ["broader", "cheaper", "chosen_because"],
                                    "properties": {"broader": STRING, "cheaper": STRING, "chosen_because": STRING}},
        "kill_test": KILL_TEST_SCHEMA,
        "anti_patterns_checked": ANTI_PATTERN_OBJECT,
        "scope_profile": {"type": "string"},
    },
}
PROPOSAL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False,
    "required": ["proposal", "reason"],
    "properties": {"proposal": {"oneOf": [{"type": "null"}, REFINEMENT_OBJECT]}, "reason": STRING},
}
REVIEW_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False,
    "required": ["verdict", "retained_edits", "anti_patterns", "pre_adoption_outcome", "reasoning"],
    "properties": {"verdict": {"enum": ["adopt", "reject", "narrow"]},
                   "retained_edits": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                   "anti_patterns": ANTI_PATTERN_OBJECT,
                   "pre_adoption_outcome": {"enum": list(improvement.KILL_TEST_OUTCOMES)},
                   "reasoning": STRING},
}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def state_root(ws: Path) -> Path:
    return ws / LOCAL_STATE_DIR


# ---------------------------------------------------------------- signal


def _rejection_classes(ws: Path) -> Dict[str, int]:
    path = ws / ".harness" / "rejections.jsonl"
    counts: Dict[str, int] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            key = str(row.get("error", "unknown"))
            counts[key] = counts.get(key, 0) + 1
    return counts


def local_signal(ws: Path, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read-only view of recorded failures; no observation text, no payload, no charter."""

    state = state if state is not None else LoopEngine(ws).load()
    attempts = state.get("attempts") or []
    attempts = list(attempts.values()) if isinstance(attempts, dict) else list(attempts)
    surgeon_kill_tests = [{"attempt": a.get("id"), "classification": (a.get("diagnosis") or {}).get("classification"),
                           "kill_test": (a.get("diagnosis") or {}).get("kill_test"), "mark": (a.get("demand") or {}).get("mark")}
                          for a in attempts if a.get("role") == "surgeon"]
    by_mode: Dict[str, Dict[str, int]] = {}
    for a in attempts:
        mode = a.get("mode", "?")
        bucket = by_mode.setdefault(mode, {})
        key = (a.get("assessment") or {}).get("verdict") or (a.get("overlay_review") or {}).get("verdict") or a.get("outcome") or "n/a"
        bucket[key] = bucket.get(key, 0) + 1
    reviews_by_lens: Dict[str, Dict[str, int]] = {}
    for a in attempts:
        if a.get("mode") == "attempt_review":
            lens = a.get("lens") or "none"
            verdict = (a.get("assessment") or {}).get("verdict", "?")
            reviews_by_lens.setdefault(lens, {})[verdict] = reviews_by_lens.setdefault(lens, {}).get(verdict, 0) + 1
    basins = state.get("basins") or {}
    closed = [k for k, v in basins.items() if v.get("status") == "closed"]
    reentered_without_shift = []
    for key in closed:
        later = [a for a in attempts if a.get("role") == "researcher" and killtests._normalized(a.get("basin", "")) == key]
        if later and not any(str(a.get("representation_shift") or "").strip() for a in later[-1:]):
            reentered_without_shift.append(key)
    refinements = improvement.current_status(state_root(ws))
    fixation = transcripts.fixation_flags(ws)
    return {
        "workspace": ws.name,
        "task_id": state.get("task_id"),
        "policy_version": state.get("policy_version"),
        "status": state.get("status"),
        "event_head": (state.get("integrity") or {}).get("event_head"),
        "event_count": (state.get("integrity") or {}).get("event_count", 0),
        "failure_streak": state.get("failure_streak", 0),
        "criterion_failure_streaks": state.get("criterion_failure_streaks") or {},
        "criteria": [{"id": c.get("id"), "status": c.get("status"), "min_rank": c.get("min_formalization_rank"),
                      "verification": (c.get("verification") or {}).get("status")} for c in state.get("criteria") or []],
        "lessons": state.get("lessons") or [],
        "barriers": [{"id": b.get("id"), "name": b.get("name"), "probes": len(b.get("probes") or [])} for b in state.get("barriers") or []],
        "process_faults": state.get("process_faults") or {},
        "surgeon_marks": state.get("surgeon_marks") or [],
        "surgeon_diagnoses": surgeon_kill_tests,
        "overlays": [{"overlay_id": o.get("overlay_id"), "verdict": o.get("verdict"), "ops": [op.get("op") for op in (o.get("delta") or {}).get("ops", [])]}
                     for o in state.get("overlays") or []],
        "basins": {"total": len(basins), "closed": closed, "reentered_without_shift": reentered_without_shift,
                   # A stall is an attempt the critic endorsed that still left its criteria open. Grinding
                   # one approach produces stalls and nothing else, so without these the loop cannot see
                   # the single pattern an operator would otherwise have to point out.
                   "stalls": {k: v.get("stalls", 0) for k, v in basins.items() if v.get("stalls")}},
        "attempts_by_mode": by_mode,
        "reviews_by_lens": reviews_by_lens,
        "rejection_classes": _rejection_classes(ws),
        "refinements": {rid: {"status": r.get("status"), "kill_test": (r.get("kill_test") or {}).get("outcome"),
                              "target": (r.get("record") or {}).get("target")} for rid, r in refinements.items()},
        "fixation": fixation,
        "validator_hooks": [h.get("hook_id") for h in state.get("validator_hooks") or []],
    }


def load_cursor(ws: Path) -> Dict[str, Any]:
    path = state_root(ws) / CURSOR_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cursor(ws: Path, cursor: Dict[str, Any]) -> None:
    root = state_root(ws)
    root.mkdir(parents=True, exist_ok=True)
    (root / CURSOR_FILE).write_text(json.dumps(cursor, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cursor_from(signal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "lessons": [l.get("id") for l in signal["lessons"]],
        "barriers": [b.get("id") for b in signal["barriers"]],
        "overlays": [o.get("overlay_id") for o in signal["overlays"]],
        "faults": {k: v.get("count", 0) for k, v in signal["process_faults"].items()},
        "rejection_classes": dict(signal["rejection_classes"]),
        "killed": sorted(rid for rid, r in signal["refinements"].items() if r.get("status") == "killed"),
        "closed_basins": sorted(signal["basins"]["closed"]),
        "basin_stalls": dict(signal["basins"].get("stalls") or {}),
        "fixation_flagged": bool(signal["fixation"].get("same_procedure_schema_last_n") or signal["fixation"].get("first_attempt_retrieved_first")),
        "at": utc_now(),
    }


def triggers(signal: Dict[str, Any], cursor: Dict[str, Any]) -> List[str]:
    now = cursor_from(signal)
    fired = []
    for key in ("lessons", "barriers", "overlays", "killed", "closed_basins"):
        new = sorted(set(now[key]) - set(cursor.get(key, [])))
        if new:
            fired.append(f"new_{key}:{','.join(map(str, new))}")
    for sig, count in now["faults"].items():
        if count > cursor.get("faults", {}).get(sig, 0):
            fired.append(f"fault_count:{sig[:12]}:{count}")
    for cls, count in now["rejection_classes"].items():
        if count >= REJECTION_TRIGGER_COUNT > cursor.get("rejection_classes", {}).get(cls, 0):
            fired.append(f"rejection_class:{cls}:{count}")
    for basin, stalls in now["basin_stalls"].items():
        if stalls > cursor.get("basin_stalls", {}).get(basin, 0):
            fired.append(f"basin_stalled:{basin[:24]}:{stalls}")
    if signal["basins"]["reentered_without_shift"] and not cursor.get("reentered_flagged"):
        fired.append("basin_reentered_without_shift")
    if now["fixation_flagged"] and not cursor.get("fixation_flagged"):
        fired.append("fixation")
    return fired


# ---------------------------------------------------------------- sessions


def spend_today(ws: Path) -> float:
    path = ws / budget.SPEND_FILE
    if not path.is_file():
        return 0.0
    today = utc_now()[:10]
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("role") in REFINE_ROLES and str(row.get("at", "")).startswith(today):
            value = row.get("notional_usd") if row.get("billing") != "api" else row.get("estimated_usd")
            total += float(value or 0.0)
    return round(total, 6)


def run_refine_session(runtime, ws: Path, role: str, kind: str, envelope: Dict[str, Any], schema: Dict[str, Any]) -> Tuple[SessionResult, AssembledPrompt, Dict[str, Any]]:
    """A fresh session for a refine role; the runtime may inject a stub for tests."""

    directive = {"action": "attempt", "directive_id": f"refine:{ws.name}:{uuid.uuid4().hex[:12]}", "role": role, "mode": "refine"}
    stub = getattr(runtime, "refine_session", None)
    assignment = None
    if not stub:
        config = provision.read_config(ws)
        assignment = runtime.router_factory(config).resolve(role)
    architecture.ensure(ws)
    prompt = assembler.assemble_refine(kind, envelope, schema, workspace=ws,
                                       architecture_tier=assignment.spec.architecture_tier if assignment else None)
    if stub:
        result = stub(role, prompt, directive)
        spec_model = result.resolved_model or "stub"
        row = {"estimated_usd": 0.0}
    else:
        spec = assignment.spec
        if not runtime.api_guard.allows(spec, assignment.budget.max_budget_usd):
            raise RuntimeError("api budget stop; refine session refused")
        handle = WorkspaceHandle(ws_id=ws.name, path=ws, session_id=uuid.uuid4().hex,
                                 container=(config.get("harness") or {}).get("container") if runtime.containers else None)
        backend = make_backend(spec, handle, docker=runtime.docker, overrides=runtime.backend_overrides.get(spec.backend),
                               tool_allow=READ_ONLY_TOOLS)
        result = backend.run(directive, handle, spec, assignment.budget, prompt)
        row = budget.record_spend(ws, spec, result.usage, role=role, mode="refine", directive_id=directive["directive_id"],
                                  session_id=handle.session_id, base_prompt_hash=prompt.manifest["base_prompt_hash"])
        spec_model = spec.model
    transcripts.persist(ws, result, prompt, directive=directive, model=spec_model, submission=result.model_output,
                        rejection=None if result.ok else [result.ended], accepted=None, spend_usd=row.get("estimated_usd"))
    return result, prompt, {"session_id": result.session_id, "model": spec_model, "ended": result.ended}


# ---------------------------------------------------------------- the pass


def _exists(signal: Dict[str, Any]) -> Callable[[str, str], bool]:
    def resolver(kind: str, ref: str) -> bool:
        if kind == "lesson":
            return any(l.get("id") == ref for l in signal["lessons"])
        if kind == "barrier":
            return any(b.get("id") == ref for b in signal["barriers"])
        if kind == "fault_signature":
            return ref in signal["process_faults"]
        if kind == "overlay":
            return any(o.get("overlay_id") == ref for o in signal["overlays"])
        if kind == "surgeon_mark":
            return ref in signal["surgeon_marks"]
        if kind == "basin":
            return killtests._normalized(ref) in signal["basins"]["closed"]
        if kind == "rejection_class":
            return ref in signal["rejection_classes"]
        if kind == "fixation":
            return bool(signal["fixation"].get(ref))
        return False
    return resolver


def next_id(ws: Path) -> str:
    existing = improvement.current_status(state_root(ws))
    return f"LR{len(existing) + 1:04d}"


def refine_local(runtime, ws: Path, *, dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
    report: Dict[str, Any] = {"workspace": ws.name, "at": utc_now()}
    engine = LoopEngine(ws)
    report["evaluated"] = evaluate_pending(ws, engine=engine)
    state = engine.load()
    signal = local_signal(ws, state)
    cursor = load_cursor(ws)
    fired = triggers(signal, cursor)
    report["triggers"] = fired
    if not fired and not force:
        report["skipped"] = "no_trigger"
        return report
    cap = getattr(runtime, "refine_max_usd_per_day", 2.0)
    spent = spend_today(ws)
    if spent >= cap:
        report["skipped"] = f"refine budget reached ({spent} >= {cap})"
        return report

    envelope = {"protocol": "adv-loop-refine/1", "scope": "local", "signal": signal, "triggers": fired,
                "transcript_dir": str(ws / transcripts.TRANSCRIPTS / "sessions"),
                "supplemental_root": LOCAL_STATE_DIR, "refinement_id": next_id(ws)}
    result, prompt, info = run_refine_session(runtime, ws, "refine_proposer", "proposer", envelope, PROPOSAL_SCHEMA)
    report["proposer"] = info
    if not result.ok or not isinstance(result.model_output, dict):
        report["skipped"] = f"proposer session ended {result.ended}"
        save_cursor(ws, cursor_from(signal))
        return report
    proposal = result.model_output.get("proposal")
    if proposal is None:
        report["null"] = result.model_output.get("reason")
        save_cursor(ws, cursor_from(signal))
        return report

    record = Refinement.from_dict({**proposal, "id": envelope["refinement_id"], "scope": "local",
                                   "proposer": {"model": info["model"], "session_id": info["session_id"]}})
    problems = improvement.validate(record, workspace=ws, exists=_exists(signal))
    adopted_seq = int(signal["event_count"])
    base = killtests.baseline(ws, record.kill_test.as_dict(), adopted_seq, adopted_at=utc_now(), state=state)
    if not base.get("discriminating"):
        problems.append(f"unfalsifiable_kill_test: the pre-adoption window already comes out {base['outcome']}")
    report["record"] = record.as_dict()
    report["validation"] = problems
    report["baseline"] = base
    if problems:
        report["rejected_by"] = "mechanical"
        improvement._append_row(state_root(ws), {"id": record.id, "status": "rejected", "record": record.as_dict(),
                                                 "problems": problems, "baseline": base, "at": utc_now()})
        save_cursor(ws, cursor_from(signal))
        return report
    if dry_run:
        report["dry_run"] = True
        return report

    review_envelope = {"protocol": "adv-loop-refine-review/1", "scope": "local", "refinement": record.as_dict(),
                       "baseline": base, "signal": signal, "transcript_dir": envelope["transcript_dir"]}
    review, review_prompt, review_info = run_refine_session(runtime, ws, "adoption_critic", "adoption_critic",
                                                            review_envelope, REVIEW_SCHEMA)
    report["reviewer"] = review_info
    verdict = review.model_output if review.ok and isinstance(review.model_output, dict) else None
    if verdict is None:
        report["rejected_by"] = f"reviewer session ended {review.ended}"
        save_cursor(ws, cursor_from(signal))
        return report
    missing = [n for n in ANTI_PATTERNS if not str(verdict.get("anti_patterns", {}).get(n, "")).strip()]
    if missing or verdict.get("verdict") == "reject":
        report["rejected_by"] = "reviewer" if not missing else f"reviewer omitted anti-patterns {missing}"
        report["review"] = verdict
        improvement._append_row(state_root(ws), {"id": record.id, "status": "rejected", "record": record.as_dict(),
                                                 "review": verdict, "at": utc_now()})
        save_cursor(ws, cursor_from(signal))
        return report
    if verdict.get("verdict") == "narrow":
        keep = sorted(set(int(i) for i in verdict.get("retained_edits", [])))
        record.edits = [e for i, e in enumerate(record.edits) if i in keep]
        if not record.edits:
            report["rejected_by"] = "reviewer narrowed to nothing"
            save_cursor(ws, cursor_from(signal))
            return report
    row = improvement.apply(record, workspace=ws, base_prompt_hash=assembler.prompt_hash("base.md"),
                            reviewer={"model": review_info["model"], "session_id": review_info["session_id"], "verdict": verdict})
    improvement._append_row(state_root(ws), {"id": record.id, "adopted_seq": adopted_seq, "adopted_at": row["applied_at"],
                                             "status": "provisional"})
    report["applied"] = {"id": record.id, "adopted_seq": adopted_seq, "edits": [e.path for e in record.edits]}
    report["review"] = verdict
    save_cursor(ws, cursor_from(signal))
    return report


# ---------------------------------------------------------------- evaluation


def evaluate_pending(ws: Path, engine: Optional[LoopEngine] = None, run_control=None) -> List[Dict[str, Any]]:
    root = state_root(ws)
    rows = improvement.current_status(root)
    if not rows:
        return []
    engine = engine or LoopEngine(ws)
    state = engine.load()
    outcomes = []
    for rid, row in rows.items():
        if row.get("status") != "provisional" or "record" not in row:
            continue
        kt = (row["record"].get("kill_test") or {})
        adopted_seq = int(row.get("adopted_seq", 0))
        verdict = killtests.evaluate(ws, kt, adopted_seq, adopted_at=row.get("adopted_at"), state=state, run_control=run_control)
        if verdict["outcome"] == "pending":
            outcomes.append({"id": rid, **verdict})
            continue
        improvement.record_outcome(root, rid, verdict["outcome"], verdict.get("detail"))
        if verdict["outcome"] == "killed":
            rolled = improvement.rollback(root, rid, reason="kill test killed")
            _memory(ws, f"Refinement {rid} ({row['record'].get('summary', '')[:80]}) was killed by its {kt.get('kind')} test; "
                        f"its target {row['record'].get('target')} still stands. Do not repeat the same edit.",
                    modes=row["record"].get("modes_affected") or [], source=f"refinement_killed:{rid}")
            try:
                engine.record_process_fault({"request_id": f"fault:refinement:{rid}", "signature": KILLED_SIGNATURE,
                                             "source": "harness:refinement_killed",
                                             "detail": f"{rid} {kt.get('kind')} killed: {json.dumps(verdict.get('detail'))[:150]}"})
                fault = "recorded"
            except LoopError as exc:
                fault = f"refused: {exc}"
            verdict = {**verdict, "rollback": rolled, "fault": fault}
        outcomes.append({"id": rid, **verdict})
    return outcomes


def _memory(ws: Path, text: str, *, modes: List[str], source: str) -> None:
    root = state_root(ws)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "memories.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"text": text, "modes": modes, "source": source, "at": utc_now()}, sort_keys=True) + "\n")
