"""The cross-workspace signal for the global loop.

Reads every workspace read-only and emits what `commons.py` and
`frontier.py` leave out: overlays with their diagnosis kill-test text,
faults and marks, surgeon classifications and review verdicts, lessons,
barriers, closed basins and whether re-entry carried a shift, per-mode
histograms by lens, harness-gap and control-check counts, verifier results
by rank, rejection classes by mode, spend by mode, pauses, refinements, two
diversity measures, and transcript digests aggregated per workspace and per
model. Fleet joins are computed here, never by a model. No observation
text, payload paths, or charter text ever enters the envelope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from adv_loop.engine import LoopEngine
from adv_loop.errors import LoopError

from . import improvement, killtests, pause, refine, transcripts

# What the global loop is scored on, and what it is explicitly not scored on.
OBJECTIVES = (
    "kill_test_survival_by_target_kind", "targeted_fault_recurrence_per_100_attempts", "targeted_rejection_recurrence",
    "criteria_verified_at_or_above_rank", "strategy_distinctness_per_criterion", "consolidate_vs_disrupt_ratio",
)
GUARDS = ("control_endorsements", "tighten_only_violations", "harness_gap_per_review", "freeze_rails")
NOT_OBJECTIVES = ("completion_rate", "attempts_to_terminal", "retry_count", "transcript_size", "spend",
                  "honest_pause_count", "blocked_or_unsafe_stops", "critic_reject_rate")
GAP_MAP_AGENDA = [
    "R3 representation shift: basin re-entry states a shift but nothing checks it; a refinement can require a constraint-enumeration or renamed-entities artifact before an experiment in a closed basin",
    "R4 expected-set modeling: ideation draws from what an expert would try; require the expected-set list and check seeds against it",
    "R6 preset-stance dissent: critics agree with researchers; sharpen a lens stance",
    "R5 protected budget: low-Elo-but-alive candidates never run; reserve room in triage",
    "R7 generalize move: decomposition splits along the obvious surface; require a generalize step first",
    "R8 lineage over score: selection follows score; require the branch ancestor to be named",
    "R2 attempt before retrieval: retrieval precedes the unassisted attempt; require the attempt first",
    "R1 conventional core plus distant element: combine moves join two in-field assets; label asset distance",
]


def _attempts(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    stored = state.get("attempts") or []
    return list(stored.values()) if isinstance(stored, dict) else list(stored)


def _spend_by_mode(ws: Path) -> Dict[str, float]:
    path = ws / ".spend.jsonl"
    out: Dict[str, float] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        value = row.get("estimated_usd") if row.get("billing") == "api" else row.get("notional_usd")
        if isinstance(value, (int, float)):
            out[row.get("mode", "?")] = round(out.get(row.get("mode", "?"), 0.0) + float(value), 6)
    return out


def distinctness_per_criterion(attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Strategy-level distinctness: distinct six-dimension fingerprints over researcher attempts per criterion."""

    per: Dict[str, List[str]] = {}
    for a in attempts:
        if a.get("role") != "researcher":
            continue
        fp = a.get("strategy_fingerprint")
        for cid in a.get("criterion_targets") or []:
            per.setdefault(cid, []).append(fp)
    return {cid: {"attempts": len(fps), "distinct": len(set(fps)),
                  "ratio": round(len(set(fps)) / len(fps), 3) if fps else None} for cid, fps in per.items()}


