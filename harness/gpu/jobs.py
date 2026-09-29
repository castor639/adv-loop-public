"""GPU job records and the synchronous job runner.

The runner is the only writer of a job's record and the only caller of the
box for that job. Every step lands in the authoritative record before the
tool result exists, so a crash anywhere leaves a record the sweeper can
finish, and the model's account of the job never becomes the evidence:
hashes come from the pulled files, exit codes from the box's status file,
hours from the clock. The public copy of the record under `.harness/gpu/` is
what the session may read; the copy under `.harness/host/` is what the
checkers trust.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from adv_loop.errors import ValidationError
from adv_loop.pathsafe import ensure_within, hash_file, safe_relative
from adv_loop.storage import atomic_write_text, object_hash

from .. import improvement, pause, transcripts
from . import DEFAULTS, ENV_FORBIDDEN_PREFIXES, ENV_KEY_PATTERN, NETWORKS, index, lock, spend
from .box_api import AGENT_STATES, Box, GpuBoxError, JobDirs

JOB_ID_PATTERN = re.compile(r"^g-\d{8}T\d{6}Z-[0-9a-f]{8}$")
ENV_KEY = re.compile(ENV_KEY_PATTERN)
ENV_VALUE_LIMIT = 4096
LABEL_LIMIT = 80
OUTPUT_TAIL = 12000
WAIT_SLICE_SECONDS = 300.0
LOG_SEPARATOR = "\n---\n"
LOG_NAMES = ("stdout.log", "stderr.log")
HOST_ONLY_FIELDS = ("session_id", "tool_use_id", "box", "pid")
ARG_NAMES = ("command", "cwd", "inputs", "outputs", "timeout_seconds", "env", "network", "label")
COLLECTABLE_STATES = ("running", "finished", "timeout", "cancelled", "interrupted")
AGENT_OUTCOMES = {"done": "finished", "failed": "finished", "timeout": "timeout", "killed": "cancelled",
                  "interrupted": "interrupted"}
# payload/ is where outputs belong (an evidence source, never a projection), so the other
# immutable workspace entries and the harness-owned files are what a job may not overwrite.
PROTECTED_OUTPUT_PATHS = tuple(p for p in improvement.IMMUTABLE_WORKSPACE_PATHS if p != "payload") + (
    ".harness", "sandbox", "transcripts", ".spend.jsonl", ".gpu-spend.jsonl", ".adv-loop.lock",
    ".sandbox-status.json", ".recovery",
)
PROTECTED_INPUT_PATHS = (".checker-key", ".harness", "transcripts", "events.jsonl", ".pending-event.json",
                         ".adv-loop.lock", ".spend.jsonl", ".gpu-spend.jsonl")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _seconds_between(start: Optional[str], end: Optional[str]) -> int:
    first = pause.parse_iso(start or "")
    last = pause.parse_iso(end or "")
    if first is None or last is None:
        return 0
    return max(int(round((last - first).total_seconds())), 0)


def _under(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


def new_job_id(clock: Callable[[], float] = time.time) -> str:
    return f"g-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))}-{secrets.token_hex(4)}"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True
    return True


def hash_path(path: Path) -> Tuple[str, int]:
    """sha256 and byte count of a file, or of a directory's sorted file manifest."""

    if path.is_file():
        return hash_file(path)
    if path.is_dir():
        entries: List[List[Any]] = []
        total = 0
        for member in sorted(p for p in path.rglob("*") if p.is_file()):
            digest, size = hash_file(member)
            entries.append([member.relative_to(path).as_posix(), digest, size])
            total += size
        return object_hash(entries), total
    raise FileNotFoundError(str(path))


