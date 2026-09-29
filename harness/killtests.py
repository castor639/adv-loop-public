"""Kill-test evaluation from the event log alone.

A kill test names a closed kind and a horizon counted in relevant chain
events, never wall-clock time. `evaluate` reads the events after adoption;
`baseline` runs the same test over the window immediately before adoption
and must come out `killed`, otherwise the test cannot tell "worked" from
"nothing changed". The only kind that acts is `checker_rejects_control`,
which runs the registered checker on the declared control input.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adv_loop.engine import LoopEngine

try:
    from adv_loop.policy import FORMALIZATION_RANKS as RANKS
except ImportError:  # keep the evaluator usable if the constant moves
    RANKS = ("sourced_claim", "replicated_experiment", "executable_spec", "smt_discharge", "model_check", "kernel_proof")

ABSENT_KINDS = ("fault_signature_absent", "rejection_class_absent", "surgeon_not_redemanded")
REACH_KINDS = ("criterion_reaches", "verification_passes_at_rank", "checker_accepts", "review_validates",
               "lesson_answered", "basin_left")
ACTION_KINDS = ("checker_rejects_control",)
TERMINAL = ("completed", "blocked", "unsafe", "budget_exhausted")


def load_events(ws: Path) -> List[Dict[str, Any]]:
    path = ws / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def head_seq(ws: Path) -> int:
    events = load_events(ws)
    return int(events[-1]["seq"]) if events else 0


def _attempt(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event.get("type") != "attempt_recorded":
        return None
    return (event.get("payload") or {}).get("attempt")


def _normalized(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _rank_at_least(rank: Optional[str], minimum: Optional[str]) -> bool:
    if not minimum:
        return True
    if rank not in RANKS or minimum not in RANKS:
        return False
    return RANKS.index(rank) >= RANKS.index(minimum)


def _attempt_by_id(events: List[Dict[str, Any]], attempt_id: str) -> Optional[Dict[str, Any]]:
    for event in events:
        attempt = _attempt(event)
        if attempt and attempt.get("id") == attempt_id:
            return attempt
    return None


def relevant(events: List[Dict[str, Any]], kt: Dict[str, Any], all_events: List[Dict[str, Any]]) -> List[int]:
    """Sequence numbers of the events that count toward the horizon."""

    unit = kt.get("horizon_unit", "attempts_of_mode")
    out = []
    for event in events:
        attempt = _attempt(event)
        if attempt is None:
            continue
        if unit == "attempts":
            out.append(event["seq"])
        elif unit == "attempts_of_mode":
            if attempt.get("mode") == (kt.get("mode") or "experiment"):
                out.append(event["seq"])
        elif unit == "verifications":
            if attempt.get("role") == "verifier":
                out.append(event["seq"])
        elif unit == "diagnoses":
            if attempt.get("role") == "surgeon":
                out.append(event["seq"])
        elif unit == "reviews_of_criterion":
            if attempt.get("mode") == "attempt_review":
                target = _attempt_by_id(all_events, (attempt.get("assessment") or {}).get("target_attempt_id", ""))
                if target and kt.get("criterion_id") in (target.get("criterion_targets") or []):
                    out.append(event["seq"])
    return out


def _check(kind: str, window: List[Dict[str, Any]], all_events: List[Dict[str, Any]], state: Dict[str, Any],
           kt: Dict[str, Any], rejections: List[Dict[str, Any]]) -> Tuple[bool, Any]:
    """True when the kind's condition is observed inside the window."""

    if kind == "fault_signature_absent":
        hits = [e["seq"] for e in window if e.get("type") == "process_fault_recorded"
                and (e.get("payload") or {}).get("signature") == kt.get("signature")]
        return bool(hits), {"fault_events": hits}
    if kind == "rejection_class_absent":
        wanted = kt.get("rejection_class")
        hits = [r for r in rejections if r.get("error") == wanted or wanted in str(r.get("message", ""))]
        return bool(hits), {"rejections": len(hits)}
    if kind == "surgeon_not_redemanded":
        sig = kt.get("signature")
        hits = []
        for e in window:
            attempt = _attempt(e)
            if attempt and attempt.get("role") == "surgeon":
                demand = attempt.get("demand") or {}
                mark = str(demand.get("mark", ""))
                if demand.get("signature") == sig or mark.startswith(f"fault:{sig}"):
                    hits.append(attempt.get("id"))
        return bool(hits), {"surgeon_attempts": hits}
    if kind == "criterion_reaches":
        wanted_status = kt.get("status") or "satisfied"
        for e in window:
            attempt = _attempt(e)
            if not attempt:
                continue
            for update in attempt.get("criterion_updates") or []:
                if update.get("id") == kt.get("criterion_id") and update.get("status") == wanted_status:
                    if not kt.get("min_rank"):
                        return True, {"attempt": attempt.get("id")}
                    for ev in attempt.get("evidence") or []:
                        if kt.get("criterion_id") in (ev.get("supports") or []) and _rank_at_least(ev.get("formalization_rank"), kt["min_rank"]):
                            return True, {"attempt": attempt.get("id"), "rank": ev.get("formalization_rank")}
        return False, {}
    if kind == "verification_passes_at_rank":
        evidence_map = state.get("evidence") or {}
        for e in window:
            attempt = _attempt(e)
            if not attempt or attempt.get("role") != "verifier":
                continue
            for result in attempt.get("verification_results") or []:
                if result.get("criterion_id") != kt.get("criterion_id") or result.get("verdict") != "pass":
                    continue
                if not kt.get("min_rank"):
                    return True, {"attempt": attempt.get("id")}
                for eid in result.get("evidence_ids") or []:
                    record = evidence_map.get(eid) if isinstance(evidence_map, dict) else None
                    if record and _rank_at_least(record.get("formalization_rank"), kt["min_rank"]):
                        return True, {"attempt": attempt.get("id"), "evidence": eid}
        return False, {}
    if kind == "checker_accepts":
        prefix = f"validate:{kt.get('hook')}"
        for e in window:
            attempt = _attempt(e)
            if not attempt:
                continue
            for ev in attempt.get("evidence") or []:
                checker = ev.get("checker") or {}
                locator = str(ev.get("locator", ""))
                if (locator.startswith(prefix) or checker.get("checker_id") == kt.get("hook")) and checker.get("accepted") is True:
                    return True, {"attempt": attempt.get("id"), "locator": locator}
        return False, {}
    if kind == "review_validates":
        for e in window:
            attempt = _attempt(e)
            if not attempt or attempt.get("mode") != "attempt_review":
                continue
            assessment = attempt.get("assessment") or {}
            if assessment.get("verdict") != "validated_progress":
                continue
            target = _attempt_by_id(all_events, assessment.get("target_attempt_id", ""))
            if target and (not kt.get("mode") or target.get("mode") == kt["mode"]):
                if not kt.get("criterion_id") or kt["criterion_id"] in (target.get("criterion_targets") or []):
                    return True, {"review": attempt.get("id"), "target": target.get("id")}
        return False, {}
    if kind == "lesson_answered":
        for e in window:
            attempt = _attempt(e)
            if not attempt or attempt.get("role") != "planner":
                continue
            for item in (attempt.get("plan") or {}).get("lessons_addressed") or []:
                if item.get("lesson_id") == kt.get("lesson_id"):
                    return True, {"attempt": attempt.get("id")}
        return False, {}
    if kind == "basin_left":
        stuck = _normalized(kt.get("basin_id", ""))
        for e in window:
            attempt = _attempt(e)
            if not attempt or attempt.get("role") != "researcher":
                continue
            if kt.get("criterion_id") and kt["criterion_id"] not in (attempt.get("criterion_targets") or []):
                continue
            basin = _normalized(attempt.get("basin", ""))
            shift = str(attempt.get("representation_shift") or "").strip()
            if basin and basin != stuck and shift:
                return True, {"attempt": attempt.get("id"), "basin": attempt.get("basin")}
        return False, {}
    raise ValueError(f"unknown kill-test kind {kind!r}")


