"""The global self-improvement loop.

Runs between supervisor passes. Three fresh sessions on two models:
proposer, narrowness critic (first, short-circuits on fail), adoption
critic. A supplemental-layer refinement is applied to the fleet state with
one kill-test instance per evidence workspace; it survives only when a
majority survive and none is killed where the baseline discriminated. A
kernel-layer refinement never applies: it becomes a pull-request bundle a
human merges. The loop never writes a chain event, and it hashes every
event log before and after to prove it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import architecture, assembler, budget, improvement, killtests, refine, signal, transcripts
from .backends.base import AssembledPrompt, SessionResult
from .improvement import ANTI_PATTERNS, Refinement

FREEZE_RAILS = ("tests.test_persistence", "tests.test_overlays", "tests.test_neutrality", "tests.test_review_context")
PAUSED_FILE = ".paused"
KILLED_STREAK_TO_PAUSE = 3
DEFAULT_MAX_USD_PER_DAY = 5.0
REPO_ROOT = Path(__file__).resolve().parents[1]
PROPOSALS_DIR = REPO_ROOT / "harness" / "proposals"

STRING = {"type": "string", "minLength": 1}
GLOBAL_REFINEMENT_OBJECT = json.loads(json.dumps(refine.REFINEMENT_OBJECT))
GLOBAL_REFINEMENT_OBJECT["properties"]["evidence_workspaces"] = {"type": "array", "minItems": 1, "items": STRING}
GLOBAL_REFINEMENT_OBJECT["properties"]["patch"] = {"type": "string"}
GLOBAL_REFINEMENT_OBJECT["properties"]["kernel_items"] = {"type": "array", "items": STRING}
GLOBAL_REFINEMENT_OBJECT["required"].append("evidence_workspaces")
PROPOSAL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False,
    "required": ["proposal", "reason", "agenda_findings"],
    "properties": {"proposal": {"oneOf": [{"type": "null"}, GLOBAL_REFINEMENT_OBJECT]}, "reason": STRING,
                   "agenda_findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                  "required": ["item", "supported", "note"],
                                                                  "properties": {"item": STRING, "supported": {"type": "boolean"}, "note": STRING}}}},
}
NARROWNESS_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False,
    "required": ["verdict", "answers", "cited_ids"],
    "properties": {
        "verdict": {"enum": ["pass", "fail"]},
        "answers": {"type": "object", "additionalProperties": False,
                    "required": [f"item_{i}" for i in range(1, 9)],
                    "properties": {f"item_{i}": STRING for i in range(1, 9)}},
        "cited_ids": {"type": "array", "items": {"type": "string"}},
    },
}
REVIEW_SCHEMA = refine.REVIEW_SCHEMA


class GlobalRefineError(RuntimeError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def events_fingerprint(root: Path) -> Dict[str, str]:
    out = {}
    for ws in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        path = ws / "events.jsonl"
        if path.is_file():
            out[ws.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def fleet_spend_today(fleet_state: Path) -> float:
    path = fleet_state / budget.SPEND_FILE
    if not path.is_file():
        return 0.0
    today = utc_now()[:10]
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if str(row.get("at", "")).startswith(today):
            value = row.get("estimated_usd") if row.get("billing") == "api" else row.get("notional_usd")
            total += float(value or 0.0)
    return round(total, 6)


def next_id(fleet_state: Path) -> str:
    return f"GR{len(improvement.current_status(fleet_state)) + 1:04d}"


def _session(runtime, fleet_state: Path, role: str, kind: str, envelope: Dict[str, Any], schema: Dict[str, Any],
             root: Path) -> Tuple[SessionResult, AssembledPrompt, Dict[str, Any]]:
    directive = {"action": "attempt", "directive_id": f"global-refine:{uuid.uuid4().hex[:12]}", "role": role, "mode": "refine"}
    stub = getattr(runtime, "refine_session", None)
    assignment = None if stub else runtime.router_factory({}).resolve(role)
    architecture.ensure(root)
    prompt = assembler.assemble_refine(kind, envelope, schema,
                                       architecture_tier=assignment.spec.architecture_tier if assignment else None)
    if stub:
        result = stub(role, prompt, directive)
        model = result.resolved_model or "stub"
        row = {"estimated_usd": 0.0}
    else:
        from .backends import make_backend
        from .backends.base import WorkspaceHandle
        spec = assignment.spec
        if not runtime.api_guard.allows(spec, assignment.budget.max_budget_usd):
            raise GlobalRefineError("api budget stop; global refine refused")
        session_dir = fleet_state / "sessions"
        handle = WorkspaceHandle(ws_id="fleet", path=root, session_id=uuid.uuid4().hex)
        backend = make_backend(spec, handle, docker=None, overrides=runtime.backend_overrides.get(spec.backend),
                               tool_allow=refine.READ_ONLY_TOOLS)
        result = backend.run(directive, handle, spec, assignment.budget, prompt)
        row = budget.record_spend(fleet_state, spec, result.usage, role=role, mode="global_refine",
                                  directive_id=directive["directive_id"], session_id=handle.session_id,
                                  base_prompt_hash=prompt.manifest["base_prompt_hash"])
        model = spec.model
    transcripts.persist(fleet_state, result, prompt, directive=directive, model=model, submission=result.model_output,
                        rejection=None if result.ok else [result.ended], accepted=None, spend_usd=row.get("estimated_usd"))
    return result, prompt, {"session_id": result.session_id, "model": model, "ended": result.ended, "role": role}


def independence(record: Refinement, sig: Dict[str, Any]) -> List[str]:
    """K distinct workspaces by task id and charter hash, not all one profile unless scoped."""

    problems = []
    rows = {w["workspace"]: w for w in sig["workspaces"]}
    chosen = [rows[n] for n in record.evidence_workspaces if n in rows]
    missing = [n for n in record.evidence_workspaces if n not in rows]
    if missing:
        problems.append(f"evidence workspaces not in the fleet: {missing}")
    need = 2 if record.scope_profile else 3
    if len({w.get("task_id") for w in chosen}) < need:
        problems.append(f"evidence_recycling: need {need} distinct task ids, have {len({w.get('task_id') for w in chosen})}")
    charters = {w.get("charter_hash") for w in chosen}
    if len(charters) < need:
        problems.append(f"evidence_recycling: need {need} distinct charter hashes, have {len(charters)}")
    profiles = {w.get("profile") for w in chosen}
    if len(profiles) == 1 and not record.scope_profile and len(chosen) >= 2:
        problems.append("all evidence workspaces share one profile; set scope_profile or widen the evidence")
    if record.scope_profile and any(w.get("profile") != record.scope_profile for w in chosen):
        problems.append("scope_profile does not match every evidence workspace")
    return problems


def evaluate_pending(root: Path, fleet_state: Path, run_control=None) -> List[Dict[str, Any]]:
    outcomes = []
    for rid, row in improvement.current_status(fleet_state).items():
        if row.get("status") != "provisional" or "record" not in row:
            continue
        kt = row["record"].get("kill_test") or {}
        verdicts = []
        for inst in row.get("instances") or []:
            ws = Path(root) / inst["workspace"]
            if not (ws / "events.jsonl").is_file():
                verdicts.append({**inst, "outcome": "stale", "reason": "workspace gone"})
                continue
            verdict = killtests.evaluate(ws, kt, int(inst.get("adopted_seq", 0)), adopted_at=inst.get("adopted_at"),
                                         run_control=run_control)
            verdicts.append({**inst, **verdict})
        killed_discriminating = [v["workspace"] for v in verdicts if v["outcome"] == "killed" and v.get("baseline_discriminating", True)]
        if not killed_discriminating and (not verdicts or any(v["outcome"] == "pending" for v in verdicts)):
            outcomes.append({"id": rid, "outcome": "pending", "instances": verdicts})
            continue
        survived = sum(1 for v in verdicts if v["outcome"] == "survived")
        if killed_discriminating:
            outcome = "killed"
        elif survived * 2 > len(verdicts):
            outcome = "survived"
        elif all(v["outcome"] == "stale" for v in verdicts):
            outcome = "stale"
        else:
            outcome = "killed"
        improvement.record_outcome(fleet_state, rid, outcome, {"instances": verdicts})
        result = {"id": rid, "outcome": outcome, "instances": verdicts}
        if outcome == "killed":
            result["rollback"] = improvement.rollback(fleet_state, rid, reason="global kill test killed")
        outcomes.append(result)
    return outcomes


def write_pr_bundle(record: Refinement, sig: Dict[str, Any], *, proposals_dir: Path = PROPOSALS_DIR,
                    repo: Path = REPO_ROOT, open_pr: bool = False, runner=subprocess.run) -> Dict[str, Any]:
    """A kernel-layer proposal becomes files a human reviews; nothing is applied to the running tree."""

    target = proposals_dir / record.id
    target.mkdir(parents=True, exist_ok=True)
    (target / "proposal.json").write_text(json.dumps(record.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows = {w["workspace"]: w for w in sig["workspaces"]}
    lines = ["| workspace | task | charter | target evidence |", "|---|---|---|---|"]
    for name in record.evidence_workspaces:
        w = rows.get(name, {})
        lines.append(f"| {name} | {w.get('task_id')} | {str(w.get('charter_hash'))[:12]} | {json.dumps(record.target)} |")
    (target / "evidence-table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (target / "kill-test.json").write_text(json.dumps(record.kill_test.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (target / "does-not-change.md").write_text("".join(f"- {item}\n" for item in record.does_not_change), encoding="utf-8")
    alts = record.alternatives_considered
    (target / "alternatives.md").write_text(
        f"## Broader\n\n{alts.get('broader')}\n\n## Cheaper\n\n{alts.get('cheaper')}\n\n## Chosen because\n\n{alts.get('chosen_because')}\n",
        encoding="utf-8")
    patch = getattr(record, "patch", None) or ""
    (target / "patch.diff").write_text(patch, encoding="utf-8")
    rails = run_freeze_rails(patch, repo=repo, runner=runner)
    (target / "freeze-rails.txt").write_text(rails["output"], encoding="utf-8")
    result = {"bundle": str(target), "freeze_rails_ok": rails["ok"], "patch_applied_cleanly": rails["patch_ok"], "pr": None}
    if open_pr and shutil.which("gh"):
        branch = f"harness/{record.id}-{_slug(record.summary)}"
        result["pr"] = {"branch": branch, "note": "draft PR creation is left to the operator's gh session; no merge path exists here"}
    return result


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "proposal"


def run_freeze_rails(patch: str, *, repo: Path = REPO_ROOT, runner=subprocess.run) -> Dict[str, Any]:
    """Apply the patch to a scratch copy of the kernel tree and run the freeze-rail suites verbatim."""

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "repo"
        scratch.mkdir()
        for name in ("src", "tests", "schemas", "checkers", "adapters", "harness", "pyproject.toml", "profiles", "prompts"):
            source = repo / name
            if source.is_dir():
                shutil.copytree(source, scratch / name, ignore=shutil.ignore_patterns("__pycache__", "state", "proposals"))
            elif source.is_file():
                shutil.copyfile(source, scratch / name)
        patch_ok = True
        output = ""
        if patch.strip():
            (scratch / "proposal.diff").write_text(patch, encoding="utf-8")
            applied = runner(["patch", "-p1", "--forward", "-i", "proposal.diff"], cwd=str(scratch), capture_output=True, text=True)
            patch_ok = applied.returncode == 0
            output += "$ patch -p1 --forward -i proposal.diff\n" + applied.stdout + applied.stderr + "\n"
        env = {**os.environ, "PYTHONPATH": f"{scratch / 'src'}{os.pathsep}{scratch}"}
        done = runner([sys.executable, "-m", "unittest", *FREEZE_RAILS, "-v"], cwd=str(scratch), capture_output=True, text=True, env=env)
        output += f"$ python3 -m unittest {' '.join(FREEZE_RAILS)} -v\n" + done.stdout + done.stderr
        return {"ok": patch_ok and done.returncode == 0, "patch_ok": patch_ok, "output": output}


def run(runtime, *, fleet_state: Optional[Path] = None, dry_run: bool = False, force: bool = False,
        open_pr: bool = False, proposals_dir: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(runtime.root)
    fleet_state = Path(fleet_state or improvement.FLEET_STATE_DIR)
    fleet_state.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {"at": utc_now(), "fleet_state": str(fleet_state)}
    if (fleet_state / PAUSED_FILE).is_file():
        report["skipped"] = "paused (delete harness/state/.paused to resume)"
        return report
    if improvement.killed_count(fleet_state) >= KILLED_STREAK_TO_PAUSE:
        (fleet_state / PAUSED_FILE).write_text(f"{KILLED_STREAK_TO_PAUSE} consecutive killed global refinements at {utc_now()}\n")
        report["skipped"] = "three consecutive killed refinements; paused"
        return report
    if not runtime.api_guard.refine_allowed():
        report["skipped"] = "api budget degraded; global refine disabled"
        return report
    cap = getattr(runtime, "global_refine_max_usd_per_day", DEFAULT_MAX_USD_PER_DAY)
    if fleet_spend_today(fleet_state) >= cap:
        report["skipped"] = f"global refine budget reached ({cap}/day)"
        return report

    before = events_fingerprint(root)
    try:
        report["evaluated"] = evaluate_pending(root, fleet_state)
        sig = signal.build(root, fleet_state)
        report["measures"] = signal.measures(sig, fleet_state)
        if not force and sig["fleet"]["workspace_count"] < 1:
            report["skipped"] = "no workspaces"
            return report
        envelope = {"protocol": "adv-loop-refine/1", "scope": "global", "signal": sig, "measures": report["measures"],
                    "refinement_id": next_id(fleet_state), "supplemental_root": "harness/state",
                    "transcript_dirs": {w["workspace"]: str(root / w["workspace"] / transcripts.TRANSCRIPTS / "sessions") for w in sig["workspaces"]}}
        result, _, info = _session(runtime, fleet_state, "refine_proposer", "proposer", envelope, PROPOSAL_SCHEMA, root)
        report["proposer"] = info
        if not result.ok or not isinstance(result.model_output, dict):
            report["skipped"] = f"proposer session ended {result.ended}"
            return report
        report["agenda_findings"] = result.model_output.get("agenda_findings")
        proposal = result.model_output.get("proposal")
        if proposal is None:
            report["null"] = result.model_output.get("reason")
            return report
        patch = proposal.pop("patch", None)
        kernel_items = proposal.pop("kernel_items", None)
        record = Refinement.from_dict({**proposal, "id": envelope["refinement_id"], "scope": "global",
                                       "proposer": {"model": info["model"], "session_id": info["session_id"]}})
        setattr(record, "patch", patch)
        problems = improvement.validate(record, root=fleet_state)
        problems = [p for p in problems if not (record.layer == "kernel" and "needs at least one edit" in p)]
        problems += independence(record, sig)
        instances = []
        for name in record.evidence_workspaces:
            ws = root / name
            if not (ws / "events.jsonl").is_file():
                continue
            base = killtests.baseline(ws, record.kill_test.as_dict(), killtests.head_seq(ws), adopted_at=utc_now())
            instances.append({"workspace": name, "adopted_seq": killtests.head_seq(ws), "adopted_at": utc_now(),
                              "baseline": base["outcome"], "baseline_discriminating": bool(base.get("discriminating"))})
        if instances and not any(i["baseline_discriminating"] for i in instances):
            problems.append("unfalsifiable_kill_test: no evidence workspace's pre-adoption window comes out killed")
        report.update({"record": record.as_dict(), "validation": problems, "instances": instances, "kernel_items": kernel_items})
        if problems:
            report["rejected_by"] = "mechanical"
            improvement._append_row(fleet_state, {"id": record.id, "status": "rejected", "record": record.as_dict(),
                                                  "problems": problems, "at": utc_now()})
            return report
        if dry_run:
            report["dry_run"] = True
            return report

        review_envelope = {"protocol": "adv-loop-refine-review/1", "scope": "global", "refinement": record.as_dict(),
                           "instances": instances, "signal": sig, "measures": report["measures"]}
        narrow, _, ninfo = _session(runtime, fleet_state, "narrowness_critic", "narrowness_critic", review_envelope, NARROWNESS_SCHEMA, root)
        report["narrowness"] = {**ninfo, "verdict": (narrow.model_output or {}).get("verdict") if narrow.ok else None}
        if not narrow.ok or (narrow.model_output or {}).get("verdict") != "pass":
            report["rejected_by"] = "narrowness"
            report["narrowness_answers"] = (narrow.model_output or {}).get("answers")
            improvement._append_row(fleet_state, {"id": record.id, "status": "rejected", "record": record.as_dict(),
                                                  "narrowness": narrow.model_output, "at": utc_now()})
            return report
        adopt, _, ainfo = _session(runtime, fleet_state, "adoption_critic", "adoption_critic", review_envelope, REVIEW_SCHEMA, root)
        report["adoption"] = ainfo
        stages = {info["session_id"], ninfo["session_id"], ainfo["session_id"]}
        if len(stages) != 3:
            raise GlobalRefineError("two refine stages shared a session context; refusing")
        verdict = adopt.model_output if adopt.ok and isinstance(adopt.model_output, dict) else None
        missing = [n for n in ANTI_PATTERNS if not str((verdict or {}).get("anti_patterns", {}).get(n, "")).strip()]
        if verdict is None or missing or verdict.get("verdict") == "reject":
            report["rejected_by"] = "adoption" if verdict and not missing else f"adoption critic omitted anti-patterns {missing}"
            report["review"] = verdict
            improvement._append_row(fleet_state, {"id": record.id, "status": "rejected", "record": record.as_dict(),
                                                  "review": verdict, "at": utc_now()})
            return report
        if verdict.get("verdict") == "narrow":
            keep = sorted(set(int(i) for i in verdict.get("retained_edits", [])))
            record.edits = [e for i, e in enumerate(record.edits) if i in keep]
            if not record.edits and record.layer == "supplemental":
                report["rejected_by"] = "adoption critic narrowed to nothing"
                return report
        report["review"] = verdict

        if record.layer == "kernel":
            bundle = write_pr_bundle(record, sig, proposals_dir=proposals_dir or PROPOSALS_DIR, open_pr=open_pr)
            improvement._append_row(fleet_state, {"id": record.id, "status": "pr_bundle", "record": record.as_dict(),
                                                  "bundle": bundle, "review": verdict, "at": utc_now()})
            report["pr_bundle"] = bundle
            return report

        row = improvement.apply(record, root=fleet_state, base_prompt_hash=assembler.prompt_hash("base.md"),
                                reviewer={"model": ainfo["model"], "session_id": ainfo["session_id"], "verdict": verdict})
        improvement._append_row(fleet_state, {"id": record.id, "status": "provisional", "instances": instances,
                                              "adopted_at": row["applied_at"]})
        report["applied"] = {"id": record.id, "edits": [e.path for e in record.edits], "instances": len(instances)}
        return report
    finally:
        after = events_fingerprint(root)
        moved = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        report["events_moved"] = moved
        if moved:
            report["aborted"] = "an event log moved during the global pass"
            (fleet_state / PAUSED_FILE).write_text(f"event logs moved during a global pass at {utc_now()}: {moved}\n")


def scheduled(runtime, passes: int, idle: bool) -> bool:
    every = getattr(runtime, "refine_every", 1)
    return getattr(runtime, "global_refine_enabled", True) and (idle or (every and passes % every == 0))