def _copy_path(source: Path, target: Path) -> None:
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copyfile(source, target)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@dataclass
class Job:
    job_id: str
    workspace: str
    ws_id: str
    session_id: Optional[str] = None
    directive_id: Optional[str] = None
    role: Optional[str] = None
    mode: Optional[str] = None
    tool_use_id: Optional[str] = None
    label: str = ""
    command: List[str] = field(default_factory=list)
    cwd: str = "payload/scratch"
    env: Dict[str, str] = field(default_factory=dict)
    network: str = "bridge"
    timeout_seconds: int = 0
    inputs: List[Dict[str, Any]] = field(default_factory=list)
    outputs_declared: List[str] = field(default_factory=list)
    status: str = "queued"
    reason: Optional[str] = None
    returncode: Optional[int] = None
    box: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    launched_at: Optional[str] = None
    finished_at: Optional[str] = None
    collected_at: Optional[str] = None
    collected_by_session: Optional[str] = None
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    missing_outputs: List[str] = field(default_factory=list)
    stdout_sha256: Optional[str] = None
    stderr_sha256: Optional[str] = None
    log_hash: Optional[str] = None
    seconds_billed: Optional[int] = None
    hours_billed: Optional[float] = None
    usd: Optional[float] = None
    # The terminal run state (finished, timeout, cancelled, interrupted, failed) survives the move
    # to `collected`, so the tool result and the checkers can still tell how the job ended.
    outcome: Optional[str] = None
    outputs_ready: bool = False
    pid: Optional[int] = None
    message: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        names = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in names})


def public_view(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if key not in HOST_ONLY_FIELDS}


def summary(job: Job) -> Dict[str, Any]:
    """What the ledger row carries about a job."""

    box = job.box or {}
    return {
        "job_id": job.job_id, "status": job.status, "outcome": job.outcome, "reason": job.reason,
        "box_instance_id": box.get("instance_id"), "instance_type": box.get("instance_type"),
        "image_digest": job.environment.get("image_digest"), "toolchain_hash": job.environment.get("toolchain_hash"),
        "launched_at": job.launched_at, "finished_at": job.finished_at, "seconds_billed": job.seconds_billed,
        "usd": job.usd, "inputs": list(job.inputs), "outputs": list(job.outputs),
        "missing_outputs": list(job.missing_outputs),
    }


def environment_of(observed: lock.GpuLock) -> Dict[str, Any]:
    return {"image": observed.image, "image_digest": observed.image_digest, "cuda": observed.cuda,
            "torch": observed.torch, "python": observed.python, "driver": observed.driver, "gpu": observed.gpu,
            "toolchain_hash": observed.toolchain_hash}


