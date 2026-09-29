"""The GPU box controller: AWS lifecycle, ssh transport, rsync, and fleet state.

Why one class with an injected `runner`: every step that costs money or reaches
the network is an argv list (`aws`, `ssh`, `rsync`, `ssh-keyscan`,
`ssh-keygen`) passed to `runner`, so the whole lifecycle is testable offline
by scripting `subprocess.CompletedProcess` values, the same way
`harness.container.Docker` and `harness/aws/aws_cost.py` are tested.

The controller keeps its truth in `<state>/gpu-box.json`: the instance, its
address, when the harness started it (`started_at`, the clock that bills the
run), and which jobs are active with their owning pid. Every mutating method
holds `<state>/gpu-box.lock` (flock, so `adv-harness gpu stop` in another
process is excluded) plus a process-level lock (sessions run in threads).

Money rules live here and nowhere else: nothing starts before the budget
check passes, a box that nobody can reach is stopped, a running job is never
killed by a budget flip, and every stop appends a `box_run` row so the AWS
pot sees the hours a day before Cost Explorer does.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from adv_loop.errors import ValidationError
from adv_loop.pathsafe import ensure_within, safe_relative
from adv_loop.storage import atomic_write_text

from .. import budget
from . import BOX_NAME, DEFAULTS, GPU_SPEND_FILE, INSTANCE_TYPE, MIRROR_ROOT, STATE_BOX_FILE, STATE_SPEND_FILE
from .box_api import AGENT_BUSY_EXIT, BoxInfo, GpuBoxError

STATE_LOCK_FILE = "gpu-box.lock"
KEY_FILE = "gpu-box.key"
KNOWN_HOSTS_FILE = "gpu-box.known_hosts"
PUSH_EXCLUDE = Path(__file__).resolve().parent / "push.exclude"
REMOTE_REPO = "/srv/adv-loop/repo"
BOOTSTRAP_SCRIPT = "harness/aws/gpu-box-bootstrap.sh"
BOOTSTRAP_PATHS = (BOOTSTRAP_SCRIPT, "harness/gpu/agent", "harness/container")
AGENT = "adv-gpu-agent"

REFUSED_ALWAYS = (".checker-key", ".harness/host", ".harness/sessions", ".harness/validate")
REFUSED_PULL = ("events.jsonl", ".pending-event.json", "state.json", "attempts.jsonl", "evidence.jsonl",
                "task.md", "decision-log.md", "report.md", "loop-config.json")

DESCRIBE_QUERY = ("Reservations[0].Instances[0].{state:State.Name,private_ip:PrivateIpAddress,"
                  "public_ip:PublicIpAddress,launch_time:LaunchTime,az:Placement.AvailabilityZone,"
                  "type:InstanceType,ami:ImageId}")
DISCOVER_QUERY = "Reservations[].Instances[].InstanceId"
AWS_ERROR = re.compile(r"An error occurred \(([\w.]+)\)")
ERROR_KINDS = {
    "InsufficientInstanceCapacity": "capacity",
    "Unsupported": "capacity",
    "VcpuLimitExceeded": "quota",
    "InstanceLimitExceeded": "quota",
    "IncorrectInstanceState": "incorrect_state",
    "InvalidInstanceID.NotFound": "not_found",
}
HOSTKEY_MARKERS = ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed")
DEAD_STATES = ("terminated", "shutting-down")
SSH_CONNECTION_EXIT = 255

DESCRIBE_CACHE_SECONDS = 60
STOPPING_FORCE_SECONDS = 600
READY_POLL_SECONDS = 10
STOPPING_WAIT_SECONDS = 600
STATE_POLL_SECONDS = 10
MIN_DISK_FREE_GB = 10
RECYCLE_MARGIN_SECONDS = 900
WAIT_MAX_FAILURES = 10
COST_EXPLORER_LAG_SECONDS = 86400

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def tail(text: Optional[str], limit: int = 500) -> str:
    return (text or "").strip()[-limit:]


def pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def aws_error_kind(stderr: Optional[str]) -> str:
    match = AWS_ERROR.search(stderr or "")
    return ERROR_KINDS.get(match.group(1) if match else "", "aws_error")


def agent_missing(done: "subprocess.CompletedProcess[str]") -> bool:
    """ssh answered but the box has no agent yet (command not found)."""

    text = (done.stderr or "") + (done.stdout or "")
    return done.returncode == 127 or "command not found" in text or "No such file" in text


def hostkey_mismatch(done: "subprocess.CompletedProcess[str]") -> bool:
    return done.returncode == SSH_CONNECTION_EXIT and any(m in (done.stderr or "") for m in HOSTKEY_MARKERS)


def tree_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


@dataclass
class GpuBoxConfig:
    instance_id: Optional[str] = None
    region: str = "us-east-2"
    ssh_user: str = "ubuntu"
    idle_minutes: int = DEFAULTS["idle_minutes"]
    rate_usd_per_hour: float = DEFAULTS["hourly_usd"]
    image: str = DEFAULTS["image"]
    start_max_wait_seconds: int = DEFAULTS["start_max_wait_seconds"]
    spend_max_age_hours: float = 12.0
    max_uptime_minutes: int = 720
    mirror_root: str = MIRROR_ROOT
    vpc_cidr: str = "172.31.0.0/16"
    ssh_ready_seconds: int = DEFAULTS["ssh_ready_seconds"]
    box_name: str = BOX_NAME

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "GpuBoxConfig":
        source = os.environ if env is None else env

        def pick(key: str, cast: Callable[[str], Any], default: Any) -> Any:
            raw = source.get(key)
            if raw is None or raw == "":
                return default
            try:
                return cast(raw)
            except ValueError:
                return default

        return cls(
            instance_id=source.get("ADV_LOOP_GPU_BOX") or None,
            region=source.get("AWS_DEFAULT_REGION") or "us-east-2",
            ssh_user=source.get("ADV_LOOP_GPU_SSH_USER") or "ubuntu",
            idle_minutes=pick("ADV_LOOP_GPU_IDLE_MINUTES", int, DEFAULTS["idle_minutes"]),
            rate_usd_per_hour=pick("ADV_LOOP_GPU_RATE_USD", float, DEFAULTS["hourly_usd"]),
            image=source.get("ADV_LOOP_GPU_IMAGE") or DEFAULTS["image"],
            start_max_wait_seconds=pick("ADV_LOOP_GPU_START_MAX_WAIT_SECONDS", int, DEFAULTS["start_max_wait_seconds"]),
            spend_max_age_hours=pick("ADV_LOOP_GPU_SPEND_MAX_AGE_HOURS", float, 12.0),
            max_uptime_minutes=pick("ADV_LOOP_GPU_MAX_UPTIME_MINUTES", int, 720),
            mirror_root=source.get("ADV_LOOP_GPU_MIRROR_ROOT") or MIRROR_ROOT,
            vpc_cidr=source.get("ADV_LOOP_GPU_VPC_CIDR") or "172.31.0.0/16",
        )


class GpuBox:
    """The production `box_api.Box`; see the module docstring for the rules it enforces."""

    lock_timeout_seconds = 3600.0
    short_lock_timeout_seconds = 5.0
    stop_lock_timeout_seconds = 60.0

    def __init__(self, state_dir: Path, *, config: Optional[GpuBoxConfig] = None, runner: Runner = subprocess.run,
                 sleep: Callable[[float], Any] = time.sleep, clock: Callable[[], float] = time.time,
                 guard: Optional[budget.AwsSpendGuard] = None, cost_refresh: Optional[Callable[[], Any]] = None,
                 repo: Optional[Path] = None) -> None:
        self.state_dir = Path(state_dir)
        self.config = config or GpuBoxConfig.from_env()
        self.runner = runner
        self.sleep = sleep
        self.clock = clock
        self.guard = guard
        self.cost_refresh = cost_refresh
        self.repo = Path(repo).resolve() if repo else None
        self.state_file = self.state_dir / STATE_BOX_FILE
        self.key_file = self.state_dir / KEY_FILE
        self.known_hosts = self.state_dir / KNOWN_HOSTS_FILE
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_fd: Optional[int] = None

    # ----- state and locking -------------------------------------------------------------------

    def _state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("active_jobs"), dict):
            data["active_jobs"] = {}
        data["jobs_running"] = len(data["active_jobs"])
        return data

    def _write_state(self, state: Dict[str, Any]) -> None:
        state["jobs_running"] = len(state.get("active_jobs") or {})
        state["updated_at"] = utc(self.clock())
        atomic_write_text(self.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")

    @contextlib.contextmanager
    def _locked(self, timeout: Optional[float] = None) -> Iterator[None]:
        with self._thread_lock:
            if self._lock_depth == 0:
                self._acquire_flock(self.lock_timeout_seconds if timeout is None else timeout)
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    self._release_flock()

    def _acquire_flock(self, timeout: float) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.state_dir / STATE_LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise GpuBoxError("locked", "another process holds the GPU box state lock", retryable=True)
                time.sleep(0.2)
                continue
            self._lock_fd = fd
            return

    def _release_flock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ----- aws ---------------------------------------------------------------------------------

    def _aws(self, *args: str, timeout: float = 120.0) -> "subprocess.CompletedProcess[str]":
        argv = ["aws", "ec2", *args, "--region", self.config.region, "--output", "json"]
        try:
            return self.runner(argv, capture_output=True, text=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GpuBoxError("aws_error", f"aws {args[0]} timed out after {timeout:.0f}s", retryable=True) from exc

    def _instance_id(self, state: Optional[Dict[str, Any]] = None, *, discover: bool = True) -> Optional[str]:
        if self.config.instance_id:
            return self.config.instance_id
        state = self._state() if state is None else state
        if state.get("instance_id"):
            return str(state["instance_id"])
        return self._discover(state) if discover else None

    def _discover(self, state: Dict[str, Any]) -> Optional[str]:
        """Find the box by its tags once and remember the id; the instance survives stop/start cycles."""

        done = self._aws("describe-instances", "--filters",
                         f"Name=tag:Name,Values={self.config.box_name}", "Name=tag:adv-loop,Values=gpu",
                         "Name=instance-state-name,Values=pending,running,stopping,stopped",
                         "--query", DISCOVER_QUERY)
        if done.returncode != 0:
            raise GpuBoxError(aws_error_kind(done.stderr), tail(done.stderr))
        try:
            ids = json.loads(done.stdout or "[]")
        except ValueError:
            ids = []
        ids = sorted(i for i in ids if isinstance(i, str) and i) if isinstance(ids, list) else []
        if not ids:
            return None
        state["instance_id"] = ids[0]
        state["discovered_at"] = utc(self.clock())
        self._write_state(state)
        return ids[0]

    def _require_instance(self, state: Dict[str, Any]) -> str:
        instance_id = self._instance_id(state)
        if not instance_id:
            raise GpuBoxError("not_configured", "no GPU box: set ADV_LOOP_GPU_BOX or tag an instance adv-loop=gpu")
        state["instance_id"] = instance_id
        return instance_id

    # ----- describe ----------------------------------------------------------------------------

    def describe(self, *, cached_ok: bool = False) -> BoxInfo:
        with self._locked():
            return self._describe_locked(cached_ok=cached_ok)

    def _describe_locked(self, cached_ok: bool = False) -> BoxInfo:
        state = self._state()
        instance_id = self._require_instance(state)
        if cached_ok and state.get("state"):
            checked = budget.parse_utc(state.get("checked_at"))
            if checked is not None and self.clock() - checked < DESCRIBE_CACHE_SECONDS:
                return self._info(state)
        done = self._aws("describe-instances", "--instance-ids", instance_id, "--query", DESCRIBE_QUERY)
        if done.returncode != 0:
            kind = aws_error_kind(done.stderr)
            state["last_error"] = tail(done.stderr)
            if kind == "not_found":
                state["state"] = "not_found"
                state["ready"] = False
            self._write_state(state)
            raise GpuBoxError(kind, tail(done.stderr), retryable=kind == "aws_error")
        try:
            data = json.loads(done.stdout or "null")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            state["state"] = "not_found"
            state["ready"] = False
            self._write_state(state)
            raise GpuBoxError("not_found", f"{instance_id} has no reservation")
        self._observe(state, data)
        self._write_state(state)
        return self._info(state)

    def _observe(self, state: Dict[str, Any], data: Dict[str, Any]) -> None:
        now = self.clock()
        aws_state = str(data.get("state") or "unknown")
        previous = state.get("state")
        state.update({
            "state": aws_state,
            "az": data.get("az"),
            "private_ip": data.get("private_ip"),
            "public_ip": data.get("public_ip"),
            "instance_type": data.get("type") or state.get("instance_type"),
            "ami_id": data.get("ami") or state.get("ami_id"),
            "checked_at": utc(now),
        })
        if data.get("launch_time"):
            state["launch_time"] = data["launch_time"]
        if aws_state == "stopping":
            state.setdefault("stopping_since", utc(now))
        else:
            state.pop("stopping_since", None)
        if aws_state != "running":
            state["ready"] = False
        # A box we believed up that is now going down was stopped by the watchdog, the alarm,
        # the budget action, or an operator: bill it to the last observation.
        going_down = aws_state in ("stopping", "stopped") + DEAD_STATES and previous in ("running", "stopping")
        if going_down and state.get("started_at"):
            self._record_run(state, reason="external_stop", stopped_at=now)
        if aws_state == "running" and not state.get("started_at"):
            state["started_at"] = state.get("launch_time") or utc(now)

    def _uptime_reference(self, state: Dict[str, Any]) -> Optional[float]:
        started = budget.parse_utc(state.get("started_at"))
        launch = budget.parse_utc(state.get("launch_time"))
        # LaunchTime moves on every start; an older value belongs to a previous run and is ignored.
        if started is not None and launch is not None and launch >= started - 600:
            return min(started, launch)
        return started if started is not None else launch

    def _info(self, state: Dict[str, Any]) -> BoxInfo:
        uptime = None
        if state.get("state") == "running":
            reference = self._uptime_reference(state)
            if reference is not None:
                uptime = int(max(0.0, self.clock() - reference))
        active = state.get("active_jobs") or {}
        return BoxInfo(
            instance_id=str(state.get("instance_id") or ""),
            state=str(state.get("state") or "unknown"),
            instance_type=str(state.get("instance_type") or INSTANCE_TYPE),
            az=state.get("az"),
            private_ip=state.get("private_ip"),
            public_ip=state.get("public_ip"),
            ami_id=state.get("ami_id"),
            launch_time=state.get("launch_time"),
            started_at=state.get("started_at"),
            ready=bool(state.get("ready")),
            bootstrapped=bool(state.get("bootstrap_hash")),
            jobs_running=len(active),
            last_job_at=state.get("last_job_at"),
            uptime_seconds=uptime,
            mirror_root=self.config.mirror_root,
        )

    # ----- lifecycle ---------------------------------------------------------------------------

    def ensure_running(self, *, need_seconds: int = 0) -> BoxInfo:
        with self._locked():
            state = self._state()
            self._require_instance(state)
            self._budget_check(need_seconds)
            info = self._describe_locked()
            if info.state in DEAD_STATES:
                raise GpuBoxError("not_found", f"{info.instance_id} is {info.state}")
            if info.state == "stopping":
                info = self._wait_for(("stopped",), STOPPING_WAIT_SECONDS, failure_kind="launch_failed")
            if info.state == "pending":
                info = self._wait_for(("running", "stopped"), self.config.start_max_wait_seconds)
            if info.state == "running" and self._needs_recycle(info, need_seconds):
                self._stop_now_locked("recycle")
                info = self._wait_for(("stopped",), STOPPING_WAIT_SECONDS, failure_kind="launch_failed")
            if info.state == "stopped":
                self._start_locked()
                info = self._wait_for(("running",), self.config.start_max_wait_seconds)
            if info.state != "running":
                raise GpuBoxError("launch_failed", f"box is {info.state}")
            return self._await_ready_locked(info)

    def _needs_recycle(self, info: BoxInfo, need_seconds: int) -> bool:
        if info.uptime_seconds is None:
            return False
        return info.uptime_seconds + need_seconds > self.config.max_uptime_minutes * 60 - RECYCLE_MARGIN_SECONDS

    def _budget_check(self, need_seconds: int) -> None:
        if self.guard is None:
            return
        max_age = self.config.spend_max_age_hours * 3600
        if self.guard.stale(max_age):
            if self.cost_refresh is None:
                raise GpuBoxError("budget_unknown", "aws spend state is stale and no refresh is configured")
            try:
                self.cost_refresh()
            except Exception as exc:
                raise GpuBoxError("budget_unknown", f"aws spend refresh failed: {exc}") from exc
            if self.guard.stale(max_age):
                raise GpuBoxError("budget_unknown", "aws spend state is still stale after a refresh")
        projected = need_seconds / 3600.0 * self.config.rate_usd_per_hour
        unreported = self.unreported_usd()
        if not self.guard.allows_gpu(projected_usd=projected, unreported_usd=unreported):
            state = self.guard.state()
            raise GpuBoxError("budget_stop", "the AWS pot does not cover this job", details={
                "aws_spend_usd": state.get("aws_spend_usd"), "gpu_stop_usd": state.get("gpu_stop_usd"),
                "projected_usd": round(projected, 4), "unreported_usd": round(unreported, 4)})

    def unreported_usd(self) -> float:
        """Box hours Cost Explorer has not shown yet: runs since the last refresh minus its lag, plus the live box."""

        since = None
        if self.guard is not None:
            at = budget.parse_utc(self.guard.state().get("at"))
            if at is not None:
                since = at - COST_EXPLORER_LAG_SECONDS
        total = 0.0
        for row in budget.gpu_spend_rows(self.state_dir / STATE_SPEND_FILE, "box_run"):
            stopped = budget.parse_utc(row.get("stopped_at") or row.get("at"))
            if since is not None and stopped is not None and stopped < since:
                continue
            usd = row.get("usd")
            if isinstance(usd, (int, float)) and not isinstance(usd, bool):
                total += float(usd)
        state = self._state()
        if state.get("state") == "running":
            started = budget.parse_utc(state.get("started_at"))
            if started is not None:
                total += max(0.0, self.clock() - started) / 3600.0 * self.config.rate_usd_per_hour
        return round(total, 4)

    def _wait_for(self, states: Sequence[str], max_seconds: float, *, failure_kind: str = "launch_failed") -> BoxInfo:
        deadline = self.clock() + max_seconds
        while True:
            info = self._describe_locked()
            if info.state in states:
                return info
            if info.state in DEAD_STATES:
                raise GpuBoxError("not_found", f"{info.instance_id} is {info.state}")
            if self.clock() >= deadline:
                raise GpuBoxError(failure_kind, f"box stayed {info.state} for {max_seconds:.0f}s", retryable=True)
            self.sleep(STATE_POLL_SECONDS)

    def _start_locked(self) -> None:
        state = self._state()
        instance_id = state["instance_id"]
        deadline = self.clock() + self.config.start_max_wait_seconds
        delay = 30
        while True:
            done = self._aws("start-instances", "--instance-ids", instance_id)
            if done.returncode == 0:
                break
            kind = aws_error_kind(done.stderr)
            message = tail(done.stderr)
            state["last_error"] = message
            self._write_state(state)
            if kind == "capacity":
                if self.clock() + delay > deadline:
                    raise GpuBoxError("capacity", message, retryable=True)
                self.sleep(delay)
                delay = 60
                continue
            if kind == "incorrect_state":
                if self.clock() + STATE_POLL_SECONDS > deadline:
                    raise GpuBoxError("launch_failed", message, retryable=True)
                self.sleep(STATE_POLL_SECONDS)
                if self._describe_locked().state == "running":
                    return
                state = self._state()
                continue
            raise GpuBoxError(kind if kind in ("quota", "not_found") else "aws_error", message)
        state = self._state()
        state.update({"started_at": utc(self.clock()), "state": "pending", "ready": False,
                      "last_error": None, "stop_failures": 0})
        self._write_state(state)

    # ----- readiness ---------------------------------------------------------------------------

    def _await_ready_locked(self, info: BoxInfo) -> BoxInfo:
        ip = info.private_ip
        if not ip:
            raise GpuBoxError("launch_failed", "the running box reports no private address")
        deadline = self.clock() + self.config.ssh_ready_seconds
        if not self._host_known(ip):
            self._keyscan_locked(ip, deadline)
        ready: Optional[Dict[str, Any]] = None
        while ready is None:
            try:
                done = self.exec_remote([AGENT, "ready"], timeout=60)
            except subprocess.TimeoutExpired:
                done = None
            if done is not None:
                if hostkey_mismatch(done):
                    raise GpuBoxError("hostkey_mismatch", "the box's host key changed; never auto-accepted. Check "
                                      "`aws ec2 get-console-output`, then run `adv-harness gpu trust --reset`",
                                      details={"stderr": tail(done.stderr)})
                if done.returncode == 0:
                    ready = self._json(done.stdout)
                elif agent_missing(done):
                    # A fresh box answers ssh but has no agent yet: install it, then keep probing.
                    self._bootstrap_locked(force=False)
                    continue
            if ready is None:
                if self.clock() >= deadline:
                    self._stop_now_locked("ssh_unreachable")
                    raise GpuBoxError("ssh_unreachable", f"no answer from {AGENT} within "
                                      f"{self.config.ssh_ready_seconds}s; the box was stopped", retryable=True)
                self.sleep(READY_POLL_SECONDS)
        disk = ready.get("disk_free_gb")
        if isinstance(disk, (int, float)) and not isinstance(disk, bool) and disk < MIN_DISK_FREE_GB:
            raise GpuBoxError("disk_full", f"{disk} GB free on the box, {MIN_DISK_FREE_GB} required",
                              details={"disk_free_gb": disk})
        expected = self.bootstrap_hash() if self.repo is not None else None
        if not ready.get("image_present") or (expected is not None and ready.get("bootstrap_hash") != expected):
            self._bootstrap_locked(force=False)
            try:
                done = self.exec_remote([AGENT, "ready"], timeout=60)
            except subprocess.TimeoutExpired:
                done = None
            ready = self._json(done.stdout) if done is not None and done.returncode == 0 else None
            if not ready or not ready.get("image_present"):
                self._stop_now_locked("bootstrap_failed")
                raise GpuBoxError("bootstrap_failed", "the image is still missing after bootstrap")
        state = self._state()
        state.update({"ready": True, "ready_at": utc(self.clock()), "bootstrap_hash": ready.get("bootstrap_hash"),
                      "image": self.config.image, "box_uptime_s": ready.get("uptime_s")})
        self._write_state(state)
        return self._info(state)

    @staticmethod
    def _json(text: Optional[str]) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(text or "")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _host_known(self, ip: str) -> bool:
        try:
            lines = self.known_hosts.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            fields = line.split()
            if fields and not fields[0].startswith("#") and ip in fields[0].split(","):
                return True
        return False

    def _keyscan_locked(self, ip: str, deadline: float) -> int:
        while True:
            done = self.runner(["ssh-keyscan", "-T", "10", "-t", "ed25519", ip],
                               capture_output=True, text=True, check=False, timeout=30)
            lines = [line for line in (done.stdout or "").splitlines() if line.strip() and not line.startswith("#")]
            if lines:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                with open(self.known_hosts, "a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                os.chmod(self.known_hosts, 0o600)
                return len(lines)
            if self.clock() >= deadline:
                self._stop_now_locked("ssh_unreachable")
                raise GpuBoxError("ssh_unreachable", "ssh-keyscan got no host key; the box was stopped", retryable=True)
            self.sleep(READY_POLL_SECONDS)

    # ----- transport ---------------------------------------------------------------------------

    def _ssh_options(self) -> List[str]:
        return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=6", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "IdentitiesOnly=yes", "-i", str(self.key_file),
                "-o", "LogLevel=ERROR"]

    def _private_ip(self) -> str:
        ip = self._state().get("private_ip")
        if not ip:
            ip = self.describe().private_ip
        if not ip:
            raise GpuBoxError("not_running", "the GPU box has no private address; is it running?")
        return str(ip)

    @staticmethod
    def remote_command(argv: Sequence[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> str:
        parts = []
        if cwd:
            parts.append(f"cd {shlex.quote(str(cwd))}")
        assignments = " ".join(f"{key}={shlex.quote(str(value))}" for key, value in sorted((env or {}).items()))
        parts.append("exec env " + (assignments + " " if assignments else "") + shlex.join([str(a) for a in argv]))
        return " && ".join(parts)

    def exec_remote(self, argv: Sequence[str], cwd: Optional[str] = None, timeout: float = 600.0,
                    env: Optional[Dict[str, str]] = None, stdin: Optional[str] = None
                    ) -> "subprocess.CompletedProcess[str]":
        ip = self._private_ip()
        ssh_argv = ["ssh", *self._ssh_options(), f"{self.config.ssh_user}@{ip}", "--",
                    self.remote_command(argv, cwd, env)]
        return self.runner(ssh_argv, capture_output=True, text=True, timeout=timeout, input=stdin, check=False)

    def _remote_spec(self, path: str) -> str:
        return f"{self.config.ssh_user}@{self._private_ip()}:{shlex.quote(path)}"

    def _exclude_file(self) -> Path:
        if self.repo is not None:
            candidate = self.repo / "harness" / "gpu" / "push.exclude"
            if candidate.is_file():
                return candidate
        return PUSH_EXCLUDE

    def _rsync(self, src: str, dst: str, *, delete: bool, kind: str) -> "subprocess.CompletedProcess[str]":
        argv = ["rsync", "-a", "--partial", "--timeout=120"]
        if delete:
            argv.append("--delete")
        argv += [f"--exclude-from={self._exclude_file()}", "-e", "ssh " + shlex.join(self._ssh_options()), src, dst]
        done = self.runner(argv, capture_output=True, text=True, check=False, timeout=None)
        if done.returncode != 0:
            raise GpuBoxError(kind, f"rsync exit {done.returncode}: {tail(done.stderr or done.stdout)}",
                              retryable=True, details={"src": src, "dst": dst})
        return done

    def _contained(self, workspace: Path, raw: str, *, pull: bool) -> PurePosixPath:
        try:
            rel = safe_relative(raw)
        except ValidationError as exc:
            raise GpuBoxError("path_refused", str(exc), details={"path": raw}) from exc
        posix = rel.as_posix()
        for item in REFUSED_ALWAYS + (REFUSED_PULL if pull else ()):
            if posix == item or posix.startswith(item + "/") or item.startswith(posix + "/"):
                raise GpuBoxError("path_refused", f"{raw} is host-only and never crosses to the box",
                                  details={"path": raw, "rule": item})
        try:
            ensure_within(workspace, workspace.joinpath(*rel.parts))
        except ValidationError as exc:
            raise GpuBoxError("path_refused", str(exc), details={"path": raw}) from exc
        return rel

    def _heartbeat(self) -> None:
        try:
            self.exec_remote([AGENT, "heartbeat"], timeout=30)
        except Exception:
            pass

    def push(self, paths: Sequence[str], dest: str, *, workspace: Path, delete: bool = False) -> Dict[str, Any]:
        workspace = Path(workspace).resolve()
        pushed: List[str] = []
        total = 0
        for raw in paths:
            rel = self._contained(workspace, str(raw), pull=False)
            local = workspace.joinpath(*rel.parts)
            if not local.exists():
                raise GpuBoxError("push_failed", f"declared input {rel} does not exist", details={"path": str(rel)})
            remote = posixpath.join(dest, rel.as_posix())
            made = self.exec_remote(["mkdir", "-p", posixpath.dirname(remote)], timeout=60)
            if made.returncode != 0:
                raise GpuBoxError("push_failed", f"mkdir on the box failed: {tail(made.stderr)}", retryable=True)
            slash = "/" if local.is_dir() else ""
            self._rsync(str(local) + slash, self._remote_spec(remote) + slash, delete=delete, kind="push_failed")
            total += tree_bytes(local)
            pushed.append(rel.as_posix())
        self._heartbeat()
        return {"pushed": pushed, "bytes": total}

    def pull(self, paths: Sequence[str], src: str, *, workspace: Path) -> Dict[str, Any]:
        workspace = Path(workspace).resolve()
        pulled: List[str] = []
        total = 0
        for raw in paths:
            rel = self._contained(workspace, str(raw), pull=True)
            local = workspace.joinpath(*rel.parts)
            local.parent.mkdir(parents=True, exist_ok=True)
            # The remote item lands inside its local parent, so a file and a directory need no
            # trailing-slash distinction and an existing directory merges instead of nesting.
            self._rsync(self._remote_spec(posixpath.join(src, rel.as_posix())), str(local.parent) + "/",
                        delete=False, kind="pull_failed")
            if local.exists():
                total += tree_bytes(local)
            pulled.append(rel.as_posix())
        self._heartbeat()
        return {"pulled": pulled, "bytes": total}

    # ----- jobs --------------------------------------------------------------------------------

    def _agent(self, args: Sequence[str], *, timeout: float) -> "subprocess.CompletedProcess[str]":
        return self.exec_remote([AGENT, *args], timeout=timeout)

    def _agent_json(self, done: "subprocess.CompletedProcess[str]", kind: str) -> Dict[str, Any]:
        data = self._json(done.stdout)
        if data is None:
            raise GpuBoxError(kind, f"{AGENT} printed no JSON: {tail(done.stdout or done.stderr)}")
        return data

    def _agent_result(self, done: "subprocess.CompletedProcess[str]", kind: str) -> Dict[str, Any]:
        if done.returncode == SSH_CONNECTION_EXIT:
            if hostkey_mismatch(done):
                raise GpuBoxError("hostkey_mismatch", tail(done.stderr))
            raise GpuBoxError("box_unreachable", tail(done.stderr), retryable=True)
        if done.returncode != 0:
            raise GpuBoxError(kind, tail(done.stderr or done.stdout), details={"returncode": done.returncode})
        return self._agent_json(done, kind)

    def submit(self, job_dir: str) -> Dict[str, Any]:
        done = self._agent(["submit", job_dir], timeout=90)
        if done.returncode == AGENT_BUSY_EXIT:
            raise GpuBoxError("busy", "the box is running another job", retryable=True,
                              details=self._json(done.stdout) or {})
        return self._agent_result(done, "submit_failed")

    def wait(self, job_dir: str, *, max_seconds: float) -> Dict[str, Any]:
        failures = 0
        while True:
            try:
                done = self._agent(["wait", job_dir, "--max", str(int(max_seconds))], timeout=float(max_seconds) + 30)
            except subprocess.TimeoutExpired:
                done = None
            if done is not None:
                if done.returncode in (0, 3):
                    return self._agent_json(done, "agent_error")
                if hostkey_mismatch(done):
                    raise GpuBoxError("hostkey_mismatch", tail(done.stderr))
                if done.returncode != SSH_CONNECTION_EXIT:
                    raise GpuBoxError("agent_error", tail(done.stderr or done.stdout),
                                      details={"returncode": done.returncode})
            info = self.describe()
            if info.state != "running":
                return {"state": "interrupted", "box_state": info.state, "job_dir": job_dir}
            failures += 1
            self._count_reconnect(job_dir)
            if failures >= WAIT_MAX_FAILURES:
                raise GpuBoxError("box_unreachable", f"{failures} consecutive ssh failures while waiting",
                                  retryable=True)
            self.sleep(min(30, 5 * failures))

    def _count_reconnect(self, job_dir: str) -> None:
        job_id = posixpath.basename(job_dir.rstrip("/"))
        with self._locked(timeout=self.short_lock_timeout_seconds):
            state = self._state()
            record = state["active_jobs"].get(job_id)
            if record is not None:
                record["reconnects"] = int(record.get("reconnects") or 0) + 1
                self._write_state(state)

    def status(self, job_dir: str) -> Dict[str, Any]:
        return self._agent_result(self._agent(["status", job_dir], timeout=60), "agent_error")

    def kill(self, job_dir: str) -> Dict[str, Any]:
        return self._agent_result(self._agent(["kill", job_dir], timeout=180), "agent_error")

    def register_job(self, job_id: str, *, workspace: str, session_id: str, timeout_seconds: int) -> None:
        with self._locked():
            state = self._state()
            now = utc(self.clock())
            state["active_jobs"][job_id] = {
                "workspace": str(workspace), "session_id": session_id, "pid": os.getpid(), "started_at": now,
                "timeout_seconds": int(timeout_seconds), "reconnects": 0,
            }
            state["last_job_at"] = now
            self._write_state(state)

    def release_job(self, job_id: str) -> None:
        with self._locked():
            state = self._state()
            state["active_jobs"].pop(job_id, None)
            state["last_job_at"] = utc(self.clock())
            self._write_state(state)

    def active_jobs(self) -> List[Dict[str, Any]]:
        active = self._state().get("active_jobs") or {}
        return [{"job_id": job_id, **record} for job_id, record in sorted(active.items())]

    # ----- stopping ----------------------------------------------------------------------------

    def stop_if_idle(self, idle_minutes: Optional[int] = None) -> Dict[str, Any]:
        minutes = self.config.idle_minutes if idle_minutes is None else int(idle_minutes)
        try:
            with self._locked(timeout=self.short_lock_timeout_seconds):
                return self._stop_if_idle_locked(minutes)
        except GpuBoxError as exc:
            if exc.kind == "locked":
                return {"action": "skipped", "reason": "locked"}
            raise

    def _stop_if_idle_locked(self, minutes: int) -> Dict[str, Any]:
        state = self._state()
        if not self._instance_id(state, discover=False):
            return {"action": "none", "reason": "not_configured"}
        checked = budget.parse_utc(state.get("checked_at"))
        if state.get("state") and checked is not None and self.clock() - checked < DESCRIBE_CACHE_SECONDS:
            info = self._info(state)
        else:
            info = self._describe_locked()
            state = self._state()
        if info.state != "running":
            return {"action": "none", "reason": f"box is {info.state}", "state": info.state}
        active = state.get("active_jobs") or {}
        if active:
            return {"action": "none", "reason": "jobs_running", "jobs": sorted(active)}
        marks = [budget.parse_utc(state.get(key)) for key in ("last_job_at", "started_at")]
        marks = [mark for mark in marks if mark is not None]
        if not marks:
            marks = [self._uptime_reference(state) or self.clock()]
        idle = self.clock() - max(marks)
        if idle < minutes * 60:
            return {"action": "none", "reason": "recent", "idle_seconds": int(idle), "idle_minutes": minutes}
        result = self._stop_now_locked("idle_stop")
        result["idle_seconds"] = int(idle)
        return result

    def stop_now(self, reason: str = "operator", *, kill_jobs: bool = True) -> Dict[str, Any]:
        with self._locked(timeout=self.stop_lock_timeout_seconds):
            return self._stop_now_locked(reason, kill_jobs=kill_jobs)

    def _stop_now_locked(self, reason: str, *, kill_jobs: bool = True) -> Dict[str, Any]:
        state = self._state()
        instance_id = self._instance_id(state, discover=False)
        if not instance_id:
            return {"action": "none", "reason": "not_configured", "stop_reason": reason}
        try:
            info = self._describe_locked()
        except GpuBoxError as exc:
            if exc.kind == "not_found":
                return {"action": "none", "reason": "not_found", "stop_reason": reason, "instance_id": instance_id}
            raise
        state = self._state()
        killed: List[str] = []
        if kill_jobs and state["active_jobs"] and info.state == "running" and info.private_ip:
            for job_id, record in sorted(state["active_jobs"].items()):
                job_dir = posixpath.join(str(record.get("workspace") or ""), ".harness/gpu/jobs", job_id)
                try:
                    self.kill(job_dir)
                    killed.append(job_id)
                except Exception:
                    continue
        stop = None
        if info.state not in ("stopped",) + DEAD_STATES:
            stop = self._stop_instances_locked(state)
        now = self.clock()
        row = self._record_run(state, reason=reason, stopped_at=now) if state.get("started_at") else None
        state.update({"state": "stopped", "ready": False, "checked_at": utc(now)})
        state.pop("stopping_since", None)
        self._write_state(state)
        return {"action": "stopped" if stop else "already_stopped", "reason": reason, "instance_id": instance_id,
                "killed": killed, "run": row, "stop": stop}

    def _stop_instances_locked(self, state: Dict[str, Any]) -> Dict[str, Any]:
        instance_id = state["instance_id"]
        stopping_since = budget.parse_utc(state.get("stopping_since"))
        force = stopping_since is not None and self.clock() - stopping_since >= STOPPING_FORCE_SECONDS
        attempts = 0
        while True:
            args = ["stop-instances", "--instance-ids", instance_id] + (["--force"] if force else [])
            done = self._aws(*args)
            attempts += 1
            if done.returncode == 0:
                state["stop_failures"] = 0
                state["last_error"] = None
                return {"forced": force, "attempts": attempts}
            kind = aws_error_kind(done.stderr)
            message = tail(done.stderr)
            state["last_error"] = message
            state["stop_failures"] = int(state.get("stop_failures") or 0) + 1
            if kind == "not_found":
                self._write_state(state)
                raise GpuBoxError("not_found", message)
            if force:
                self._write_state(state)
                raise GpuBoxError("stop_failed", message, retryable=True, details={"attempts": attempts})
            # --force only after two graceful failures: a forced stop skips the guest's clean shutdown.
            if attempts >= 2:
                force = True
                continue
            self.sleep(5)

    def _record_run(self, state: Dict[str, Any], *, reason: str, stopped_at: float) -> Dict[str, Any]:
        started_iso = state.get("started_at")
        launch_iso = state.get("launch_time")
        reference = self._uptime_reference(state)
        seconds = int(max(0.0, stopped_at - reference)) if reference is not None else 0
        rate = self.config.rate_usd_per_hour
        row = {
            "at": utc(stopped_at), "kind": "box_run", "instance_id": state.get("instance_id"),
            "started_at": started_iso or launch_iso, "stopped_at": utc(stopped_at), "seconds": seconds,
            "usd": round(seconds / 3600.0 * rate, 4), "rate_usd_per_hour": rate, "reason": reason,
            "jobs": sorted(state.get("active_jobs") or {}), "launch_time": launch_iso, "source": "harness_timer",
        }
        budget.record_gpu_spend(self.state_dir / STATE_SPEND_FILE, row)
        state["started_at"] = None
        state["stopped_at"] = utc(stopped_at)
        state["last_stop_reason"] = reason
        return row

    def reap(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"orphans": [], "idle": None, "error": None}
        try:
            with self._locked(timeout=self.short_lock_timeout_seconds):
                result["orphans"] = self._reap_orphans_locked()
                result["idle"] = self._stop_if_idle_locked(self.config.idle_minutes)
        except GpuBoxError as exc:
            if exc.kind == "locked":
                result["idle"] = {"action": "skipped", "reason": "locked"}
            else:
                result["error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    def _reap_orphans_locked(self) -> List[Dict[str, Any]]:
        state = self._state()
        active = state["active_jobs"]
        orphans: List[Dict[str, Any]] = []
        now = self.clock()
        for job_id in sorted(active):
            record = active[job_id]
            if pid_alive(record.get("pid")):
                continue
            workspace = str(record.get("workspace") or "")
            kill_result: Optional[Dict[str, Any]] = None
            if state.get("state") == "running" and state.get("private_ip"):
                try:
                    kill_result = self.kill(posixpath.join(workspace, ".harness/gpu/jobs", job_id))
                except Exception as exc:
                    kill_result = {"error": str(exc)}
            started = budget.parse_utc(record.get("started_at"))
            seconds = int(max(0.0, now - started)) if started is not None else 0
            timeout = record.get("timeout_seconds")
            if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
                seconds = min(seconds, timeout)
            row = {
                "kind": "job", "job_id": job_id, "session_id": record.get("session_id"), "workspace": workspace,
                "started_at": record.get("started_at"), "finished_at": utc(now), "seconds": seconds,
                "usd": round(seconds / 3600.0 * self.config.rate_usd_per_hour, 4), "state": "orphaned",
                "exit_code": None, "reason": "owner_pid_dead", "owner_pid": record.get("pid"),
                "instance_id": state.get("instance_id"), "source": "harness_reaper",
            }
            try:
                budget.record_gpu_spend(Path(workspace) / GPU_SPEND_FILE, row)
            except OSError as exc:
                row["ledger_error"] = str(exc)
            del active[job_id]
            orphans.append({"job_id": job_id, "pid": record.get("pid"), "kill": kill_result, "row": row})
        if orphans:
            state["last_job_at"] = utc(now)
            self._write_state(state)
        return orphans

    # ----- provisioning ------------------------------------------------------------------------

    def bootstrap_hash(self) -> str:
        """sha256 over the files bootstrap ships (sorted relative paths, name and bytes); None repo -> error."""

        if self.repo is None:
            raise GpuBoxError("bootstrap_failed", "no repo checkout configured (ADV_LOOP_REPO)")
        digest = hashlib.sha256()
        for base in BOOTSTRAP_PATHS:
            root = self.repo / base
            if root.is_file():
                files = [root]
            elif root.is_dir():
                files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                               and p.suffix != ".pyc")
            else:
                continue
            for path in files:
                rel = path.relative_to(self.repo).as_posix()
                digest.update(rel.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
        return digest.hexdigest()

    def _await_ssh_locked(self) -> None:
        """Block until the box answers a plain ssh command; a fresh instance refuses connections for a while."""

        info = self._describe_locked()
        if info.state != "running":
            raise GpuBoxError("not_running", f"the box is {info.state}; start it before bootstrapping")
        ip = info.private_ip
        if not ip:
            raise GpuBoxError("launch_failed", "the box reports no private address")
        deadline = self.clock() + self.config.ssh_ready_seconds
        if not self._host_known(ip):
            self._keyscan_locked(ip, deadline)
        while True:
            try:
                done = self.exec_remote(["true"], timeout=30)
            except subprocess.TimeoutExpired:
                done = None
            if done is not None and done.returncode == 0:
                return
            if done is not None and hostkey_mismatch(done):
                raise GpuBoxError("hostkey_mismatch", "the box's host key changed; never auto-accepted")
            if self.clock() >= deadline:
                raise GpuBoxError("ssh_unreachable", f"ssh did not answer within {self.config.ssh_ready_seconds}s",
                                  retryable=True)
            self.sleep(READY_POLL_SECONDS)

    def bootstrap(self, force: bool = False) -> Dict[str, Any]:
        with self._locked():
            return self._bootstrap_locked(force=force)

    def _bootstrap_locked(self, *, force: bool) -> Dict[str, Any]:
        if self.repo is None:
            self._stop_now_locked("bootstrap_failed")
            raise GpuBoxError("bootstrap_failed", "no repo checkout configured (ADV_LOOP_REPO); the box was stopped")
        paths = [base for base in BOOTSTRAP_PATHS if (self.repo / base).exists()]
        if BOOTSTRAP_SCRIPT not in paths:
            self._stop_now_locked("bootstrap_failed")
            raise GpuBoxError("bootstrap_failed", f"{BOOTSTRAP_SCRIPT} missing from {self.repo}")
        digest = self.bootstrap_hash()
        env = {"ADV_GPU_IMAGE": self.config.image, "ADV_GPU_BOOTSTRAP_HASH": digest}
        if force:
            env["ADV_GPU_FORCE_BUILD"] = "1"
        try:
            self._await_ssh_locked()
            self.push(paths, REMOTE_REPO, workspace=self.repo)
            # `sudo env K=V` because the DLAMI's sudo drops -E.
            argv = ["sudo", "env"] + [f"{k}={v}" for k, v in sorted(env.items())] + ["bash", f"{REMOTE_REPO}/{BOOTSTRAP_SCRIPT}"]
            done = self.exec_remote(argv, timeout=1800)
        except (GpuBoxError, subprocess.TimeoutExpired) as exc:
            self._stop_now_locked("bootstrap_failed")
            self._note_error(f"bootstrap: {exc}")
            raise GpuBoxError("bootstrap_failed", f"bootstrap did not complete: {exc}") from exc
        if done.returncode != 0:
            message = tail(done.stderr or done.stdout, 2000)
            self._stop_now_locked("bootstrap_failed")
            self._note_error(f"bootstrap exit {done.returncode}: {message}")
            raise GpuBoxError("bootstrap_failed", message, details={"returncode": done.returncode})
        summary = self._json((done.stdout or "").strip().splitlines()[-1] if done.stdout else "") or {}
        state = self._state()
        state.update({"bootstrap_hash": digest, "image": self.config.image,
                      "image_digest": summary.get("image_digest") or state.get("image_digest"),
                      "bootstrapped_at": utc(self.clock()), "last_error": None})
        self._write_state(state)
        return {"bootstrap_hash": digest, "image": self.config.image, "pushed": paths, "summary": summary}

    def _note_error(self, message: str) -> None:
        state = self._state()
        state["last_error"] = tail(message, 2000)
        self._write_state(state)

    def keygen(self) -> Path:
        pub = self.key_file.with_name(self.key_file.name + ".pub")
        with self._locked():
            if self.key_file.is_file() and pub.is_file():
                return pub
            self.state_dir.mkdir(parents=True, exist_ok=True)
            done = self.runner(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "adv-harness-box",
                                "-f", str(self.key_file)], capture_output=True, text=True, check=False, timeout=60)
            if done.returncode != 0 or not self.key_file.is_file():
                raise GpuBoxError("keygen_failed", tail(done.stderr or done.stdout))
            os.chmod(self.key_file, 0o600)
            return pub

    def trust(self, reset: bool = False) -> Dict[str, Any]:
        with self._locked():
            ip = self._describe_locked().private_ip
            if not ip:
                raise GpuBoxError("not_running", "the box has no private address to scan")
            if reset:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                self.known_hosts.write_text("", encoding="utf-8")
            entries = self._keyscan_locked(ip, self.clock() + self.config.ssh_ready_seconds)
            return {"known_hosts": str(self.known_hosts), "private_ip": ip, "entries": entries, "reset": reset}


# ----- module-level entry points for the CLI, systemd, and ad hoc use --------------------------

def state_dir_from(state: Optional[str] = None) -> Path:
    raw = state or os.environ.get("ADV_LOOP_STATE")
    if not raw:
        raise GpuBoxError("not_configured", "ADV_LOOP_STATE is unset and no state directory was given")
    return Path(raw)


def default_cost_refresh(state_dir: Path) -> Optional[Callable[[], Any]]:
    since = os.environ.get("ADV_LOOP_AWS_SINCE")
    if not since:
        return None

    def refresh() -> Any:
        from harness.aws import aws_cost  # lazy: the script imports the aws CLI wrapper, never needed offline

        return aws_cost.refresh(Path(state_dir) / "aws-spend.json", since, group_by_tag="adv-loop")

    return refresh


def open_box(state: Optional[str] = None, **kwargs: Any) -> GpuBox:
    state_dir = state_dir_from(state)
    if "repo" not in kwargs:
        kwargs["repo"] = os.environ.get("ADV_LOOP_REPO") or None
    if "guard" not in kwargs:
        kwargs["guard"] = budget.AwsSpendGuard(state_dir / "aws-spend.json")
    if "cost_refresh" not in kwargs:
        kwargs["cost_refresh"] = default_cost_refresh(state_dir)
    return GpuBox(state_dir, **kwargs)


def ensure_running(need_seconds: int = 0, *, state: Optional[str] = None, **kwargs: Any) -> BoxInfo:
    return open_box(state, **kwargs).ensure_running(need_seconds=need_seconds)


def stop_if_idle(idle_minutes: Optional[int] = None, *, state: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
    return open_box(state, **kwargs).stop_if_idle(idle_minutes)


def stop_now(reason: str = "operator", *, kill_jobs: bool = True, state: Optional[str] = None,
             **kwargs: Any) -> Dict[str, Any]:
    return open_box(state, **kwargs).stop_now(reason, kill_jobs=kill_jobs)


def exec_remote(argv: Sequence[str], *, state: Optional[str] = None, cwd: Optional[str] = None,
                timeout: float = 600.0, env: Optional[Dict[str, str]] = None, stdin: Optional[str] = None,
                **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    return open_box(state, **kwargs).exec_remote(argv, cwd=cwd, timeout=timeout, env=env, stdin=stdin)


def push(paths: Sequence[str], dest: str, *, workspace: Path, delete: bool = False, state: Optional[str] = None,
         **kwargs: Any) -> Dict[str, Any]:
    return open_box(state, **kwargs).push(paths, dest, workspace=workspace, delete=delete)


def pull(paths: Sequence[str], src: str, *, workspace: Path, state: Optional[str] = None,
         **kwargs: Any) -> Dict[str, Any]:
    return open_box(state, **kwargs).pull(paths, src, workspace=workspace)


def describe(state: Optional[str] = None, *, cached_ok: bool = False, **kwargs: Any) -> BoxInfo:
    return open_box(state, **kwargs).describe(cached_ok=cached_ok)
