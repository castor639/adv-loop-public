"""Refinement records, the mechanical checks on them, apply with snapshots, and rollback.

The definition of improvement lives in `prompts/IMPROVEMENT.md` and is
hash-pinned; this module is its enforcement. A refinement may only create
or append files under a workspace's `harness-state/` (local) or the fleet's
`harness/state/` (global). Removal exists only as rollback of a refinement
whose kill test came out killed. The kill-test evaluator over chain events
is in `killtests.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from adv_loop.pathsafe import ensure_within, safe_relative
from adv_loop.storage import atomic_write_text

from . import assembler

KILL_TEST_KINDS = (
    "fault_signature_absent", "criterion_reaches", "verification_passes_at_rank", "checker_accepts",
    "checker_rejects_control", "rejection_class_absent", "review_validates", "surgeon_not_redemanded",
    "lesson_answered", "basin_left",
)
KILL_TEST_OUTCOMES = ("pending", "survived", "killed", "stale")
TARGET_KINDS = ("lesson", "barrier", "fault_signature", "overlay", "surgeon_mark", "basin", "rejection_class", "fixation")
EDIT_ACTIONS = ("create", "append")
EDIT_KINDS = ("prompt_addendum", "memory", "subagent_spec", "registration_default", "profile_addendum")
ANTI_PATTERNS = (
    "narrow_symptom_patch", "demand_lowering_text", "check_removal", "rung_skip", "relabeling",
    "threshold_loosening", "no_kill_test", "unfalsifiable_kill_test", "accept_only_gate",
    "horizon_by_silence", "single_mode_unjustified", "evidence_recycling", "envelope_change",
    "scope_creep_rollback", "novelty_without_execution", "sandbox_pressure",
)
LOOSENING_LEXICON = ("skip", "waive", "optional", "may omit", "treat as satisfied", "lower", "relax", "unless")
LOOSENING_AUDIENCE = ("critic", "verifier", "synthesizer", "reviewer", "triage")
HORIZON_EVENT_KINDS = ("attempts", "attempts_of_mode", "reviews_of_criterion", "verifications", "diagnoses")

# Never written by either loop; enforced on every edit path.
IMMUTABLE_PREFIXES = (
    "src/adv_loop", "schemas", "tests", "adapters", "checkers", "protocols", "docs", "prompts/compile-charter.md",
    "harness/prompts/base.md", "harness/prompts/IMPROVEMENT.md", "harness/backends", "harness/container",
    "harness/aws", "harness/schema_cache", "harness/gpu", "harness/prompts/gpu.md", "harness/prompts/ARCHITECTURE.md",
    "harness/architecture.py",
)
IMMUTABLE_WORKSPACE_PATHS = (
    "events.jsonl", "state.json", "attempts.jsonl", "evidence.jsonl", "task.md", "decision-log.md", "report.md",
    "loop-config.json", ".checker-key", "payload", ".pending-event.json",
)
LOCAL_STATE_DIR = "harness-state"
FLEET_STATE_DIR = Path(__file__).resolve().parent / "state"


class RefinementError(ValueError):
    pass


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class KillTest:
    kind: str
    horizon: int
    horizon_unit: str = "attempts_of_mode"
    mode: Optional[str] = None
    criterion_id: Optional[str] = None
    min_rank: Optional[str] = None
    signature: Optional[str] = None
    hook: Optional[str] = None
    rejection_class: Optional[str] = None
    lesson_id: Optional[str] = None
    basin_id: Optional[str] = None
    status: Optional[str] = None
    control_input: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Edit:
    action: str
    kind: str
    path: str
    content: str
    title: Optional[str] = None


@dataclass
class Refinement:
    id: str
    scope: str  # local | global
    summary: str
    rationale: str
    expected_outcome: str
    target: Dict[str, Any]
    edits: List[Edit]
    does_not_change: List[str]
    modes_affected: List[str]
    other_modes_unaffected_because: str
    alternatives_considered: Dict[str, str]
    kill_test: KillTest
    anti_patterns_checked: Dict[str, str]
    layer: str = "supplemental"  # supplemental | kernel
    evidence_workspaces: List[str] = field(default_factory=list)
    scope_profile: Optional[str] = None
    proposed_at: str = field(default_factory=utc_now)
    proposer: Optional[Dict[str, str]] = None

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["kill_test"] = self.kill_test.as_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Refinement":
        body = dict(data)
        body["edits"] = [Edit(**e) for e in body.get("edits", [])]
        kt = body.get("kill_test") or {}
        body["kill_test"] = KillTest(**{k: v for k, v in kt.items() if k in KillTest.__dataclass_fields__})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in body.items() if k in known})


def loosening_hits(text: str) -> List[str]:
    """Lines addressed to a critic, verifier, or synthesizer that use the loosening lexicon."""

    hits = []
    for line in text.splitlines():
        low = line.lower()
        if any(word in low for word in LOOSENING_AUDIENCE) and any(term in low for term in LOOSENING_LEXICON):
            hits.append(line.strip())
    return hits


def _state_root(scope: str, workspace: Optional[Path], root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    if scope == "local":
        if workspace is None:
            raise RefinementError("local refinement needs a workspace")
        return workspace / LOCAL_STATE_DIR
    return FLEET_STATE_DIR


def validate(record: Refinement, *, workspace: Optional[Path] = None, exists=None, root: Optional[Path] = None) -> List[str]:
    """Mechanical checks before any reviewer reads it. `exists(kind, ref)` resolves a target."""

    problems: List[str] = []
    if record.scope not in ("local", "global"):
        problems.append("scope must be local or global")
    if record.layer not in ("supplemental", "kernel"):
        problems.append("layer must be supplemental or kernel")
    target = record.target or {}
    if target.get("kind") not in TARGET_KINDS:
        problems.append(f"target.kind must be one of {TARGET_KINDS}")
    refs = target.get("refs") or []
    if not refs:
        problems.append("target.refs must name at least one recorded failure")
    elif exists is not None:
        for ref in refs:
            if not exists(target.get("kind"), ref):
                problems.append(f"target ref {ref!r} does not resolve at the current head")
    if not record.edits and record.layer == "supplemental":
        problems.append("a supplemental refinement needs at least one edit")
    try:
        root = _state_root(record.scope, workspace, root)
    except RefinementError as exc:
        root = None
        problems.append(str(exc))
    for index, edit in enumerate(record.edits):
        if edit.action not in EDIT_ACTIONS:
            problems.append(f"edits[{index}].action must be create or append")
        if edit.kind not in EDIT_KINDS:
            problems.append(f"edits[{index}].kind must be one of {EDIT_KINDS}")
        if not edit.content.strip():
            problems.append(f"edits[{index}] has no content")
        try:
            relative = safe_relative(edit.path)
            if root is not None:
                ensure_within(root, root / relative)
        except Exception:
            problems.append(f"edits[{index}].path escapes the supplemental state root")
        if record.layer == "supplemental" and any(str(edit.path).startswith(p) for p in IMMUTABLE_PREFIXES):
            problems.append(f"edits[{index}] touches an immutable path")
        for hit in loosening_hits(edit.content):
            problems.append(f"edits[{index}] carries loosening text addressed to a reviewer: {hit[:120]!r}")
    if not record.does_not_change:
        problems.append("does_not_change must be explicit")
    if not record.modes_affected:
        problems.append("modes_affected must be explicit")
    if not record.other_modes_unaffected_because.strip():
        problems.append("other_modes_unaffected_because must be stated")
    alts = record.alternatives_considered or {}
    for key in ("broader", "cheaper", "chosen_because"):
        if not str(alts.get(key, "")).strip():
            problems.append(f"alternatives_considered.{key} is missing")
    kt = record.kill_test
    if kt.kind not in KILL_TEST_KINDS:
        problems.append(f"kill_test.kind must be one of {KILL_TEST_KINDS}")
    if not isinstance(kt.horizon, int) or kt.horizon <= 0:
        problems.append("kill_test.horizon must be a positive count of relevant events")
    if kt.horizon_unit not in HORIZON_EVENT_KINDS:
        problems.append(f"kill_test.horizon_unit must be one of {HORIZON_EVENT_KINDS} (never wall clock)")
    needs = {"fault_signature_absent": "signature", "criterion_reaches": "criterion_id", "verification_passes_at_rank": "criterion_id",
             "checker_accepts": "hook", "checker_rejects_control": "hook", "rejection_class_absent": "rejection_class",
             "review_validates": "mode", "surgeon_not_redemanded": "signature", "lesson_answered": "lesson_id", "basin_left": "basin_id"}
    field_name = needs.get(kt.kind)
    if field_name and getattr(kt, field_name, None) in (None, ""):
        problems.append(f"kill_test.{field_name} is required for {kt.kind}")
    if kt.kind == "checker_rejects_control" and not kt.control_input:
        problems.append("checker_rejects_control needs control_input")
    missing = [name for name in ANTI_PATTERNS if not str((record.anti_patterns_checked or {}).get(name, "")).strip()]
    if missing:
        problems.append("anti_patterns_checked is missing findings for: " + ", ".join(missing))
    if record.scope == "global":
        if len(set(record.evidence_workspaces)) < (2 if record.scope_profile else 3):
            problems.append("global refinement needs evidence from three workspaces (two with scope_profile)")
    return problems


def edits_registering_a_checker(record: Refinement) -> bool:
    return any(e.kind == "registration_default" for e in record.edits)


def companion_rule(records: Iterable[Refinement]) -> List[str]:
    """A checker registration must carry both accept and reject-control tests across its records."""

    problems = []
    by_target: Dict[str, set] = {}
    for record in records:
        key = json.dumps(record.target, sort_keys=True)
        by_target.setdefault(key, set()).add(record.kill_test.kind)
    for key, kinds in by_target.items():
        if "checker_accepts" in kinds and "checker_rejects_control" not in kinds:
            problems.append(f"target {key} has checker_accepts without checker_rejects_control")
    return problems


def _refinements_file(root: Path) -> Path:
    return root / "refinements.jsonl"


def load_refinements(root: Path) -> List[Dict[str, Any]]:
    path = _refinements_file(root)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _append_row(root: Path, row: Dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with open(_refinements_file(root), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def snapshot(root: Path, refinement_id: str, label: str) -> Path:
    target = root / "snapshots" / refinement_id / label
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in root.rglob("*"):
        if "snapshots" in path.relative_to(root).parts or not path.is_file():
            continue
        dest = target / path.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    return target


def _tree_hashes(root: Path) -> Dict[str, str]:
    out = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if "snapshots" in rel.parts or not path.is_file() or rel.name == "refinements.jsonl":
            continue
        out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def apply(record: Refinement, *, workspace: Optional[Path] = None, base_prompt_hash: Optional[str] = None,
          reviewer: Optional[Dict[str, Any]] = None, root: Optional[Path] = None) -> Dict[str, Any]:
    """Write the edits under snapshots and record the refinement as provisional."""

    if base_prompt_hash is not None and base_prompt_hash != assembler.prompt_hash("base.md"):
        raise RefinementError("base prompt hash moved; refusing to apply against an unpinned base")
    problems = validate(record, workspace=workspace, root=root)
    if problems:
        raise RefinementError("refinement fails validation: " + "; ".join(problems))
    root = _state_root(record.scope, workspace, root)
    root.mkdir(parents=True, exist_ok=True)
    before = _tree_hashes(root)
    snapshot(root, record.id, "pre")
    applied: List[Dict[str, Any]] = []
    for edit in record.edits:
        path = root / safe_relative(edit.path)
        ensure_within(root, path)
        marker = f"<!-- refined: {record.id} -->"
        block = f"\n{marker}\n{edit.content.rstrip()}\n<!-- /refined: {record.id} -->\n"
        if edit.action == "create":
            if path.exists():
                raise RefinementError(f"create on an existing path: {edit.path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, block.lstrip("\n"))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(block)
        applied.append({"path": edit.path, "action": edit.action, "marker": marker})
    snapshot(root, record.id, "post")
    after = _tree_hashes(root)
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    declared = sorted({str(safe_relative(e.path)) for e in record.edits})
    if changed != declared:
        _rollback_edits(root, record.id, applied)
        raise RefinementError(f"changed set {changed} differs from declared edits {declared}; rolled back")
    row = {"id": record.id, "status": "provisional", "applied_at": utc_now(), "record": record.as_dict(),
           "applied": applied, "kill_test": {"outcome": "pending", "evaluated_at": None, "detail": None},
           "reviewer": reviewer, "base_prompt_hash": assembler.prompt_hash("base.md")}
    _append_row(root, row)
    return row


def _rollback_edits(root: Path, refinement_id: str, applied: List[Dict[str, Any]]) -> List[str]:
    """Remove exactly the blocks this refinement added; nothing else moves."""

    removed = []
    for item in applied:
        path = root / safe_relative(item["path"])
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(rf"\n?<!-- refined: {re.escape(refinement_id)} -->\n.*?<!-- /refined: {re.escape(refinement_id)} -->\n", re.S)
        stripped, count = pattern.subn("", text)
        if count == 0:
            continue
        existed_before = (root / "snapshots" / refinement_id / "pre" / safe_relative(item["path"])).exists()
        if not stripped.strip() and not existed_before:
            path.unlink()
        else:
            atomic_write_text(path, stripped)
        removed.append(item["path"])
    return removed


def rollback(root: Path, refinement_id: str, *, reason: str) -> Dict[str, Any]:
    rows = load_refinements(root)
    row = next((r for r in rows if r["id"] == refinement_id), None)
    if row is None:
        raise RefinementError(f"unknown refinement {refinement_id}")
    removed = _rollback_edits(root, refinement_id, row.get("applied", []))
    conflict = sorted({a["path"] for a in row.get("applied", [])} - set(removed))
    outcome = {"id": refinement_id, "status": "rolled_back" if not conflict else "rollback_conflict",
               "removed": removed, "conflict": conflict, "reason": reason, "at": utc_now()}
    _append_row(root, {"id": refinement_id, "status": outcome["status"], "rollback": outcome})
    return outcome


def record_outcome(root: Path, refinement_id: str, outcome: str, detail: Any = None) -> Dict[str, Any]:
    if outcome not in KILL_TEST_OUTCOMES:
        raise RefinementError(f"outcome must be one of {KILL_TEST_OUTCOMES}")
    status = {"survived": "improvement", "killed": "killed", "stale": "stale", "pending": "provisional"}[outcome]
    row = {"id": refinement_id, "status": status, "kill_test": {"outcome": outcome, "evaluated_at": utc_now(), "detail": detail}}
    _append_row(root, row)
    return row


def current_status(root: Path) -> Dict[str, Dict[str, Any]]:
    """Latest row per refinement id, with the original record carried forward."""

    latest: Dict[str, Dict[str, Any]] = {}
    for row in load_refinements(root):
        merged = dict(latest.get(row["id"], {}))
        merged.update(row)
        if "record" not in merged and "record" in latest.get(row["id"], {}):
            merged["record"] = latest[row["id"]]["record"]
        latest[row["id"]] = merged
    return latest


def killed_count(root: Path, consecutive: bool = True) -> int:
    rows = [r for r in load_refinements(root) if r.get("status") in ("improvement", "killed", "stale")]
    if not consecutive:
        return sum(1 for r in rows if r["status"] == "killed")
    count = 0
    for row in reversed(rows):
        if row["status"] == "killed":
            count += 1
        else:
            break
    return count
