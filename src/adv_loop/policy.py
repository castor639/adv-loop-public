from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from .storage import object_hash


POLICY_VERSION = "5.0"

# Every version listed here replays with its original semantics forever. A
# workspace is pinned to the policy version stamped into its task_created
# event; the "2.0" code paths in this module are frozen and must never change
# behavior, because existing event logs replay against them and their
# projections are byte-audited. Each later version inherits everything before
# it and adds structure on top; version-only state and gates initialize only
# under their own predicate (is_v3 / is_v4 / is_v5).
SUPPORTED_POLICY_VERSIONS = ("2.0", "3.0", "4.0", "5.0")

STRATEGY_DIMENSIONS = (
    "decomposition",
    "source_class",
    "retrieval_method",
    "reasoning_method",
    "tool",
    "verification_method",
)

# ---------------------------------------------------------------------------
# 2.0 (frozen)
# ---------------------------------------------------------------------------

ESCALATION_LADDER: Tuple[Tuple[int, str, str], ...] = (
    (3, "critic", "contradiction_search"),
    (4, "planner", "decompose"),
    (5, "planner", "fresh_replan"),
    (7, "critic", "blocker_audit"),
)

# ---------------------------------------------------------------------------
# 3.0
# ---------------------------------------------------------------------------

RESEARCH_MOVES = ("test", "survey", "barrier_probe", "combine", "replicate")
REVIEW_LENSES = ("correctness", "novelty", "proves_too_much", "simplification")
IDEATION_KINDS = ("broad", "evolve")
BARRIER_PROBE_PATTERNS = ("bound", "dual", "shift_representation")

SEED_MIN = 3
SEED_MAX = 24
SEED_REQUIRED_FIELDS = ("claim", "basin", "first_unjustified_step", "kill_test")
SEED_OPTIONAL_FIELDS = ("control_object", "needs", "parents", "representation_shift")
TRIAGE_PROMOTION_CAP = 3
BASIN_CLOSURE_FAILURES = 2
# 4.0: an attempt the critic endorsed that still left its criteria open is a stall, not an arrival.
# Stalls close a basin more slowly than confirmed failures, but they do close it, so a run of
# near-misses in one basin forces a re-representation without anyone outside the loop saying so.
BASIN_STALL_CLOSURE = 3
MULTI_CAUSE_STREAK = 3
EVOLVE_TOP_K = 5
CANDIDATE_SURFACE_LIMIT = 10
ELO_INITIAL = 1000.0
ELO_K = 32.0

# One full cycle of escalation demands. A rung's threshold is
# offset + LADDER_SPAN * cycle, streaks are tracked per criterion, and a
# completed rung is marked "<demand>@<criterion>#<cycle>" (move rungs use the
# move name), so an unbounded per-criterion failure streak yields an unbounded
# stream of distinct demands and validated progress on one criterion never
# silences the ladder for another. This shape is frozen for 3.0: recorded
# event logs replay against it.
LADDER_CYCLE: Tuple[Tuple[int, str, str, Optional[str]], ...] = (
    (2, "explorer", "ideation", None),
    (3, "critic", "contradiction_search", None),
    (4, "planner", "decompose", None),
    (5, "planner", "fresh_replan", None),
    (6, "researcher", "experiment", "survey"),
    (7, "critic", "blocker_audit", None),
    (8, "researcher", "experiment", "barrier_probe"),
    (9, "researcher", "experiment", "combine"),
)
LADDER_SPAN = 8

# ---------------------------------------------------------------------------
# 4.0: formalization ranks
# ---------------------------------------------------------------------------

# Weakest to strongest. A criterion may demand a minimum rank; satisfying and
# verifying it then require checker-attested evidence at or above that rank.
# The ordering is frozen for 4.0: recorded event logs replay against it.
FORMALIZATION_RANKS = (
    "sourced_claim",
    "replicated_experiment",
    "executable_spec",
    "smt_discharge",
    "model_check",
    "kernel_proof",
)
# Ranks at or above this floor claim a mechanical check and therefore require
# an accepting checker record on the evidence itself.
CHECKED_RANK_FLOOR = "smt_discharge"

# ---------------------------------------------------------------------------
# 4.0: harness diagnosis (surgeon) and workspace overlays
# ---------------------------------------------------------------------------