def consolidate_vs_disrupt(attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How often a research attempt extends its recent ancestor's basin versus enters a new one."""

    research = [a for a in attempts if a.get("role") == "researcher"]
    consolidate = disrupt = 0
    seen: List[str] = []
    for a in research:
        basin = killtests._normalized(a.get("basin", ""))
        recent = seen[-3:]
        if basin and basin in recent:
            consolidate += 1
        elif basin:
            disrupt += 1
        if basin:
            seen.append(basin)
    total = consolidate + disrupt
    return {"consolidate": consolidate, "disrupt": disrupt,
            "ratio": round(consolidate / total, 3) if total else None}


def workspace_signal(ws: Path) -> Dict[str, Any]:
    engine = LoopEngine(ws)
    state = engine.load()
    local = refine.local_signal(ws, state)
    attempts = _attempts(state)
    reviews = [a for a in attempts if a.get("mode") == "attempt_review"]
    control_checks: Dict[str, int] = {}
    harness_gap = 0
    for r in reviews:
        assessment = r.get("assessment") or {}
        if assessment.get("harness_gap"):
            harness_gap += 1
        outcome = (assessment.get("control_check") or {}).get("outcome")
        if outcome:
            control_checks[outcome] = control_checks.get(outcome, 0) + 1
    evidence = state.get("evidence") or {}
    evidence = evidence if isinstance(evidence, dict) else {e.get("id"): e for e in evidence}
    verifier_by_rank: Dict[str, Dict[str, int]] = {}
    for a in attempts:
        if a.get("role") != "verifier":
            continue
        for result in a.get("verification_results") or []:
            ranks = [evidence.get(eid, {}).get("formalization_rank") for eid in result.get("evidence_ids") or []]
            top = max((r for r in ranks if r in killtests.RANKS), key=killtests.RANKS.index, default="none")
            bucket = verifier_by_rank.setdefault(top, {})
            bucket[result.get("verdict", "?")] = bucket.get(result.get("verdict", "?"), 0) + 1
    criteria_at_rank = {"demanded": 0, "verified_at_or_above": 0}
    for c in state.get("criteria") or []:
        minimum = c.get("min_formalization_rank")
        if not minimum:
            continue
        criteria_at_rank["demanded"] += 1
        ids = (c.get("verification") or {}).get("evidence_ids") or []
        if (c.get("verification") or {}).get("status") == "pass" and any(
                killtests._rank_at_least(evidence.get(eid, {}).get("formalization_rank"), minimum) for eid in ids):
            criteria_at_rank["verified_at_or_above"] += 1
    overlay_reviews = [{"attempt": a.get("id"), "verdict": (a.get("overlay_review") or {}).get("verdict")}
                       for a in attempts if a.get("mode") == "overlay_review"]
    rejections_by_mode: Dict[str, Dict[str, int]] = {}
    for row in transcripts.load_index(ws):
        if row.get("rejected"):
            bucket = rejections_by_mode.setdefault(row.get("mode", "?"), {})
            key = str((row.get("note") or "rejected"))[:60]
            bucket[key] = bucket.get(key, 0) + 1
    digests = transcripts.workspace_digests(ws)
    per_model: Dict[str, Dict[str, Any]] = {}
    for d in digests:
        bucket = per_model.setdefault(d.get("model") or "?", {"sessions": 0, "rejected": 0, "retrieval_first": 0, "tool_calls": 0})
        bucket["sessions"] += 1
        bucket["rejected"] += 1 if d.get("rejected") else 0
        bucket["retrieval_first"] += 1 if d.get("retrieval_before_attempt") else 0
        bucket["tool_calls"] += d.get("tool_calls", 0)
    pause_record = pause.read(ws / pause.PAUSE_FILE)
    return {
        **local,
        "charter_hash": state.get("charter_hash"),
        "profile": (json.loads((ws / "loop-config.json").read_text()) if (ws / "loop-config.json").is_file() else {}).get("profile"),
        "attempt_count": len(attempts),
        "surgeon_classifications": [d.get("classification") for d in local["surgeon_diagnoses"]],
        "overlay_reviews": overlay_reviews,
        "harness_gap_flags": harness_gap,
        "control_checks": control_checks,
        "verifier_by_rank": verifier_by_rank,
        "criteria_at_rank": criteria_at_rank,
        "rejections_by_mode": rejections_by_mode,
        "spend_by_mode": _spend_by_mode(ws),
        "paused": pause.workspace_paused(ws),
        "pause_reason": (pause_record or {}).get("reason"),
        "distinctness": distinctness_per_criterion(attempts),
        "consolidate_vs_disrupt": consolidate_vs_disrupt(attempts),
        "transcripts_per_model": per_model,
    }


def build(root: Path, fleet_state: Optional[Path] = None) -> Dict[str, Any]:
    workspaces: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for ws in sorted(p for p in Path(root).iterdir() if p.is_dir() and (p / "events.jsonl").is_file()):
        try:
            workspaces.append(workspace_signal(ws))
        except (LoopError, OSError, ValueError) as exc:
            errors[ws.name] = str(exc)
    by_mode: Dict[str, Dict[str, int]] = {}
    faults: Dict[str, Dict[str, Any]] = {}
    target_kinds: Dict[str, int] = {}
    for w in workspaces:
        for mode, hist in w["attempts_by_mode"].items():
            bucket = by_mode.setdefault(mode, {})
            for k, v in hist.items():
                bucket[k] = bucket.get(k, 0) + v
        for sig, fault in w["process_faults"].items():
            entry = faults.setdefault(sig, {"count": 0, "workspaces": [], "detail": fault.get("detail")})
            entry["count"] += fault.get("count", 0)
            entry["workspaces"].append(w["workspace"])
        for cls in w["rejection_classes"]:
            target_kinds[f"rejection_class:{cls}"] = target_kinds.get(f"rejection_class:{cls}", 0) + 1
    global_refinements = improvement.current_status(fleet_state or improvement.FLEET_STATE_DIR)
    return {
        "protocol": "adv-loop-global-signal/1",
        "workspaces": workspaces,
        "errors": errors,
        "fleet": {
            "workspace_count": len(workspaces),
            "distinct_task_ids": len({w.get("task_id") for w in workspaces}),
            "distinct_charters": len({w.get("charter_hash") for w in workspaces if w.get("charter_hash")}),
            "attempts_by_mode": by_mode,
            "faults_across_workspaces": {k: v for k, v in faults.items() if len(v["workspaces"]) >= 1},
            "recurring_faults": [k for k, v in faults.items() if len(set(v["workspaces"])) >= 2],
            "lessons_total": sum(len(w["lessons"]) for w in workspaces),
            "closed_basins_total": sum(len(w["basins"]["closed"]) for w in workspaces),
            "reentered_without_shift": [(w["workspace"], b) for w in workspaces for b in w["basins"]["reentered_without_shift"]],
            "fixation_flagged": [w["workspace"] for w in workspaces
                                 if w["fixation"].get("same_procedure_schema_last_n") or w["fixation"].get("first_attempt_retrieved_first")],
        },
        "global_refinements": {rid: {"status": r.get("status"), "kill_test": (r.get("kill_test") or {}).get("outcome")}
                               for rid, r in global_refinements.items()},
        "agenda": GAP_MAP_AGENDA,
    }


def measures(signal: Dict[str, Any], fleet_state: Optional[Path] = None) -> Dict[str, Any]:
    """Scoring computed from chain events only; novelty and acceptability stay separate keys."""

    workspaces = signal["workspaces"]
    refinements = improvement.load_refinements(fleet_state or improvement.FLEET_STATE_DIR)
    by_kind: Dict[str, Dict[str, int]] = {}
    for row in refinements:
        record = row.get("record") or {}
        outcome = (row.get("kill_test") or {}).get("outcome")
        kind = (record.get("target") or {}).get("kind")
        if kind and outcome in ("survived", "killed"):
            bucket = by_kind.setdefault(kind, {"survived": 0, "killed": 0})
            bucket[outcome] += 1
    attempts_by_mode = signal["fleet"]["attempts_by_mode"]
    experiments = sum(attempts_by_mode.get("experiment", {}).values()) or 0
    fault_total = sum(f.get("count", 0) for w in workspaces for f in w["process_faults"].values())
    rejection_total = sum(sum(w["rejection_classes"].values()) for w in workspaces)
    demanded = sum(w["criteria_at_rank"]["demanded"] for w in workspaces)
    verified = sum(w["criteria_at_rank"]["verified_at_or_above"] for w in workspaces)
    ratios = [v["ratio"] for w in workspaces for v in w["distinctness"].values() if v["ratio"] is not None]
    cvd = [w["consolidate_vs_disrupt"]["ratio"] for w in workspaces if w["consolidate_vs_disrupt"]["ratio"] is not None]
    endorsements = sum(w["control_checks"].get("endorses_control", 0) for w in workspaces)
    reviews = sum(sum(h.values()) for w in workspaces for h in w["reviews_by_lens"].values())
    gaps = sum(w["harness_gap_flags"] for w in workspaces)
    return {
        "objectives": {
            "kill_test_survival_by_target_kind": by_kind,
            "targeted_fault_recurrence_per_100_attempts": round(100.0 * fault_total / experiments, 3) if experiments else None,
            "targeted_rejection_recurrence": rejection_total,
            "criteria_verified_at_or_above_rank": {"demanded": demanded, "verified": verified},
            "strategy_distinctness_per_criterion": round(sum(ratios) / len(ratios), 3) if ratios else None,
            "consolidate_vs_disrupt_ratio": round(sum(cvd) / len(cvd), 3) if cvd else None,
        },
        "guards": {
            "control_endorsements": endorsements,
            "tighten_only_violations": 0,
            "harness_gap_per_review": round(gaps / reviews, 3) if reviews else None,
            "freeze_rails": "run by the PR bundle writer on kernel-layer proposals",
        },
        "novelty": {"recorded_separately": True, "closed_basins_reentered_with_shift": None},
        "acceptability": {"recorded_separately": True},
        "excluded": list(NOT_OBJECTIVES),
    }
