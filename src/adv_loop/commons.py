"""Commons export: discoveries from one workspace become citable by the rest.

The commons is a read-only aggregation directory: per-workspace manifests of
assets, lessons, open wishes, barriers, and (for completed work) the final
report — each stamped with the workspace's event head as provenance. Other
workspaces cite commons paths as asset locators in survey moves; nothing here
touches any event chain, so the kernel stays untouched and neutral.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from .engine import LoopEngine
from .errors import LoopError
from .storage import utc_now


def _manifest(state: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    return {
        "task_id": state["task_id"],
        "task": state["task"],
        "status": state["status"],
        "policy_version": state["policy_version"],
        "event_head": state["integrity"]["event_head"],
        "exported_at": utc_now(),
        "source_workspace": str(workspace.resolve()),
        "criteria": [
            {"id": criterion["id"], "text": criterion["text"], "status": criterion["status"]}
            for criterion in state["criteria"]
        ],
        "evidence_count": len(state["evidence"]),
        "assets": list(state.get("assets", [])),
        "lessons": list(state.get("lessons", [])),
        "open_wishes": [
            {"id": wish["id"], "statement": wish["statement"], "would_open": wish["would_open"]}
            for wish in state.get("wishes", [])
            if wish["status"] == "open"
        ],
        "barriers": [
            {"id": barrier["id"], "name": barrier["name"], "statement": barrier["statement"]}
            for barrier in state.get("barriers", [])
        ],
        "report_exported": bool(state["status"] == "completed" and state["report"]["ready"]),
    }


def commons_report(root: Path, out: Path) -> Dict[str, Any]:
    """Aggregate every readable workspace under root into the commons at out."""

    root = Path(root)
    out = Path(out)
    if not root.is_dir():
        raise LoopError(f"Commons root is not a directory: {root}")
    out.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    worst = 0
    index: List[Dict[str, Any]] = []
    for path in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        if not (path / "events.jsonl").is_file():
            continue
        try:
            state = LoopEngine(path).load()
        except LoopError as exc:
            rows.append({"workspace": str(path), "ok": False, "error": exc.as_dict()})
            worst = max(worst, 2)
            continue
        manifest = _manifest(state, path)
        entry_dir = out / state["task_id"]
        entry_dir.mkdir(exist_ok=True)
        (entry_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if manifest["report_exported"]:
            shutil.copyfile(path / "report.md", entry_dir / "report.md")
        rows.append({"workspace": str(path), "ok": True, "task_id": state["task_id"],
                     "status": state["status"]})
        index.append({
            "task_id": state["task_id"],
            "status": state["status"],
            "task": state["task"],
            "assets": len(manifest["assets"]),
            "lessons": len(manifest["lessons"]),
            "report": manifest["report_exported"],
        })
    (out / "index.json").write_text(
        json.dumps({"generated_at": utc_now(), "entries": index}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "root": str(root),
        "out": str(out),
        "exported": sum(1 for row in rows if row.get("ok")),
        "rows": rows,
        "ok": worst == 0,
        "exit_code": worst,
    }