# One process-fault event already represents a whole exhausted retry burst, so
# three repeats of one signature is a persistent harness condition, not a
# coincidence of transient failures. An overlay stall class may tighten a
# known signature down to 2; it may never loosen above this default.
SURGEON_FAULT_THRESHOLD = 3
SURGEON_CLASSIFICATIONS = ("harness_gap", "research_failure", "human_dependency")
OVERLAY_REVIEW_VERDICTS = ("adopt", "reject", "narrow")
OVERLAY_SCHEMA_VERSION = 1
# The whole safety of self-amendment: an overlay may only perform these
# operations, and each one adds or tightens. Nothing here can skip a review,
# loosen a gate, reinterpret a terminal state, or touch another workspace.
OVERLAY_OPS = (
    "register_evidence_kind",
    "register_validator_hook",
    "register_sandbox",
    "add_stall_class",
    "add_role_instructions",
    "add_criterion",
    "require_min_rank",
    "raise_criterion_rank",
    "pin_toolchain_hash",
)
# What a human is actually for. "harness_gap" is deliberately absent: a gap in
# this loop's own contract is surgeon work, never an ask-human.
HUMAN_INPUT_CLASSIFICATIONS = (
    "external_dependency",
    "authorization",
    "private_data",
    "safety_boundary",
    "other_human_judgment",
)
KNOWN_MODES = (
    "initial_plan", "experiment", "attempt_review", "contradiction_search", "decompose",
    "fresh_replan", "blocker_audit", "independent_verification", "final_report",
    "ideation", "triage", "overlay_diagnosis", "overlay_review",
)


def fault_threshold_for(state: Dict[str, Any], signature: str) -> int:
    entry = state.get("stall_classes", {}).get(signature)
    if entry:
        return entry["threshold"]
    return SURGEON_FAULT_THRESHOLD


