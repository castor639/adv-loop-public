"""Pauses are infrastructure, not research faults.

A rate limit on a subscription role, a budget stop, or a human hold writes a
pause file; the supervisor skips paused workspaces and never records a chain
event for it, so a pause can never feed the surgeon threshold. A
subscription rate limit pauses every subscription role fleet-wide, because
they share one account.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PAUSE_FILE = ".harness/pause.json"
SUBSCRIPTION_PAUSE = "subscription-pause.json"
BACKOFF_SECONDS = (30 * 60, 60 * 60, 2 * 60 * 60, 5 * 60 * 60)
RESET_GRACE_SECONDS = 90
GPU_BUDGET_STOP = "gpu_budget_stop"
TASK_BUDGET_STOP = "task_budget_stop"
KNOWN_REASONS = ("subscription_rate_limit", "api_rate_limit", "api_budget_stop", "auth_error", "human_hold",
                 GPU_BUDGET_STOP, TASK_BUDGET_STOP)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def resume_after(resets_at: Optional[str], prior_pauses: int, now: Optional[datetime] = None) -> datetime:
    now = now or utc_now()
    parsed = parse_iso(resets_at) if resets_at else None
    if parsed and parsed > now:
        return parsed + timedelta(seconds=RESET_GRACE_SECONDS)
    return now + timedelta(seconds=BACKOFF_SECONDS[min(prior_pauses, len(BACKOFF_SECONDS) - 1)])


def read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write(path: Path, reason: str, until: datetime, **extra: Any) -> Dict[str, Any]:
    record = {"reason": reason, "paused_at": iso(utc_now()), "resume_after": iso(until), **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def is_active(path: Path, now: Optional[datetime] = None) -> bool:
    record = read(path)
    if not record:
        return False
    if record.get("indefinite"):
        return True
    until = parse_iso(record.get("resume_after", ""))
    return bool(until and until > (now or utc_now()))


def clear(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def pause_workspace(ws: Path, reason: str, until: datetime, **extra: Any) -> Dict[str, Any]:
    return write(ws / PAUSE_FILE, reason, until, **extra)


def workspace_paused(ws: Path) -> bool:
    return is_active(ws / PAUSE_FILE)


def pause_subscription(state_dir: Path, resets_at: Optional[str], **extra: Any) -> Dict[str, Any]:
    path = state_dir / SUBSCRIPTION_PAUSE
    prior = read(path) or {}
    count = int(prior.get("count", 0)) + 1
    until = resume_after(resets_at, count - 1)
    return write(path, "subscription_rate_limit", until, count=count, resets_at=resets_at, **extra)


def subscription_paused(state_dir: Path) -> bool:
    return is_active(state_dir / SUBSCRIPTION_PAUSE)


def human_hold(ws: Path, note: str) -> Dict[str, Any]:
    return write(ws / PAUSE_FILE, "human_hold", utc_now(), indefinite=True, note=note)


def pause_gpu_budget(ws: Path, hours_used: float, max_hours: float, **extra: Any) -> Dict[str, Any]:
    """The workspace's GPU hour cap is spent; only `adv-harness resume` after a raised cap clears it."""

    return write(ws / PAUSE_FILE, GPU_BUDGET_STOP, utc_now(), indefinite=True, hours_used=float(hours_used),
                 max_hours=float(max_hours), **extra)


def sleep_until(until: datetime, step: float = 30.0) -> None:
    while utc_now() < until:
        time.sleep(step)
