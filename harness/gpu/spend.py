"""GPU hours go to the AWS pot and to the workspace's own hour cap.

A job row lands in `<ws>/.gpu-spend.jsonl` (the charter's `max_hours` is
enforced against it) and in `<state>/gpu-spend.jsonl` (the fleet's view beside
the box_run rows). Neither file is read by the API budget and `.spend.jsonl`
never sees a GPU row, so the two budgets cannot borrow from each other.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .. import budget
from . import GPU_SPEND_FILE, HOURLY_USD, STATE_SPEND_FILE


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def hours_billed(seconds: float) -> float:
    """Hours rounded up to the whole minute, the way the box is billed."""

    minutes = math.ceil(max(float(seconds), 0.0) / 60.0)
    return round(minutes / 60.0, 6)


def usd_for(hours: float, hourly_usd: float = HOURLY_USD) -> float:
    return round(float(hours) * float(hourly_usd), 6)


def workspace_spend_path(ws: Path) -> Path:
    return Path(ws) / GPU_SPEND_FILE


def state_spend_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_SPEND_FILE


def _append(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def record_job_spend(ws: Path, state_dir: Optional[Path], job: Any, *, hourly_usd: float = HOURLY_USD) -> Dict[str, Any]:
    """Append one job row to the workspace ledger and, when a state dir is given, the fleet ledger."""

    data = job.as_dict() if hasattr(job, "as_dict") else dict(job)
    seconds = int(data.get("seconds_billed") or 0)
    hours = data.get("hours_billed")
    if hours is None:
        hours = hours_billed(seconds)
    usd = data.get("usd")
    if usd is None:
        usd = usd_for(hours, hourly_usd)
    box = data.get("box") or {}
    row = {
        "at": utc_now(), "kind": "job", "job_id": data.get("job_id"), "session_id": data.get("session_id"),
        "directive_id": data.get("directive_id"), "role": data.get("role"), "mode": data.get("mode"),
        "instance_type": box.get("instance_type"), "hourly_usd": float(hourly_usd),
        "seconds_billed": seconds, "hours_billed": float(hours), "usd": float(usd),
        "box_instance_id": box.get("instance_id"), "status": data.get("status"),
    }
    _append(workspace_spend_path(ws), row)
    if state_dir is not None:
        _append(state_spend_path(state_dir), row)
    return row


def hours_used(ws: Path) -> float:
    return budget.ledger_total(workspace_spend_path(ws), "hours_billed")


def hours_remaining(ws: Path, config: Dict[str, Any]) -> float:
    return round(max(float(config.get("max_hours") or 0.0) - hours_used(ws), 0.0), 6)