def _rejections(ws: Path, since: Optional[str], until: Optional[str]) -> List[Dict[str, Any]]:
    path = ws / ".harness" / "rejections.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = str(row.get("at", ""))
        if since and at < since:
            continue
        if until and at >= until:
            continue
        rows.append(row)
    return rows


def evaluate(ws: Path, kt: Dict[str, Any], adopted_seq: int, *, adopted_at: Optional[str] = None,
             state: Optional[Dict[str, Any]] = None, run_control: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Outcome of one kill test over the events after adoption."""

    kind = kt["kind"]
    events = load_events(ws)
    state = state if state is not None else LoopEngine(ws).load()
    if kind in ACTION_KINDS:
        return _run_control(ws, kt, run_control)
    after = [e for e in events if int(e["seq"]) > adopted_seq]
    counted = relevant(after, kt, events)
    horizon = int(kt.get("horizon", 1))
    reached = len(counted) >= horizon
    window = after if not reached else [e for e in after if int(e["seq"]) <= counted[horizon - 1]]
    rejections = _rejections(ws, adopted_at, None)
    hit, detail = _check(kind, window, events, state, kt, rejections)
    base = {"kind": kind, "relevant_seen": len(counted), "horizon": horizon, "detail": detail,
            "window": {"after_seq": adopted_seq, "through_seq": window[-1]["seq"] if window else adopted_seq}}
    if kind in ABSENT_KINDS:
        if hit:
            return {**base, "outcome": "killed"}
        if reached:
            return {**base, "outcome": "survived"}
    else:
        if hit:
            return {**base, "outcome": "survived"}
        if reached:
            return {**base, "outcome": "killed"}
    if state.get("status") in TERMINAL:
        return {**base, "outcome": "stale", "reason": f"workspace is {state.get('status')} before the horizon"}
    return {**base, "outcome": "pending"}


def baseline(ws: Path, kt: Dict[str, Any], adopted_seq: int, *, adopted_at: Optional[str] = None,
             state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The same test over the window immediately before adoption; discriminating iff killed."""

    kind = kt["kind"]
    if kind in ACTION_KINDS:
        hooks = {h.get("hook_id") for h in (state or {}).get("validator_hooks", [])} if state else set()
        registered = kt.get("hook") in hooks
        return {"kind": kind, "outcome": "survived" if registered else "killed", "discriminating": not registered,
                "reason": "action test; discriminating when the hook was not registered before adoption"}
    events = load_events(ws)
    state = state if state is not None else LoopEngine(ws).load()
    before = [e for e in events if int(e["seq"]) <= adopted_seq]
    counted = relevant(before, kt, events)
    horizon = int(kt.get("horizon", 1))
    tail = counted[-horizon:]
    # fewer relevant events than the horizon: the whole history before adoption is the window
    start = tail[0] if len(tail) >= horizon else 1
    window = [e for e in before if int(e["seq"]) >= start]
    start_at = next((e.get("at") for e in window), None)
    rejections = _rejections(ws, start_at, adopted_at)
    hit, detail = _check(kind, window, events, state, kt, rejections)
    outcome = ("killed" if hit else "survived") if kind in ABSENT_KINDS else ("survived" if hit else "killed")
    return {"kind": kind, "outcome": outcome, "discriminating": outcome == "killed", "detail": detail,
            "relevant_seen": len(tail), "window": {"from_seq": start, "through_seq": adopted_seq}}


def _run_control(ws: Path, kt: Dict[str, Any], run_control: Optional[Callable[..., Dict[str, Any]]]) -> Dict[str, Any]:
    if run_control is None:
        from adv_loop.validators import validate_report
        run_control = validate_report
    try:
        report = run_control(ws, kt["hook"], kt.get("control_input"))
    except Exception as exc:
        return {"kind": kt["kind"], "outcome": "killed", "detail": {"error": str(exc)}}
    result = report.get("result") or {}
    if not report.get("ok"):
        return {"kind": kt["kind"], "outcome": "killed", "detail": {"report": {k: report.get(k) for k in ("ok", "exit_code", "failure")}}}
    accepted = result.get("accepted")
    return {"kind": kt["kind"], "outcome": "survived" if accepted is False else "killed",
            "detail": {"accepted": accepted, "checker_id": result.get("checker_id")}}