class JobStore:
    """Both copies of every job record in one workspace."""

    def __init__(self, ws: Path) -> None:
        self.ws = Path(ws).resolve()

    def dirs(self, job_id: str) -> JobDirs:
        return JobDirs(self.ws, job_id)

    @staticmethod
    def _dump(data: Dict[str, Any]) -> str:
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    def write(self, job: Job) -> None:
        dirs = self.dirs(job.job_id)
        data = job.as_dict()
        atomic_write_text(dirs.host_record / "job.json", self._dump(data))
        atomic_write_text(dirs.local / "job.json", self._dump(public_view(data)))

    def _load(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def read(self, job_id: str) -> Optional[Job]:
        if not isinstance(job_id, str) or not JOB_ID_PATTERN.match(job_id):
            return None
        data = self._load(self.dirs(job_id).host_record / "job.json")
        return Job.from_dict(data) if data else None

    def public(self, job_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(job_id, str) or not JOB_ID_PATTERN.match(job_id):
            return None
        return self._load(self.dirs(job_id).local / "job.json")

    def list(self) -> List[Job]:
        root = self.ws / ".harness" / "host" / "gpu" / "jobs"
        if not root.is_dir():
            return []
        jobs: List[Job] = []
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and JOB_ID_PATTERN.match(entry.name):
                data = self._load(entry / "job.json")
                if data:
                    jobs.append(Job.from_dict(data))
        return jobs


@dataclass
class JobSpec:
    command: List[str]
    cwd: str
    inputs: List[str]
    outputs: List[str]
    env: Dict[str, str]
    timeout_seconds: int
    network: str
    label: str


def _relative(ws: Path, value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a workspace-relative path string")
    rel = safe_relative(value)
    ensure_within(ws, ws / rel)
    return rel.as_posix()


def _path_list(ws: Path, value: Any, name: str) -> List[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of workspace-relative paths")
    seen: List[str] = []
    for item in value:
        rel = _relative(ws, item, name)
        if rel not in seen:
            seen.append(rel)
    return seen


def validate_args(ws: Path, args: Any, config: Dict[str, Any]) -> JobSpec:
    """The gpu_run contract; raises ValueError or ValidationError with the message the model sees."""

    if not isinstance(args, dict):
        raise ValueError("gpu_run arguments must be an object")
    unknown = set(args) - set(ARG_NAMES)
    if unknown:
        raise ValueError(f"unknown gpu_run arguments: {sorted(unknown)}")
    command = args.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
        raise ValueError("command must be a non-empty argv list of non-empty strings")
    cwd = _relative(ws, args.get("cwd") or "payload/scratch", "cwd")
    inputs = _path_list(ws, args.get("inputs") or [], "inputs")
    for rel in inputs:
        if any(_under(rel, p) for p in PROTECTED_INPUT_PATHS):
            raise ValueError(f"input {rel} is host-only material and is never pushed")
        if not (ws / rel).exists():
            raise ValueError(f"input does not exist: {rel}")
    outputs = _path_list(ws, args.get("outputs"), "outputs")
    if not outputs:
        raise ValueError("outputs must name at least one workspace-relative path")
    for rel in outputs:
        if any(_under(rel, p) for p in PROTECTED_OUTPUT_PATHS):
            raise ValueError(f"output {rel} would overwrite a harness-owned path")
    env = args.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("env must be an object of string values")
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_KEY.match(key):
            raise ValueError(f"env key is not allowed: {key!r}")
        if key.startswith(ENV_FORBIDDEN_PREFIXES):
            raise ValueError(f"env key {key} would carry credentials or loader settings into the job")
        if not isinstance(value, str) or len(value) > ENV_VALUE_LIMIT:
            raise ValueError(f"env value for {key} must be a string of at most {ENV_VALUE_LIMIT} characters")
    timeout = args.get("timeout_seconds", config["max_job_seconds"])
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout_seconds must be a number of seconds")
    timeout = int(min(max(float(timeout), float(config["min_job_seconds"])), float(config["max_job_seconds"])))
    network = args.get("network") or config["network"]
    if network not in NETWORKS:
        raise ValueError(f"network must be one of {list(NETWORKS)}")
    label = args.get("label") or ""
    if not isinstance(label, str) or len(label) > LABEL_LIMIT:
        raise ValueError(f"label must be a string of at most {LABEL_LIMIT} characters")
    return JobSpec(command=list(command), cwd=cwd, inputs=inputs, outputs=outputs, env=dict(env),
                   timeout_seconds=timeout, network=str(network), label=label)


def _gpu_allowed(guard: Any, projected_usd: float) -> bool:
    if guard is None:
        return True
    try:
        return bool(guard.allows_gpu(projected_usd=projected_usd, unreported_usd=0.0))
    except TypeError:
        # The shipped AwsSpendGuard.allows_gpu takes no arguments; the AWS wave adds the projection.
        return bool(guard.allows_gpu())


def session_dead(ws: Path, job: Job, alive: Callable[[int], bool] = pid_alive) -> bool:
    if job.session_id and (transcripts.session_dir(ws, job.session_id) / "result.json").is_file():
        return True
    if job.pid is None:
        return not job.session_id
    return not alive(job.pid)


def _write_and_index(store: JobStore, state_dir: Optional[Path], job: Job) -> None:
    store.write(job)
    if state_dir is not None:
        index.append(state_dir, {"job_id": job.job_id, "ws_id": job.ws_id, "workspace": job.workspace,
                                 "session_id": job.session_id, "status": job.status, "outcome": job.outcome,
                                 "reason": job.reason})


ToolTuple = Tuple[Dict[str, Any], str, str, Dict[str, Any]]


class JobRunner:
    """One session's GPU dispatch: validate, guard, launch, wait, collect, ledger."""

    def __init__(self, box: Box, ws: Path, state_dir: Path, config: Optional[Dict[str, Any]], *,
                 session_id: str, directive_id: Optional[str] = None, role: Optional[str] = None,
                 mode: Optional[str] = None, guard: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, pid: Optional[int] = None) -> None:
        self.box = box
        self.ws = Path(ws).resolve()
        self.state_dir = Path(state_dir)
        self.config = {**DEFAULTS, **(config or {})}
        self.session_id = session_id
        self.directive_id = directive_id
        self.role = role
        self.mode = mode
        self.guard = guard
        self.clock = clock
        self.sleep = sleep
        self.pid = os.getpid() if pid is None else pid
        self.store = JobStore(self.ws)

    def _now(self) -> str:
        return _iso(self.clock())

    def _transition(self, job: Job, status: str) -> None:
        job.status = status
        _write_and_index(self.store, self.state_dir, job)

    def _refusal(self, job: Optional[Job], reason: str, message: str, started: float) -> ToolTuple:
        payload: Dict[str, Any] = {"status": "refused", "reason": reason, "message": message,
                                   "hours_remaining": spend.hours_remaining(self.ws, self.config),
                                   "duration_ms": int((self.clock() - started) * 1000)}
        extra: Dict[str, Any] = {"exit_code": None, "interrupted": False}
        if job is not None:
            payload["job_id"] = job.job_id
            extra["job"] = summary(job)
        return payload, "", message, extra

    def _refuse(self, job: Job, reason: str, message: str, started: float) -> ToolTuple:
        job.reason = reason
        job.message = message
        job.finished_at = self._now()
        self._transition(job, "refused")
        return self._refusal(job, reason, message, started)

    def _pause_for_cap(self, job: Job) -> None:
        pause.pause_workspace(self.ws, "gpu_budget_stop", pause.utc_now(), indefinite=True,
                              hours_used=spend.hours_used(self.ws), max_hours=float(self.config["max_hours"]),
                              job_id=job.job_id, session_id=self.session_id)

    def run(self, args: Any, tool_use_id: Optional[str] = None) -> ToolTuple:
        started = self.clock()
        try:
            spec = validate_args(self.ws, args, self.config)
        except (ValidationError, ValueError, TypeError) as exc:
            return self._refusal(None, "invalid_input", str(exc), started)
        job = Job(job_id=new_job_id(self.clock), workspace=str(self.ws), ws_id=self.ws.name,
                  session_id=self.session_id, directive_id=self.directive_id, role=self.role, mode=self.mode,
                  tool_use_id=tool_use_id, label=spec.label, command=spec.command, cwd=spec.cwd, env=spec.env,
                  network=spec.network, timeout_seconds=spec.timeout_seconds, outputs_declared=spec.outputs,
                  created_at=self._now(), pid=self.pid)
        self._transition(job, "queued")
        projected_hours = spec.timeout_seconds / 3600.0
        if not _gpu_allowed(self.guard, projected_hours * float(self.config["hourly_usd"])):
            return self._refuse(job, "aws_gpu_stop", "the AWS budget line refuses GPU starts; no hours were used", started)
        remaining = spend.hours_remaining(self.ws, self.config)
        if remaining < projected_hours:
            self._pause_for_cap(job)
            return self._refuse(job, "gpu_hours_cap",
                                f"{remaining:.2f} GPU hours remain of {float(self.config['max_hours']):g}; a"
                                f" {projected_hours:.2f} h job does not fit and the workspace is paused until the"
                                " operator raises max_hours", started)
        if lock.read_fleet_lock(self.state_dir) is None:
            return self._refuse(job, "box_unavailable", "no fleet GPU lock; the box has not been provisioned", started)
        ws_lock = lock.read_workspace_lock(self.ws)
        if ws_lock is None:
            return self._refuse(job, "not_configured", "the workspace has no sandbox/gpu.lock", started)
        launch = index.LaunchLock(self.state_dir, wait_seconds=float(self.config["queue_wait_seconds"]),
                                  sleep=self.sleep, clock=self.clock)
        try:
            launch.acquire()
        except GpuBoxError as exc:
            return self._refuse(job, "busy", str(exc), started)
        try:
            return self._launch(job, spec, ws_lock, started)
        finally:
            launch.release()

    def _launch(self, job: Job, spec: JobSpec, ws_lock: lock.GpuLock, started: float) -> ToolTuple:
        try:
            info = self.box.ensure_running(need_seconds=job.timeout_seconds)
        except GpuBoxError as exc:
            return self._refuse(job, exc.kind, str(exc), started)
        try:
            observed = lock.observe(self.box, image=str(self.config["image"]))
        except GpuBoxError as exc:
            return self._refuse(job, exc.kind, str(exc), started)
        if observed.toolchain_hash != ws_lock.toolchain_hash:
            return self._refuse(job, "environment_drift",
                                f"the box toolchain {observed.toolchain_hash[:12]} is not the workspace lock"
                                f" {ws_lock.toolchain_hash[:12]} (driver {observed.driver}, cuda {observed.cuda},"
                                f" image {observed.image_digest[:19]}); nothing was pushed", started)
        job.environment = environment_of(observed)
        job.box = {"instance_id": info.instance_id, "instance_type": info.instance_type, "started_at": info.started_at}
        try:
            job.inputs = [self._hashed(rel) for rel in spec.inputs]
        except (OSError, ValidationError) as exc:
            return self._refuse(job, "invalid_input", f"input vanished before push: {exc}", started)
        self._transition(job, "starting")
        self.box.register_job(job.job_id, workspace=str(self.ws), session_id=self.session_id,
                              timeout_seconds=job.timeout_seconds)
        try:
            return self._execute(job, spec, started)
        finally:
            self.box.release_job(job.job_id)

    def _hashed(self, rel: str) -> Dict[str, Any]:
        digest, size = hash_path(self.ws / rel)
        return {"path": rel, "sha256": digest, "bytes": size}

    def _execute(self, job: Job, spec: JobSpec, started: float) -> ToolTuple:
        dirs = self.store.dirs(job.job_id)
        try:
            self.box.push(list(spec.inputs) + [f"{dirs.relative}/job.json"], str(self.ws), workspace=self.ws)
        except Exception as exc:
            return self._refuse(job, "push_failed", str(exc), started)
        try:
            self.box.submit(dirs.remote)
        except GpuBoxError as exc:
            return self._refuse(job, "busy" if exc.kind == "busy" else "launch_failed", str(exc), started)
        except Exception as exc:
            return self._refuse(job, "launch_failed", str(exc), started)
        job.launched_at = self._now()
        self._transition(job, "running")
        deadline = self.clock() + job.timeout_seconds + float(self.config["collect_grace_seconds"])
        outcome, status = self._await(dirs, deadline)
        self._settle(job, outcome, status)
        self._finalize(job)
        return self._result(job, started)

    def _await(self, dirs: JobDirs, deadline: float) -> Tuple[str, Dict[str, Any]]:
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                try:
                    self.box.kill(dirs.remote)
                except Exception:
                    pass
                return "timeout", {"state": "timeout", "exit_code": None, "killed_by": "harness"}
            try:
                status = self.box.wait(dirs.remote, max_seconds=min(WAIT_SLICE_SECONDS, remaining))
            except GpuBoxError as exc:
                return "interrupted", {"state": "interrupted", "exit_code": None, "error": exc.kind}
            state = status.get("state") if isinstance(status, dict) else None
            if state in AGENT_OUTCOMES:
                return AGENT_OUTCOMES[state], status
            if state not in AGENT_STATES:
                return "failed", {"state": str(state), "exit_code": None}

    def _settle(self, job: Job, outcome: str, status: Dict[str, Any]) -> None:
        code = status.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int):
            code = None
        if status.get("state") == "failed" and code is None:
            code = 1
        job.returncode = code
        job.finished_at = self._now()
        job.outcome = outcome
        if outcome == "failed":
            job.reason = "launch_failed"
            job.message = f"the box reported no run for this job (state {status.get('state')!r})"
        self._transition(job, outcome)

    def _remote_output_bytes(self, outputs: Sequence[str]) -> Optional[int]:
        try:
            done = self.box.exec_remote(["du", "-sb"] + [f"{self.ws}/{rel}" for rel in outputs], timeout=600.0)
        except Exception:
            return None
        total = 0
        for line in done.stdout.splitlines():
            head = line.split("\t")[0].split(" ")[0].strip()
            if head.isdigit():
                total += int(head)
        return total

    def _collect_outputs(self, job: Job, dirs: JobDirs) -> None:
        job.outputs = []
        job.missing_outputs = []
        for rel in job.outputs_declared:
            source = self.ws / rel
            if not source.exists():
                job.missing_outputs.append(rel)
                continue
            digest, size = hash_path(source)
            job.outputs.append({"path": rel, "sha256": digest, "bytes": size})
            _copy_path(source, dirs.local / "outputs" / rel)

    def _logs(self, dirs: JobDirs) -> Tuple[str, str]:
        return _read_text(dirs.local / LOG_NAMES[0]), _read_text(dirs.local / LOG_NAMES[1])

    def _finalize(self, job: Job, *, pull: bool = True) -> None:
        """From a terminal run state to `collected`: size check, pull, hash, copy, bill."""

        dirs = self.store.dirs(job.job_id)
        hourly = float(self.config["hourly_usd"])
        if job.outcome == "failed":
            job.seconds_billed, job.hours_billed, job.usd = 0, 0.0, 0.0
            self._transition(job, "failed")
            return
        record_spend = job.hours_billed is None
        # A collection reason is recomputed on every attempt, so a retry after a failed pull can succeed.
        job.reason = None
        job.message = None
        if pull:
            paths = [dirs.relative]
            total = self._remote_output_bytes(job.outputs_declared)
            cap = int(self.config["max_output_bytes"])
            if total is not None and total > cap:
                job.reason = "output_too_large"
                job.message = f"declared outputs total {total} bytes on the box, above the cap of {cap}"
            else:
                paths = list(job.outputs_declared) + paths
            try:
                self.box.pull(paths, str(self.ws), workspace=self.ws)
            except Exception as exc:
                job.reason = "pull_failed"
                job.message = str(exc)
                try:
                    self.box.pull([dirs.relative], str(self.ws), workspace=self.ws)
                except Exception:
                    pass
        if job.reason is None:
            self._collect_outputs(job, dirs)
            if job.missing_outputs:
                job.reason = "output_missing"
                job.message = "declared outputs the job did not produce: " + ", ".join(job.missing_outputs)
        stdout, stderr = self._logs(dirs)
        job.stdout_sha256 = _sha_text(stdout)
        job.stderr_sha256 = _sha_text(stderr)
        job.log_hash = _sha_text(stdout + LOG_SEPARATOR + stderr)
        job.seconds_billed = _seconds_between(job.created_at, job.finished_at)
        job.hours_billed = spend.hours_billed(job.seconds_billed)
        job.usd = spend.usd_for(job.hours_billed, hourly)
        if job.reason in (None, "output_missing"):
            job.collected_at = self._now()
            job.collected_by_session = self.session_id
            job.outputs_ready = False
            self._transition(job, "collected")
        else:
            job.outputs_ready = True
            self._transition(job, job.outcome or job.status)
        if record_spend:
            spend.record_job_spend(self.ws, self.state_dir, job, hourly_usd=hourly)
            if spend.hours_remaining(self.ws, self.config) <= 0:
                self._pause_for_cap(job)

    def _result(self, job: Job, started: float) -> ToolTuple:
        dirs = self.store.dirs(job.job_id)
        stdout, stderr = self._logs(dirs)
        first = job.outputs[0]["path"] if job.outputs else (job.outputs_declared[0] if job.outputs_declared else None)
        payload: Dict[str, Any] = {
            "job_id": job.job_id, "status": job.outcome or job.status, "returncode": job.returncode,
            "reason": job.reason, "stdout": stdout[-OUTPUT_TAIL:], "stderr": stderr[-OUTPUT_TAIL:],
            "outputs": list(job.outputs), "missing_outputs": list(job.missing_outputs),
            "environment": {"instance_type": (job.box or {}).get("instance_type"),
                            **{key: job.environment.get(key) for key in
                               ("gpu", "driver", "cuda", "image", "image_digest", "toolchain_hash")}},
            "duration_ms": int((self.clock() - started) * 1000), "hours_billed": job.hours_billed, "usd": job.usd,
            "hours_remaining": spend.hours_remaining(self.ws, self.config),
            "logs": {"stdout": f"{dirs.relative}/{LOG_NAMES[0]}", "stderr": f"{dirs.relative}/{LOG_NAMES[1]}"},
            "validate_hint": {"hook": "gpu-replay", "input": {"job_id": job.job_id, "artifact": first}},
            "collected": job.status == "collected",
        }
        if job.message:
            payload["message"] = job.message
        extra = {"exit_code": job.returncode, "interrupted": job.outcome in ("timeout", "interrupted"),
                 "job": summary(job)}
        return payload, stdout, stderr, extra

    def _outputs_present_locally(self, job: Job, dirs: JobDirs) -> bool:
        return (dirs.local / LOG_NAMES[0]).is_file() and all((self.ws / rel).exists() for rel in job.outputs_declared)

    def collect(self, job_id: Any, tool_use_id: Optional[str] = None) -> ToolTuple:
        """Finish a job whose session is gone; idempotent once collected."""

        started = self.clock()
        job = self.store.read(job_id) if isinstance(job_id, str) else None
        if job is None:
            return self._refusal(None, "invalid_input", f"unknown job id: {job_id!r}", started)
        if job.status == "collected":
            return self._result(job, started)
        if job.status not in COLLECTABLE_STATES and job.status != "starting":
            return self._refusal(job, "invalid_input", f"job {job.job_id} has nothing to collect (status {job.status})",
                                 started)
        dirs = self.store.dirs(job.job_id)
        needs_box = job.status in ("starting", "running") or not self._outputs_present_locally(job, dirs)
        if needs_box:
            try:
                self.box.ensure_running(need_seconds=int(self.config["collect_grace_seconds"]))
            except GpuBoxError as exc:
                return self._refusal(job, exc.kind, str(exc), started)
        if job.status in ("starting", "running"):
            launched = pause.parse_iso(job.launched_at or job.created_at or "")
            base = launched.timestamp() if launched else self.clock()
            deadline = base + job.timeout_seconds + float(self.config["collect_grace_seconds"])
            outcome, status = self._await(dirs, deadline)
            self._settle(job, outcome, status)
        self._finalize(job, pull=needs_box)
        return self._result(job, started)


def collect(ws: Path, state_dir: Path, job_id: str, *, session_id: str, box: Optional[Box] = None,
            config: Optional[Dict[str, Any]] = None, tool_use_id: Optional[str] = None) -> ToolTuple:
    """gpu_collect outside a runner; without a box only jobs already pulled to the host can finish."""

    runner = JobRunner(box if box is not None else _NoBox(), ws, state_dir, config, session_id=session_id)
    return runner.collect(job_id, tool_use_id=tool_use_id)


class _NoBox:
    """Stands in when no controller is configured: every box call is a box_unavailable refusal."""

    def __getattr__(self, name: str) -> Any:
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise GpuBoxError("box_unavailable", "no GPU box controller is configured for this call")
        return refuse


def cancel(box: Box, ws: Path, state_dir: Optional[Path], job_id: str) -> Dict[str, Any]:
    store = JobStore(ws)
    job = store.read(job_id)
    if job is None:
        raise ValueError(f"unknown job id: {job_id!r}")
    if job.status not in ("starting", "running"):
        return job.as_dict()
    dirs = store.dirs(job.job_id)
    try:
        box.kill(dirs.remote)
    except Exception as exc:
        job.message = f"kill failed: {exc}"
    job.finished_at = utc_now()
    job.outcome = "cancelled"
    job.status = "cancelled"
    job.outputs_ready = True
    _write_and_index(store, state_dir, job)
    try:
        box.release_job(job.job_id)
    except Exception:
        pass
    return job.as_dict()


def sweep(box: Box, root: Path, state_dir: Optional[Path], *,
          alive: Callable[[int], bool] = pid_alive) -> Dict[str, List[str]]:
    """Settle jobs whose session died: poll once, pull logs, leave the declared outputs on the box."""

    report: Dict[str, List[str]] = {"settled": [], "still_running": [], "errors": []}
    root = Path(root)
    if not root.is_dir():
        return report
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        store = JobStore(entry)
        ws = store.ws
        for job in store.list():
            if job.status not in ("starting", "running") or not session_dead(ws, job, alive):
                continue
            dirs = store.dirs(job.job_id)
            try:
                status = box.status(dirs.remote)
            except Exception as exc:
                report["errors"].append(f"{job.job_id}: {exc}")
                continue
            state = status.get("state") if isinstance(status, dict) else None
            if state == "running":
                report["still_running"].append(job.job_id)
                continue
            outcome = AGENT_OUTCOMES.get(state, "failed")
            try:
                box.pull([dirs.relative], str(ws), workspace=ws)
            except Exception as exc:
                report["errors"].append(f"{job.job_id}: logs not pulled: {exc}")
            code = status.get("exit_code") if isinstance(status, dict) else None
            job.returncode = code if isinstance(code, int) and not isinstance(code, bool) else None
            job.finished_at = utc_now()
            job.outcome = outcome
            job.status = outcome
            job.reason = "launch_failed" if outcome == "failed" else None
            job.outputs_ready = outcome != "failed"
            _write_and_index(store, state_dir, job)
            try:
                box.release_job(job.job_id)
            except Exception:
                pass
            report["settled"].append(job.job_id)
    return report


def pending_collection(ws: Path, alive: Callable[[int], bool] = pid_alive) -> List[Job]:
    store = JobStore(ws)
    return [job for job in store.list()
            if job.status in COLLECTABLE_STATES and (job.outputs_ready or session_dead(store.ws, job, alive))]


def pending_collection_note(ws: Path, alive: Callable[[int], bool] = pid_alive) -> Optional[str]:
    """Supplemental text for the next session: jobs whose outputs are not in the workspace yet."""

    pending = pending_collection(ws, alive)
    if not pending:
        return None
    lines = ["## gpu jobs awaiting collection", "",
             "These GPU jobs ended after the session that started them. Their declared outputs are not in the"
             " workspace until you call gpu_collect with the job_id; do that before starting new GPU work.", ""]
    for job in pending:
        label = f" {job.label!r}" if job.label else ""
        lines.append(f"- {job.job_id}{label}: status {job.status}, declared outputs: "
                     + ", ".join(job.outputs_declared))
    return "\n".join(lines)


def status_report(ws: Path, state_dir: Optional[Path], config: Optional[Dict[str, Any]], guard: Any,
                  box: Optional[Box], job_id: Optional[str] = None) -> Dict[str, Any]:
    """gpu_status: never starts the box."""

    merged = {**DEFAULTS, **(config or {})}
    store = JobStore(ws)
    used = spend.hours_used(ws)
    report: Dict[str, Any] = {
        "enabled": merged.get("enabled") is True,
        "hours": {"used": used, "max": float(merged["max_hours"]), "remaining": spend.hours_remaining(ws, merged)},
        "hourly_usd": float(merged["hourly_usd"]),
        "aws_gpu_allowed": _gpu_allowed(guard, 0.0) if guard is not None else None,
        "fleet_lock": lock.read_fleet_lock(state_dir) is not None if state_dir is not None else False,
        "workspace_lock": lock.read_workspace_lock(ws) is not None,
        "running": index.running_jobs(state_dir) if state_dir is not None else [],
        "jobs": [{"job_id": job.job_id, "status": job.status, "outcome": job.outcome, "reason": job.reason,
                  "label": job.label, "created_at": job.created_at, "hours_billed": job.hours_billed,
                  "usd": job.usd, "outputs": [o.get("path") for o in job.outputs],
                  "outputs_declared": list(job.outputs_declared)} for job in store.list()],
        "awaiting_collection": [job.job_id for job in pending_collection(ws)],
    }
    if box is None:
        report["box"] = None
    else:
        try:
            report["box"] = box.describe(cached_ok=True).as_dict()
        except Exception as exc:
            report["box"] = {"error": str(exc)}
    if job_id is not None:
        report["job"] = store.public(str(job_id))
    return report
