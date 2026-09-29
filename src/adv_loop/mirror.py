"""Append-only event-log mirroring (supervisor plane, Phase F).

A mirror holds a byte-for-byte copy of a workspace's `events.jsonl` somewhere
else — another disk, a mount, a synced directory. Sync appends only the new
tail; a mirror that is not an exact prefix of the source is never repaired
silently, because divergence on an append-only log is evidence of truncation
or rewriting on one side, and the honest response is a loud refusal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from .errors import LoopError
from .sandbox import require_workspace


def mirror_report(workspace: Path, destination: Path) -> Dict[str, Any]:
    workspace = require_workspace(workspace)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    source_bytes = (workspace / "events.jsonl").read_bytes()
    target = destination / f"{workspace.resolve().name}.events.jsonl"
    existing = target.read_bytes() if target.exists() else b""
    if not source_bytes.startswith(existing):
        return {
            "workspace": str(workspace),
            "mirror": str(target),
            "ok": False,
            "diverged": True,
            "detail": (
                "the mirror is not a prefix of the source event log; one side was"
                " truncated or rewritten — refusing to overwrite the evidence"
            ),
            "exit_code": 2,
        }
    tail = source_bytes[len(existing):]
    if tail:
        with open(target, "ab") as handle:
            handle.write(tail)
            handle.flush()
            os.fsync(handle.fileno())
    if target.stat().st_size != len(source_bytes):
        raise LoopError("Mirror append did not reach the expected size", {"mirror": str(target)})
    return {
        "workspace": str(workspace),
        "mirror": str(target),
        "ok": True,
        "diverged": False,
        "appended_bytes": len(tail),
        "total_bytes": len(source_bytes),
        "exit_code": 0,
    }
