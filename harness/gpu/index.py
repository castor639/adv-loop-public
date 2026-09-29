"""The fleet's view of GPU jobs: one row per state change, and the launch lock.

Sessions run in threads of one supervisor and `adv-harness gpu` is another
process, so the only way to know what is running on the single box is a file:
the index says which jobs are live and the flock serialises launches.
"""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import STATE_JOBS_FILE
from .box_api import GpuBoxError

LAUNCH_LOCK_FILE = "gpu-box.launch.lock"
ACTIVE_STATES = ("queued", "starting", "running")
POLL_SECONDS = 1.0


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def index_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_JOBS_FILE


def append(state_dir: Path, row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    row.setdefault("at", utc_now())
    path = index_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def rows(state_dir: Path) -> List[Dict[str, Any]]:
    path = index_path(state_dir)
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _latest_by_job(state_dir: Path) -> Dict[str, Dict[str, Any]]:
    latest_rows: Dict[str, Dict[str, Any]] = {}
    for row in rows(state_dir):
        job_id = row.get("job_id")
        if isinstance(job_id, str):
            latest_rows[job_id] = row
    return latest_rows


def latest(state_dir: Path, job_id: str) -> Optional[Dict[str, Any]]:
    return _latest_by_job(state_dir).get(job_id)


def running_jobs(state_dir: Path) -> List[Dict[str, Any]]:
    return [row for row in _latest_by_job(state_dir).values() if row.get("status") in ACTIVE_STATES]


class LaunchLock:
    """Non-blocking flock on `<state>/gpu-box.launch.lock`, retried until `wait_seconds`."""

    def __init__(self, state_dir: Path, *, wait_seconds: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(state_dir) / LAUNCH_LOCK_FILE
        self.wait_seconds = float(wait_seconds)
        self.sleep = sleep
        self.clock = clock
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = self.clock() + self.wait_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except OSError:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    handle.close()
                    raise GpuBoxError("busy", "another GPU job holds the launch lock", retryable=True)
                self.sleep(min(POLL_SECONDS, remaining))

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def launch_lock(state_dir: Path, *, wait_seconds: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> Iterator[LaunchLock]:
    lock = LaunchLock(state_dir, wait_seconds=wait_seconds, sleep=sleep, clock=clock)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