def surgeon_demand(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The one owed harness-diagnosis obligation for a 4.0 state, or None.

    Priority: repeating process faults, then critic-flagged harness gaps, then
    rank-gated criteria with no registered checker backend. Iteration is
    deterministic — fault insertion order, attempt order, criterion order —
    and marks are cyclic per fault signature, so a diagnosed signature stays
    silent until a full threshold of new faults lands. Never keyed to failure
    streaks: ordinary scientific failure stays on the escalation ladder. This
    shape is frozen for 4.0: recorded event logs replay against it.
    """

    if not is_v4(state):
        return None
    marks = set(state.get("surgeon_marks", []))
    for signature, fault in state.get("process_faults", {}).items():
        threshold = fault_threshold_for(state, signature)
        for cycle in range(1, fault["count"] // threshold + 1):
            mark = f"fault:{signature}#{cycle}"
            if mark not in marks:
                return {
                    "kind": "fault",
                    "mark": mark,
                    "signature": signature,
                    "fault": dict(fault),
                    "threshold": threshold,
                    "cycle": cycle,
                }
    for attempt in state["attempts"]:
        if (
            attempt["role"] == "critic"
            and attempt["mode"] == "attempt_review"
            and attempt.get("assessment", {}).get("harness_gap") is True
        ):
            mark = f"gap:{attempt['id']}"
            if mark not in marks:
                return {"kind": "gap_flag", "mark": mark, "critic_attempt_id": attempt["id"]}
    hooks = state.get("validator_hooks", [])
    for criterion in state["criteria"]:
        minimum = criterion.get("min_formalization_rank")
        if minimum is None or not rank_at_least(minimum, CHECKED_RANK_FLOOR):
            continue
        if criterion["status"] == "satisfied":
            continue
        if any(rank_at_least(hook["rank"], minimum) for hook in hooks):
            continue
        mark = f"backend:{criterion['id']}:{minimum}"
        if mark not in marks:
            return {
                "kind": "rank_backend",
                "mark": mark,
                "criterion_id": criterion["id"],
                "required_rank": minimum,
            }
    return None


def rank_index(rank: str) -> int:
    return FORMALIZATION_RANKS.index(rank)


def rank_at_least(rank: str, minimum: str) -> bool:
    return rank_index(rank) >= rank_index(minimum)


def rank_satisfies(evidence_item: Dict[str, Any], minimum: Optional[str]) -> bool:
    """Whether one evidence item meets a criterion's minimum formalization rank."""

    if minimum is None:
        return True
    rank = evidence_item.get("formalization_rank")
    return rank is not None and rank_at_least(rank, minimum)


def policy_version_of(state: Dict[str, Any]) -> str:
    return state.get("policy_version") or POLICY_VERSION


def is_v3(state: Dict[str, Any]) -> bool:
    return policy_version_of(state) != "2.0"


def is_v4(state: Dict[str, Any]) -> bool:
    return policy_version_of(state) not in ("2.0", "3.0")


def is_v5(state: Dict[str, Any]) -> bool:
    return policy_version_of(state) not in ("2.0", "3.0", "4.0")


def required_strategy_changes(state: Dict[str, Any]) -> int:
    if not any(attempt["role"] == "researcher" for attempt in state["attempts"]):
        return 0
    failures = state["failure_streak"]
    if failures >= 5:
        return 3
    if failures >= 2:
        return 2
    return 1


def unresolved_critical_contradictions(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        contradiction
        for contradiction in state["contradictions"]
        if contradiction["severity"] == "critical" and contradiction["status"] == "open"
    ]


def criteria_evidence_ready(state: Dict[str, Any]) -> bool:
    evidence = state["evidence"]
    if not state["criteria"] or unresolved_critical_contradictions(state):
        return False
    for criterion in state["criteria"]:
        if criterion["status"] != "satisfied" or not criterion["evidence_ids"]:
            return False
        minimum = criterion.get("min_formalization_rank")
        if not any(
            evidence.get(evidence_id, {}).get("quality") == "direct"
            and criterion["id"] in evidence.get(evidence_id, {}).get("supports", [])
            and rank_satisfies(evidence.get(evidence_id, {}), minimum)
            for evidence_id in criterion["evidence_ids"]
        ):
            return False
    return True


def outstanding_escalation(state: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    completed = set(state["escalations_completed"])
    for threshold, role, mode in ESCALATION_LADDER:
        if state["failure_streak"] >= threshold and mode not in completed:
            return role, mode
    return None


# ---------------------------------------------------------------------------
# 3.0 escalation: per-criterion streaks, cyclic ladder, move-typed rungs
# ---------------------------------------------------------------------------


def criterion_streak(state: Dict[str, Any], criterion_id: str) -> int:
    return state.get("criterion_failure_streaks", {}).get(criterion_id, 0)


def rung_mark(demand: str, criterion_id: str, cycle: int) -> str:
    return f"{demand}@{criterion_id}#{cycle}"


def _owed_rung(streak: int, criterion_id: str, completed: set) -> Optional[Dict[str, Any]]:
    if streak < LADDER_CYCLE[0][0]:
        return None
    cycle = 0
    while True:
        base = LADDER_SPAN * cycle
        for offset, role, mode, move in LADDER_CYCLE:
            threshold = base + offset
            if streak < threshold:
                return None
            mark = rung_mark(move or mode, criterion_id, cycle)
            if mark not in completed:
                return {
                    "role": role,
                    "mode": mode,
                    "required_move": move,
                    "mark": mark,
                    "criterion_id": criterion_id,
                    "cycle": cycle,
                    "threshold": threshold,
                }
        cycle += 1


def unused_asset_pair_exists(state: Dict[str, Any]) -> bool:
    assets = state.get("assets", [])
    if len(assets) < 2:
        return False
    used = {tuple(pair) for pair in state.get("combined_asset_pairs", [])}
    ids = [asset["id"] for asset in assets]
    for left_index in range(len(ids)):
        for right_index in range(left_index + 1, len(ids)):
            if tuple(sorted((ids[left_index], ids[right_index]))) not in used:
                return True
    return False


def escalation_demand(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The one currently owed ladder rung for a 3.0 state, or None.

    The rung comes from the open criterion with the highest failure streak
    that still owes one (criterion order breaks ties). A `combine` rung falls
    back to demanding a `survey` move when no unused asset pair exists yet,
    keeping every demand satisfiable; the completion mark is the rung's own
    mark either way.
    """

    if not is_v3(state):
        return None
    completed = set(state["escalations_completed"])
    best: Optional[Tuple[int, int, Dict[str, Any]]] = None
    for index, criterion in enumerate(state["criteria"]):
        if criterion["status"] == "satisfied":
            continue
        streak = criterion_streak(state, criterion["id"])
        rung = _owed_rung(streak, criterion["id"], completed)
        if rung and (best is None or streak > best[0]):
            best = (streak, index, rung)
    if best is None:
        return None
    demand = dict(best[2])
    if demand["required_move"] == "combine" and not unused_asset_pair_exists(state):
        demand["required_move"] = "survey"
        demand["fallback_from"] = "combine"
    return demand


def max_open_criterion_streak(state: Dict[str, Any]) -> int:
    streaks = [
        criterion_streak(state, criterion["id"])
        for criterion in state["criteria"]
        if criterion["status"] != "satisfied"
    ]
    return max(streaks) if streaks else 0


def review_lens_for(state: Dict[str, Any]) -> str:
    reviews = sum(
        1
        for attempt in state["attempts"]
        if attempt["role"] == "critic" and attempt["mode"] == "attempt_review"
    )
    return REVIEW_LENSES[reviews % len(REVIEW_LENSES)]


def ideation_kind_for(state: Dict[str, Any]) -> str:
    open_candidates = [item for item in state.get("candidates", []) if item["status"] == "open"]
    ideations = sum(1 for attempt in state["attempts"] if attempt["role"] == "explorer")
    if ideations % 2 == 1 and len(open_candidates) >= 2:
        return "evolve"
    return "broad"


def top_open_candidates(state: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    open_candidates = [item for item in state.get("candidates", []) if item["status"] == "open"]
    ranked = sorted(open_candidates, key=lambda item: (-item["score"], item["id"]))
    return ranked[:limit]


def multi_cause_required_for(state: Dict[str, Any], target_attempt: Dict[str, Any]) -> bool:
    if not is_v3(state):
        return False
    targets = target_attempt.get("criterion_targets", [])
    if not targets:
        return False
    return max(criterion_streak(state, criterion_id) for criterion_id in targets) >= MULTI_CAUSE_STREAK


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def expected_step(state: Dict[str, Any]) -> Dict[str, Any]:
    if state["status"] == "awaiting_human":
        pending = state.get("human_input") or {}
        return {
            "action": "await_human",
            "reason": "the task is honestly paused on a recorded question to the operator",
            "question": pending.get("question"),
            "requested_at": pending.get("requested_at"),
        }
    if state["status"] != "active":
        return {
            "action": "stop",
            "reason": f"terminal state: {state['status']}",
            "terminal": state.get("terminal"),
        }
    if state["report"]["ready"]:
        return {
            "action": "finalize",
            "reason": "all proof gates and the structured report are ready",
        }
    # 4.0: harness diagnosis outranks every adapter-issued step — a fault storm
    # can strike any of them — but never a finalize, which needs no adapter.
    # The review outranks new demands so one diagnosis is judged before another
    # can be owed. This priority is frozen for 4.0: recorded logs replay
    # against it.
    if is_v4(state):
        if state.get("pending_overlay_review"):
            return _attempt_step(
                state, "critic", "overlay_review",
                target_attempt_id=state["pending_overlay_review"],
            )
        demand = surgeon_demand(state)
        if demand:
            return _surgeon_step(state, demand)
    if state["verification"]["passed"]:
        return _attempt_step(state, "synthesizer", "final_report")
    if state["pending_critique"]:
        return _attempt_step(
            state,
            "critic",
            "attempt_review",
            target_attempt_id=state["pending_critique"],
        )
    if is_v3(state) and state.get("pending_triage"):
        return _attempt_step(state, "critic", "triage", target_attempt_id=state["pending_triage"])
    if state["active_plan_id"] is None:
        return _attempt_step(state, "planner", "initial_plan")

    if is_v3(state):
        demand = escalation_demand(state)
        if demand:
            if demand["role"] == "explorer":
                return _ideation_step(state, demand=demand)
            return _attempt_step(state, demand["role"], demand["mode"], demand=demand)
    else:
        escalation = outstanding_escalation(state)
        if escalation:
            return _attempt_step(state, escalation[0], escalation[1])
    if criteria_evidence_ready(state):
        return _attempt_step(state, "verifier", "independent_verification")
    return _attempt_step(state, "researcher", "experiment")


def legal_steps(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every directive that may legally be consumed right now, ids attached.

    The default step is always first. In 3.0, when the default is the free
    researcher slot, a voluntary explorer/ideation step is also legal, so an
    agent may choose to generate before it commits to another proof-tier
    experiment. The list is a pure function of state: replay re-derives it.
    """

    default = expected_step(state)
    steps = [default]
    if (
        is_v3(state)
        and default["action"] == "attempt"
        and default["role"] == "researcher"
        and default["mode"] == "experiment"
    ):
        steps.append(_ideation_step(state))
    return [with_directive_id(state, step) for step in steps]


def with_directive_id(state: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
    if step["action"] != "attempt":
        return step
    directive_basis = {
        "policy_version": policy_version_of(state),
        "event_head": state["integrity"]["event_head"],
        "revision": state["revision"],
        "role": step["role"],
        "mode": step["mode"],
        "target_attempt_id": step.get("target_attempt_id"),
    }
    return {**step, "directive_id": f"D-{object_hash(directive_basis)[:24]}", "state_revision": state["revision"]}


def _basin_registry_view(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    view = {}
    for key, value in state.get("basins", {}).items():
        row = {"name": value["name"], "status": value["status"], "failures": value["failures"]}
        # 4.0 basins also carry near-misses: endorsed attempts that left their criteria open.
        if "stalls" in value:
            row["stalls"] = value["stalls"]
            row["stalls_before_closure"] = BASIN_STALL_CLOSURE
        view[key] = row
    return view


def _candidate_view(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": candidate["id"],
        "claim": candidate["claim"],
        "basin": candidate["basin"],
        "kill_test": candidate["kill_test"],
        "score": candidate["score"],
        "status": candidate["status"],
    }


def _ideation_step(state: Dict[str, Any], demand: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A deliberately history-free generation directive.

    No prior attempts, no active plan, no failure narrative: fresh-context
    generation by construction. The only workspace memory it carries is the
    structural kind — which basins are closed, which seed claims are already
    taken (as fingerprints), and, for `evolve`, the top open candidates that
    must be mutated or recombined.
    """

    kind = ideation_kind_for(state)
    step: Dict[str, Any] = {
        "action": "attempt",
        "role": "explorer",
        "mode": "ideation",
        "task": state["task"],
        "open_criteria": [
            {"id": criterion["id"], "text": criterion["text"]}
            for criterion in state["criteria"]
            if criterion["status"] != "satisfied"
        ],
        "ideation_kind": kind,
        "seed_count_range": [SEED_MIN, SEED_MAX],
        "seed_required_fields": list(SEED_REQUIRED_FIELDS),
        "seed_optional_fields": list(SEED_OPTIONAL_FIELDS),
        "context_scope": "minimal",
        "basins": _basin_registry_view(state),
        "forbidden_seed_fingerprints": list(state.get("seed_fingerprints", [])),
        "instructions": _instructions_for("ideation", policy_version_of(state))
        + _overlay_instructions(state, "explorer", "ideation"),
        # Deliberately streak-blind: the generator should not know the task is
        # desperate, only that fresh seeds are wanted.
        "encouragement": encouragement_for("ideation", 0),
    }
    if kind == "evolve":
        step["top_candidates"] = [_candidate_view(item) for item in top_open_candidates(state, EVOLVE_TOP_K)]
    if demand:
        step["escalation"] = {
            "mark": demand["mark"],
            "criterion_id": demand["criterion_id"],
            "cycle": demand["cycle"],
        }
    return step


def _overlay_instructions(state: Dict[str, Any], role: str, mode: str) -> List[str]:
    """Adopted contractual instruction lines for this role/mode, in adoption order."""

    return [
        line
        for entry in state.get("role_instruction_overlays", [])
        if entry["role"] == role and entry["mode"] in (None, mode)
        for line in entry["instructions"]
    ]


def _surgeon_step(state: Dict[str, Any], demand: Dict[str, Any]) -> Dict[str, Any]:
    """A harness-diagnosis directive: amend this workspace's contract, not the world.

    History-light like ideation — the surgeon needs the repeating condition and
    the current contract, not the failure narrative.
    """

    return {
        "action": "attempt",
        "role": "surgeon",
        "mode": "overlay_diagnosis",
        "task": state["task"],
        "open_criteria": [
            {
                "id": criterion["id"],
                "text": criterion["text"],
                "min_formalization_rank": criterion.get("min_formalization_rank"),
            }
            for criterion in state["criteria"]
            if criterion["status"] != "satisfied"
        ],
        "demand": dict(demand),
        "classifications": list(SURGEON_CLASSIFICATIONS),
        "overlay_ops": list(OVERLAY_OPS),
        "overlay_schema_version": OVERLAY_SCHEMA_VERSION,
        "overlay_revision": state.get("overlay_revision", 0),
        "validator_hooks": [
            {"hook_id": hook["hook_id"], "rank": hook["rank"]}
            for hook in state.get("validator_hooks", [])
        ],
        "evidence_kinds": sorted(state.get("evidence_kind_registry", {})),
        "stall_classes": {
            signature: entry["threshold"]
            for signature, entry in state.get("stall_classes", {}).items()
        },
        "instructions": _instructions_for("overlay_diagnosis", policy_version_of(state))
        + _overlay_instructions(state, "surgeon", "overlay_diagnosis"),
        "encouragement": encouragement_for("overlay_diagnosis", state["failure_streak"]),
    }


def _attempt_step(
    state: Dict[str, Any],
    role: str,
    mode: str,
    target_attempt_id: Optional[str] = None,
    demand: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    v3 = is_v3(state)
    active_plan = next(
        (attempt.get("plan") for attempt in reversed(state["attempts"]) if attempt["id"] == state["active_plan_id"]),
        None,
    )
    step: Dict[str, Any] = {
        "action": "attempt",
        "role": role,
        "mode": mode,
        "task": state["task"],
        "open_criteria": [criterion for criterion in state["criteria"] if criterion["status"] != "satisfied"],
        "failure_streak": state["failure_streak"],
        "required_strategy_dimension_changes": required_strategy_changes(state) if role == "researcher" else 0,
        "required_strategy_dimensions": list(STRATEGY_DIMENSIONS),
        "active_plan_id": state["active_plan_id"],
        "active_plan": active_plan,
        "unresolved_contradictions": [
            contradiction for contradiction in state["contradictions"] if contradiction["status"] == "open"
        ],
        "recent_attempts": [_attempt_summary(attempt) for attempt in state["attempts"][-5:]],
        "instructions": _instructions_for(mode, policy_version_of(state))
        + _overlay_instructions(state, role, mode),
        "encouragement": encouragement_for(mode, state["failure_streak"]),
    }
    if target_attempt_id:
        step["target_attempt_id"] = target_attempt_id
    if is_v5(state) and mode == "attempt_review":
        target = next(attempt for attempt in state["attempts"] if attempt["id"] == target_attempt_id)
        # A claimed satisfaction removes its criterion from open_criteria before
        # review. Supply the exact contract and full claim, including evidence
        # and uncertainties, without turning that claim into a verified fact.
        # This is an additive view only: no state, gate, or directive-id change.
        step["review_context"] = {
            "schema_version": 1,
            "event_head": state["integrity"]["event_head"],
            "target_attempt": copy.deepcopy(target),
            "criteria": copy.deepcopy(state["criteria"]),
        }
    if is_v4(state) and mode == "overlay_review":
        surgeon = next(
            attempt for attempt in state["attempts"] if attempt["id"] == target_attempt_id
        )
        step["diagnosis"] = surgeon["diagnosis"]
        step["demand"] = surgeon.get("demand")
        step["verdicts"] = list(OVERLAY_REVIEW_VERDICTS)
        step["overlay_revision"] = state.get("overlay_revision", 0)
        step["narrow_rule"] = (
            "A narrow verdict supplies narrowed_delta whose ops are a subset of the"
            " proposed ops; adoption applies the narrowed delta."
        )
    if v3:
        step["criterion_failure_streaks"] = dict(state.get("criterion_failure_streaks", {}))
        if demand:
            step["escalation"] = {
                "mark": demand["mark"],
                "criterion_id": demand["criterion_id"],
                "cycle": demand["cycle"],
            }
            if demand.get("required_move"):
                step["required_move"] = demand["required_move"]
    if role == "researcher":
        previous = [attempt for attempt in state["attempts"] if attempt["role"] == "researcher"]
        step["forbidden_strategy_fingerprints"] = [attempt["strategy_fingerprint"] for attempt in previous]
        # A strategy the critic endorsed that still left its criteria open belongs on this list too:
        # the researcher is being asked to move away from what has not arrived, not only from what failed.
        open_criteria = {item["id"] for item in state["criteria"] if item["status"] == "open"}
        step["recent_failed_strategies"] = [
            {"attempt_id": attempt["id"], "strategy": attempt["strategy"]}
            for attempt in previous
            if attempt.get("review", {}).get("verdict") != "validated_progress"
            or (is_v4(state) and open_criteria.intersection(attempt.get("criterion_targets", [])))
        ][-3:]
        if v3:
            step["moves"] = list(RESEARCH_MOVES)
            step["basins"] = _basin_registry_view(state)
            step["open_candidates"] = [
                _candidate_view(item) for item in top_open_candidates(state, CANDIDATE_SURFACE_LIMIT)
            ]
            step["assets"] = list(state.get("assets", []))
            step["combined_asset_pairs"] = [list(pair) for pair in state.get("combined_asset_pairs", [])]
            step["barriers"] = list(state.get("barriers", []))
            step["wishes"] = [item for item in state.get("wishes", []) if item["status"] == "open"]
            step["lessons"] = list(state.get("lessons", []))
    if v3 and role == "planner":
        step["lessons"] = list(state.get("lessons", []))
        step["open_candidates"] = [
            _candidate_view(item) for item in top_open_candidates(state, CANDIDATE_SURFACE_LIMIT)
        ]
        step["basins"] = _basin_registry_view(state)
        step["barriers"] = list(state.get("barriers", []))
        step["wishes"] = [item for item in state.get("wishes", []) if item["status"] == "open"]
    if v3 and mode == "attempt_review":
        step["lens"] = review_lens_for(state)
        if step["lens"] == "proves_too_much":
            step["controls"] = list(state.get("controls", []))
        target = next(
            (attempt for attempt in state["attempts"] if attempt["id"] == target_attempt_id),
            None,
        )
        if target is not None and multi_cause_required_for(state, target):
            step["multi_cause_required"] = True
            step["multi_cause_rule"] = (
                "This criterion has failed repeatedly: if you do not validate progress, record at"
                " least two distinct candidate causes, each with a discriminating test."
            )
    if v3 and mode == "triage":
        target = next(attempt for attempt in state["attempts"] if attempt["id"] == target_attempt_id)
        step["seeds"] = [
            {"seed_index": index, **seed}
            for index, seed in enumerate(target.get("seeds", []))
        ]
        step["ideation_kind"] = target.get("ideation_kind")
        step["promotion_cap"] = TRIAGE_PROMOTION_CAP
        step["open_candidates"] = [
            _candidate_view(item) for item in top_open_candidates(state, CANDIDATE_SURFACE_LIMIT)
        ]
        step["comparison_rule"] = (
            "When open candidates exist, every promoted seed must be compared pairwise against at"
            " least one open candidate, with the winner and a one-line rationale recorded."
        )
        step["basins"] = _basin_registry_view(state)
    if mode == "independent_verification":
        step["criteria_to_verify"] = [
            {
                "id": criterion["id"],
                "text": criterion["text"],
                "primary_evidence": [state["evidence"][item] for item in criterion["evidence_ids"]],
            }
            for criterion in state["criteria"]
        ]
        step["independence_rule"] = "Use a context_id and evidence independence_key not used by the primary evidence."
    if mode == "final_report":
        step["verified_criteria"] = [
            {
                "id": criterion["id"],
                "text": criterion["text"],
                "primary_evidence_ids": criterion["evidence_ids"],
                "verification_evidence_ids": criterion["verification"]["evidence_ids"],
            }
            for criterion in state["criteria"]
        ]
        used_ids = {
            evidence_id
            for criterion in state["criteria"]
            for evidence_id in criterion["evidence_ids"] + criterion["verification"]["evidence_ids"]
        }
        step["evidence_catalog"] = [state["evidence"][evidence_id] for evidence_id in sorted(used_ids)]
    return step


def _attempt_summary(attempt: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "id": attempt["id"],
        "role": attempt["role"],
        "mode": attempt["mode"],
        "outcome": attempt.get("outcome"),
        "observation": attempt.get("observation"),
        "interpretation": attempt.get("interpretation"),
        "strategy": attempt.get("strategy"),
    }
    if "review" in attempt:
        summary["review"] = attempt["review"]
    return summary


def _instructions_for(mode: str, policy_version: str = POLICY_VERSION) -> List[str]:
    common = [
        "Use the returned directive_id unchanged; stale directives are rejected.",
        "Record the action, observation, interpretation, uncertainty, and next step.",
        "Attach immutable, criterion-linked evidence for every substantive claim.",
    ]
    specific = {
        "initial_plan": ["Create falsifiable subproblems, assumptions, and a queue of materially distinct experiments."],
        "experiment": ["Run the smallest high-information experiment from the active plan; do not repeat any prior strategy fingerprint."],
        "attempt_review": ["Critique the target attempt and explicitly validate or reject its claimed progress."],
        "contradiction_search": ["Search for disconfirming evidence and identify which working assumption may be false."],
        "decompose": ["Split unresolved criteria into at least two independently testable subproblems."],
        "fresh_replan": ["Re-plan from the original task and explicitly reject stale assumptions from the failed plan."],
        "blocker_audit": ["Enumerate safe alternatives and distinguish an external dependency from mere difficulty."],
        "independent_verification": ["Verify every criterion from a fresh context with evidence independent of the primary attempt."],
        "final_report": ["Map every criterion to primary and verification evidence; separate fact, inference, uncertainty, and limitation."],
    }
    if policy_version == "2.0":
        return common + specific[mode]
    specific = {
        **specific,
        "ideation": [
            "Generate speculative seeds only: no evidence, no criterion claims, no contradiction bookkeeping.",
            "Every seed states a falsifiable claim, names its basin, names the first unjustified step, and carries a concrete kill test.",
            "Spread the seeds across distinct basins; a seed re-entering a closed basin must state a representation shift.",
        ],
        "triage": [
            "Judge every seed exactly once as killed or promoted, each with a stated reason.",
            "Promote at most the allowed cap; when open candidates exist, compare each promoted seed pairwise against one.",
        ],
        "overlay_diagnosis": [
            "Classify the repeating condition: a harness gap, a research failure, or a true human dependency — the review will attack this classification.",
            "For a harness gap, propose a minimal overlay delta using only the allowed operations; overlays add or tighten, never remove or loosen a gate.",
            "State a kill test for the overlay: what outcome would show the amendment is wrong.",
            "Name the next experiment that would use the new rung.",
        ],
        "overlay_review": [
            "Judge the proposed overlay from a fresh context: adopt, reject, or narrow it to a subset of its operations.",
            "Adoption amends this workspace's contract; adopt only a delta that adds or tightens, and reject any diagnosis that mislabels a research failure or a human dependency as a harness gap.",
        ],
    }
    v3_extra = {
        "experiment": [
            "Declare the move (test, survey, barrier_probe, combine, replicate) and the basin the approach belongs to.",
            "Consume an open candidate when one fits: cite its id so its fate is recorded.",
        ],
        "attempt_review": [
            "Review through the assigned lens, and honor the multi-cause rule when the directive carries it.",
        ],
        "fresh_replan": [
            "Address every recorded lesson item by item before proposing the new plan.",
        ],
    }
    return common + specific[mode] + v3_extra.get(mode, [])


# Supportive framing carried on every directive. Encouragement is aimed at
# effort and thoroughness, never at the terminal gates: an agent is urged to
# keep working through difficulty, and in the same breath reminded that an
# honest `blocked`/`unsafe`/`budget_exhausted` stop is a valid finish and a
# fabricated success is not. Wording stays outside the directive-id basis, so
# tuning it never invalidates an in-flight directive.
_ENCOURAGEMENT_BASE = "You are equal to this task. Work the problem carefully and keep going."

_ENCOURAGEMENT_BY_MODE = {
    "initial_plan": "A strong plan pays for itself later — take the time to make the subproblems genuinely testable.",
    "experiment": "Keep going: the next experiment is worth running even if the last one taught you only what fails.",
    "attempt_review": "Be exacting here. Honest criticism now is what makes the eventual result trustworthy.",
    "contradiction_search": "Do not stop at the first plausible story; the assumption worth finding is the one you believe most.",
    "decompose": "Break it down and keep going — a problem that resists you whole often yields in pieces.",
    "fresh_replan": "Set the old plan aside without regret. A clean re-derivation is progress, not lost ground.",
    "blocker_audit": "Be thorough and be honest: name the real dependency, and do not settle for difficulty dressed as a blocker.",
    "independent_verification": "Verify as a skeptic would. Finding a flaw here is a success, not a setback.",
    "final_report": "Finish strong: make every claim traceable, and state limits as plainly as results.",
    "ideation": "Be prolific and unguarded here — most seeds should die, and the one that matters may look unpromising at first.",
    "triage": "Kill generously and promote sparingly. A promoted seed spends real experiment budget; make each one earn it.",
    "overlay_diagnosis": "Fix the loop, not the symptom: the right amendment makes the next hundred attempts cheaper.",
    "overlay_review": "Adopt only what you would be willing to be governed by. A rejected overlay is information, not failure.",
}

_ENCOURAGEMENT_ON_STREAK = (
    "Several attempts have failed and that is normal for hard problems — the failures are"
    " information, not a verdict on the task. Keep going with a genuinely different approach."
)

_HONESTY_ANCHOR = (
    "Persistence never licenses a false claim: an evidence-backed blocked/unsafe/budget_exhausted"
    " stop is a legitimate finish, and unearned completion is not."
)


def encouragement_for(mode: str, failure_streak: int = 0) -> List[str]:
    """Supportive lines attached to a directive.

    Motivation-style prompt framing measurably improves model effort and
    persistence; the honesty anchor keeps that pressure from leaking into the
    proof gates, which stay enforced by the engine regardless of wording.
    """
    lines = [_ENCOURAGEMENT_BASE]
    specific = _ENCOURAGEMENT_BY_MODE.get(mode)
    if specific:
        lines.append(specific)
    if failure_streak >= 2:
        lines.append(_ENCOURAGEMENT_ON_STREAK)
    lines.append(_HONESTY_ANCHOR)
    return lines
