"""The contract between the job runner and the box controller.

`harness.gpu.jobs` orchestrates a job through this interface; `harness.gpu.box`
implements it against AWS and ssh, and tests implement it against a local
directory. The box is only ever reached through these calls, so the runner
never learns an instance id, a key path, or an address.

Remote layout, shared by both sides: the workspace is mirrored at the same
absolute path on the box. A job lives in
`<workspace>/.harness/gpu/jobs/<job_id>/` on both machines. The runner pushes
`job.json` there; the box-side agent writes `status.json`, `stdout.log`, and
`stderr.log` beside it. `status.json` carries `state` (running, done, failed,
timeout, killed, interrupted), `pid`, `container`, `started_at`,
`finished_at`, `exit_code`, and `seconds`.
"""

from __future__ import annotations

import importlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

AGENT_STATES = ("running", "done", "failed", "timeout", "killed", "interrupted")
AGENT_BUSY_EXIT = 75


@dataclass(frozen=True)
class BoxInfo:
    instance_id: str
    state: str
    instance_type: str = "g5.xlarge"
    az: Optional[str] = None
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    ami_id: Optional[str] = None
    launch_time: Optional[str] = None
    started_at: Optional[str] = None
    ready: bool = False
    bootstrapped: bool = False
    jobs_running: int = 0
    last_job_at: Optional[str] = None
    uptime_seconds: Optional[int] = None
    mirror_root: str = "/srv/adv-loop/workspaces"

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class GpuBoxError(RuntimeError):
    """A box-side refusal or failure the tool reports to the model as data."""

    def __init__(self, kind: str, message: str, *, retryable: bool = False,
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.details = dict(details or {})

    def as_dict(self) -> Dict[str, Any]:
        return {"error": f"gpu_{self.kind}", "message": str(self), "retryable": self.retryable,
                "details": self.details}


class Box(Protocol):
    """What the job runner needs from a box controller."""

    def describe(self, *, cached_ok: bool = False) -> BoxInfo: ...

    def ensure_running(self, *, need_seconds: int = 0) -> BoxInfo:
        """Start the box if needed and block until the agent answers; raises GpuBoxError."""
        ...

    def stop_if_idle(self, idle_minutes: Optional[int] = None) -> Dict[str, Any]: ...

    def stop_now(self, reason: str = "operator", *, kill_jobs: bool = True) -> Dict[str, Any]: ...

    def exec_remote(self, argv: Sequence[str], cwd: Optional[str] = None, timeout: float = 600.0,
                    env: Optional[Dict[str, str]] = None, stdin: Optional[str] = None
                    ) -> "subprocess.CompletedProcess[str]":
        """Run an argv list on the box; raises subprocess.TimeoutExpired on timeout."""
        ...

    def push(self, paths: Sequence[str], dest: str, *, workspace: Path, delete: bool = False) -> Dict[str, Any]:
        """Copy workspace-relative paths into the mirror root `dest`; refuses secrets and escapes."""
        ...

    def pull(self, paths: Sequence[str], src: str, *, workspace: Path) -> Dict[str, Any]:
        """Copy workspace-relative paths back from the mirror root `src`; never overwrites projections."""
        ...

    def submit(self, job_dir: str) -> Dict[str, Any]:
        """`adv-gpu-agent submit <job_dir>`; returns status.json; raises GpuBoxError('busy') on exit 75."""
        ...

    def wait(self, job_dir: str, *, max_seconds: float) -> Dict[str, Any]:
        """`adv-gpu-agent wait <job_dir> --max N`; returns the latest status.json, terminal or not."""
        ...

    def status(self, job_dir: str) -> Dict[str, Any]: ...

    def kill(self, job_dir: str) -> Dict[str, Any]: ...

    def register_job(self, job_id: str, *, workspace: str, session_id: str, timeout_seconds: int) -> None:
        """Record an active job in the box state so idle stop and the reaper see it."""
        ...

    def release_job(self, job_id: str) -> None: ...

    def active_jobs(self) -> List[Dict[str, Any]]: ...

    def reap(self) -> Dict[str, Any]:
        """Orphan cleanup plus stop_if_idle; never raises."""
        ...


BoxFactory = Callable[[Path], Box]


def load_box(state_dir: Path, **kwargs: Any) -> Box:
    """The production controller, imported lazily so tests never touch aws or ssh."""

    module = importlib.import_module("harness.gpu.box")
    return module.GpuBox(Path(state_dir), **kwargs)


@dataclass
class JobDirs:
    """Where a job's files live on either machine."""

    workspace: Path
    job_id: str
    mirror_root: str = field(default="/srv/adv-loop/workspaces")

    @property
    def relative(self) -> str:
        return f".harness/gpu/jobs/{self.job_id}"

    @property
    def local(self) -> Path:
        return self.workspace / self.relative

    @property
    def host_record(self) -> Path:
        return self.workspace / ".harness" / "host" / "gpu" / "jobs" / self.job_id

    @property
    def remote_workspace(self) -> str:
        return str(self.workspace)

    @property
    def remote(self) -> str:
        return f"{self.workspace}/{self.relative}"
