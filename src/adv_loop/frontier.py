"""Frontier: the fleet proposes its own next problems (supervisor plane).

Completed reports, open wishes, named barriers, and blocked premises across a
root are collected into field-neutral signals and handed to a generator
adapter, which returns new goal briefs. Accepted proposals are written as
drops into an inbox directory, where the fleet's auto-intake turns them into
chartered workspaces — the existing gates handle proposal quality, and the
standing budgets handle proposal appetite. The kernel never generates or
interprets a proposal; without a generator adapter there is no frontier.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .engine import LoopEngine
from .errors import AdapterError, LoopError

FRONTIER_PROTOCOL = "adv-loop-frontier/1"
DEFAULT_MAX_PROPOSALS = 5
PROPOSAL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
BRIEF_CAP = 100_000


def _signal(state: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    signal: Dict[str, Any] = {
        "workspace": str(workspace),
        "task_id": state["task_id"],
        "task": state["task"],
        "status": state["status"],
        "open_wishes": [
            {"statement": wish["statement"], "would_open": wish["would_open"]}
            for wish in state.get("wishes", [])
            if wish["status"] == "open"
        ],
        "barriers": [
            {"name": barrier["name"], "statement": barrier["statement"]}
            for barrier in state.get("barriers", [])
        ],
    }
    terminal = state.get("terminal") or {}
    if state["status"] == "blocked":
        signal["blocked_dependency"] = terminal.get("dependency")
        signal["blocked_premise"] = (terminal.get("retest") or {}).get("premise")
    structured = (state.get("report") or {}).get("structured")
    if state["status"] == "completed" and structured:
        signal["report_summary"] = structured.get("summary")
        signal["next_actions"] = structured.get("next_actions", [])
    return signal


def _validate_proposals(value: Any, max_proposals: int, out: Optional[Path]) -> List[Dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("proposals"), list):
        raise AdapterError("Frontier generator must return {proposals: [...]}")
    proposals = value["proposals"]
    if len(proposals) > max_proposals:
        raise AdapterError("Frontier generator exceeded max_proposals",
                           {"max_proposals": max_proposals, "received": len(proposals)})
    seen = set()
    normalized: List[Dict[str, Any]] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict) or set(proposal) - {"name", "brief", "charter"}:
            raise AdapterError("Frontier proposal has an invalid shape", {"index": index})
        name = proposal.get("name")
        if not isinstance(name, str) or not PROPOSAL_NAME.fullmatch(name) or name in seen:
            raise AdapterError("Frontier proposal name must be a unique kebab-case slug",
                               {"index": index, "name": name})
        seen.add(name)
        brief = proposal.get("brief")
        if not isinstance(brief, str) or not brief.strip() or len(brief) > BRIEF_CAP:
            raise AdapterError("Frontier proposal brief must be non-empty text", {"index": index})
        charter = proposal.get("charter")
        if charter is not None and not isinstance(charter, dict):
            raise AdapterError("Frontier proposal charter must be an object", {"index": index})
        if out is not None and (out / name).exists():
            raise AdapterError("Frontier proposal collides with an existing drop",
                               {"index": index, "name": name})
        normalized.append({"name": name, "brief": brief, "charter": charter})
    return normalized


def frontier_report(
    root: Path,
    adapter: Callable[[Dict[str, Any]], Dict[str, Any]],
    out: Optional[Path] = None,
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
    apply: bool = False,
) -> Dict[str, Any]:
    if adapter is None:
        raise LoopError("The frontier requires a generator adapter; the kernel does not invent goals")
    if max_proposals <= 0:
        raise AdapterError("max_proposals must be positive")
    root = Path(root)
    if not root.is_dir():
        raise LoopError(f"Frontier root is not a directory: {root}")
    out = Path(out) if out is not None else None
    signals: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for path in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        if not (path / "events.jsonl").is_file():
            continue
        try:
            state = LoopEngine(path).load()
        except LoopError as exc:
            skipped.append({"workspace": str(path), "error": exc.as_dict()})
            continue
        signals.append(_signal(state, path))
    envelope = {
        "protocol": FRONTIER_PROTOCOL,
        "fleet": signals,
        "max_proposals": max_proposals,
    }
    proposals = _validate_proposals(adapter(envelope), max_proposals, out)
    written: List[str] = []
    if apply:
        if out is None:
            raise LoopError("Applying frontier proposals requires an inbox directory (--out)")
        out.mkdir(parents=True, exist_ok=True)
        for proposal in proposals:
            drop = out / proposal["name"]
            drop.mkdir()
            (drop / "brief.md").write_text(proposal["brief"], encoding="utf-8")
            if proposal["charter"]:
                (drop / "charter.json").write_text(
                    json.dumps(proposal["charter"], indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            written.append(str(drop))
    return {
        "mode": "apply" if apply else "dry_run",
        "signals": len(signals),
        "skipped": skipped,
        "proposals": proposals,
        "written": written,
        "exit_code": 0,
    }
