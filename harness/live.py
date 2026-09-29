"""Per-workspace live log: one line per request, response, session, and verdict.

`.harness/live.log` is operator-facing only. Nothing reads it back into a
prompt or a decision; it exists so a `tail -F` (or the console footer) shows
what the harness is waiting on while a model call is in flight.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

LIVE_FILE = ".harness/live.log"


def note(ws: Path, text: str) -> None:
    path = Path(ws) / LIVE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + text.replace("\n", " ") + "\n")
    except OSError:
        pass  # a full disk must not break a session


def tail(ws: Path, lines: int = 1) -> List[str]:
    path = Path(ws) / LIVE_FILE
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 16384))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]
