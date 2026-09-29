"""The console's remote mode: a local viewer over a supervisor that runs on another box.

The console reads projection files from a workspace root. In remote mode that
root is a local cache mirrored from the box with rsync on every refresh, so
viewing costs one incremental transfer and never touches the remote state.
Anything that changes state (a new workspace, an answer, guidance, a pause, a
step) goes back over ssh as `adv-harness op <name>` on the box, where the
supervisor's own code runs it under the supervisor's user. No key, credential
or event log is ever copied down: payloads, sandboxes, session transcripts and
the host-only directories are excluded from the mirror.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

MIRROR_EXCLUDES = (
    "payload/", "sandbox/", ".harness/host/", ".harness/sessions/",
    ".harness/validate/", ".harness/gpu/jobs/*/outputs/", ".checker-key",
    ".pending-event.json", ".adv-loop.lock", "__pycache__/", "*.key", "*.key.pub", "*known_hosts*", "*.lock",
)
SYNC_INTERVAL_SECONDS = 1.0
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30")


class RemoteError(RuntimeError):
    pass


@dataclass
class RemoteRoot:
    host: str
    remote_root: str = "/srv/adv-loop/workspaces"
    remote_state: str = "/srv/adv-loop/state"
    remote_repo: str = "/srv/adv-loop/repo"
    cache: Path = field(default_factory=lambda: Path.home() / ".cache" / "adv-loop" / "remote")
    user: str = "advloop"
    venv: str = "/srv/adv-loop/venv/bin"
    runner: Runner = subprocess.run
    clock: Callable[[], float] = time.monotonic
    interval: float = SYNC_INTERVAL_SECONDS
    last_sync: float = field(default=-1.0, init=False)
    last_error: Optional[str] = field(default=None, init=False)

    @property
    def root(self) -> Path:
        return self.cache / self.host / "workspaces"

    @property
    def state(self) -> Path:
        return self.cache / self.host / "state"

    def _ssh(self) -> List[str]:
        return ["ssh", *SSH_OPTIONS, self.host]

    def rsync_argv(self, src: str, dest: Path) -> List[str]:
        argv = ["rsync", "-az", "--delete", "--timeout=60"]
        # Session directories stay on the box except the tool ledger, which the console renders as tool rows.
        for pattern in ("transcripts/", "transcripts/sessions/", "transcripts/sessions/*/",
                        "transcripts/sessions/*/ledger.jsonl"):
            argv += ["--include", pattern]
        argv += ["--exclude", "transcripts/sessions/*/*"]
        for pattern in MIRROR_EXCLUDES:
            argv += ["--exclude", pattern]
        argv += ["--rsync-path", f"sudo -u {self.user} rsync", "-e", "ssh " + " ".join(SSH_OPTIONS),
                 f"{self.host}:{src.rstrip('/')}/", str(dest) + "/"]
        return argv

    def sync(self, *, force: bool = False) -> bool:
        """Mirror the workspace root and the state directory; rate limited unless forced."""

        now = self.clock()
        if not force and self.last_sync >= 0 and now - self.last_sync < self.interval:
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        for src, dest in ((self.remote_root, self.root), (self.remote_state, self.state)):
            done = self.runner(self.rsync_argv(src, dest), capture_output=True, text=True, check=False)
            if done.returncode not in (0, 23, 24):
                self.last_error = (done.stderr or "").strip()[-400:] or f"rsync exited {done.returncode}"
                self.last_sync = now
                return False
        self.last_error = None
        self.last_sync = now
        return True

    def op(self, name: str, payload: Dict[str, Any], *, timeout: float = 3900.0) -> Dict[str, Any]:
        """Run `adv-harness op <name>` on the box as the supervisor's user; the payload travels on stdin."""

        remote = (f"sudo -u {self.user} env ADV_LOOP_STATE={self.remote_state} ADV_LOOP_REPO={self.remote_repo} "
                  f"{self.venv}/adv-harness op {name} --root {self.remote_root} --state {self.remote_state} "
                  f"--repo {self.remote_repo}")
        try:
            done = self.runner(self._ssh() + [remote], input=json.dumps(payload), capture_output=True, text=True,
                               timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(f"{name} did not finish within {int(timeout)}s") from exc
        text = (done.stdout or "").strip()
        try:
            result = json.loads(text.splitlines()[-1]) if text else {}
        except ValueError:
            result = {}
        if done.returncode != 0 or not isinstance(result, dict):
            detail = (done.stderr or text or "").strip()[-400:]
            raise RemoteError(f"{name} failed on {self.host}: {detail or 'no output'}")
        if result.get("error"):
            raise RemoteError(str(result.get("message") or result["error"]))
        self.sync(force=True)
        return result

    def workspace_name(self, ws: Path) -> str:
        return Path(ws).name
