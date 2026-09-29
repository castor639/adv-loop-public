"""A GPU box for tests: the Box protocol over a local directory.

Everything the runner asks of a box happens on this machine. push and pull
copy files between the workspace and a mirror of its absolute path under the
fake's remote root, submit runs the job's argv with subprocess in a thread
under the job's own timeout, and exec_remote answers the handful of probes
the harness sends. Knobs make every failure path deterministic.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.gpu.box_api import BoxInfo, GpuBoxError

PINS = "torch=2.13.0+cu130\ncuda=13.0\npython=3.12.3\nuv=0.8.4\n"
IMAGE_ID = "sha256:" + "ab" * 32
DRIVER = "580.65.06"
DRIFTED_DRIVER = "580.99.99"
GPU_NAME = "NVIDIA A10G"
TERMINAL = ("done", "failed", "timeout", "killed", "interrupted")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _tree_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _copy(source: Path, target: Path) -> None:
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copyfile(source, target)


class _FakeJob:
    def __init__(self, box: "FakeBox", job_dir: str) -> None:
        self.box = box
        self.dir = box.mirror(job_dir)
        self.spec = json.loads((self.dir / "job.json").read_text(encoding="utf-8"))
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.final: Optional[str] = None
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._files: List[Any] = []

    def _write_status(self, state: str, **extra: Any) -> Dict[str, Any]:
        status = {"state": state, "pid": self.proc.pid if self.proc else None,
                  "container": f"adv-gpu-{self.spec['job_id']}", "started_at": utc_now(), **extra}
        temp = self.dir / "status.json.tmp"
        temp.write_text(json.dumps(status, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.dir / "status.json")
        return status

    def start(self) -> Dict[str, Any]:
        workspace = self.box.mirror(self.spec["workspace"])
        cwd = workspace / self.spec.get("cwd", "payload/scratch")
        cwd.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "ADV_LOOP_WORKSPACE": str(workspace), "ADV_GPU_JOB_ID": self.spec["job_id"],
               **(self.spec.get("env") or {})}
        out = open(self.dir / "stdout.log", "wb")
        err = open(self.dir / "stderr.log", "wb")
        self._files = [out, err]
        self.proc = subprocess.Popen(self.spec["command"], cwd=str(cwd), env=env, stdout=out, stderr=err,
                                     start_new_session=True)
        status = self._write_status("running")
        self.thread = threading.Thread(target=self._watch, daemon=True, name=f"fake-gpu-{self.spec['job_id']}")
        self.thread.start()
        return status

    def _watch(self) -> None:
        timeout = None if self.box.ignore_timeout else float(self.spec["timeout_seconds"])
        try:
            code = self.proc.wait(timeout=timeout)
            state = "done" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            self._terminate()
            code = self.proc.wait()
            state = "timeout"
        with self._lock:
            if self.final is None:
                self.final = state
                self._write_status(state, exit_code=code, finished_at=utc_now(),
                                   seconds=int(time.monotonic() - self.started))
        for handle in self._files:
            handle.close()

    def _terminate(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def end(self, state: str) -> Dict[str, Any]:
        with self._lock:
            if self.final is None:
                self.final = state
                self._terminate()
                return self._write_status(state, exit_code=None, finished_at=utc_now(),
                                          seconds=int(time.monotonic() - self.started))
        return self.status()

    def status(self) -> Dict[str, Any]:
        try:
            return json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "missing"}

    def join(self, timeout: float = 30.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


class FakeBox:
    def __init__(self, remote_root: Path, *, instance_id: str = "i-0fakegpu", instance_type: str = "g5.xlarge",
                 ami_id: str = "ami-0fake") -> None:
        self.remote_root = Path(remote_root)
        self.instance_id = instance_id
        self.instance_type = instance_type
        self.ami_id = ami_id
        self.busy = False
        self.drift = False
        self.fail_push: Optional[str] = None
        self.fail_pull: Optional[str] = None
        self.fail_start: Optional[str] = None
        self.interrupt_after_wait = False
        self.ignore_timeout = False
        self.crash_on_wait = False
        self.calls: List[Tuple[str, Any]] = []
        self.pushed: List[str] = []
        self.pulled: List[str] = []
        self.registered: Dict[str, Dict[str, Any]] = {}
        self.released: List[str] = []
        self.state = "stopped"
        self.started_at: Optional[str] = None
        self.stops: List[str] = []
        self._jobs: Dict[str, _FakeJob] = {}
        self._lock = threading.Lock()

    def mirror(self, path: Any) -> Path:
        return self.remote_root / str(path).lstrip("/")

    def _info(self) -> BoxInfo:
        running = sum(1 for job in self._jobs.values() if job.final is None and job.proc is not None)
        return BoxInfo(instance_id=self.instance_id, state=self.state, instance_type=self.instance_type,
                       az="us-east-2a", private_ip="172.31.0.9", ami_id=self.ami_id, started_at=self.started_at,
                       ready=self.state == "running", bootstrapped=True, jobs_running=running)

    def describe(self, *, cached_ok: bool = False) -> BoxInfo:
        self.calls.append(("describe", cached_ok))
        return self._info()

    def ensure_running(self, *, need_seconds: int = 0) -> BoxInfo:
        self.calls.append(("ensure_running", need_seconds))
        if self.fail_start:
            raise GpuBoxError(self.fail_start, f"fake box refuses to start: {self.fail_start}",
                              retryable=self.fail_start == "capacity")
        if self.state != "running":
            self.state = "running"
            self.started_at = utc_now()
        return self._info()

    def stop_if_idle(self, idle_minutes: Optional[int] = None) -> Dict[str, Any]:
        self.calls.append(("stop_if_idle", idle_minutes))
        if self.registered:
            return {"stopped": False, "reason": "jobs_active"}
        self.state = "stopped"
        self.stops.append("idle")
        return {"stopped": True, "reason": "idle"}

    def stop_now(self, reason: str = "operator", *, kill_jobs: bool = True) -> Dict[str, Any]:
        self.calls.append(("stop_now", reason))
        if kill_jobs:
            for job in self._jobs.values():
                job.end("interrupted")
        self.state = "stopped"
        self.stops.append(reason)
        return {"stopped": True, "reason": reason}

    def exec_remote(self, argv: Sequence[str], cwd: Optional[str] = None, timeout: float = 600.0,
                    env: Optional[Dict[str, str]] = None, stdin: Optional[str] = None
                    ) -> "subprocess.CompletedProcess[str]":
        argv = list(argv)
        self.calls.append(("exec_remote", argv))
        out, err, code = "", "", 0
        if argv[:1] == ["nvidia-smi"]:
            out = f"{GPU_NAME}, {DRIFTED_DRIVER if self.drift else DRIVER}\n"
        elif argv[:3] == ["docker", "image", "inspect"]:
            out = IMAGE_ID + "\n"
        elif argv[:2] == ["docker", "run"] and argv[-1] == "/opt/adv-loop/PINS":
            out = PINS
        elif argv[:2] == ["du", "-sb"]:
            for target in argv[2:]:
                path = self.mirror(target)
                if path.exists():
                    out += f"{_tree_bytes(path)}\t{target}\n"
                else:
                    err += f"du: cannot access '{target}': No such file or directory\n"
                    code = 1
        elif argv[:2] == ["mkdir", "-p"]:
            for target in argv[2:]:
                self.mirror(target).mkdir(parents=True, exist_ok=True)
        elif argv[:2] == ["adv-gpu-agent", "heartbeat"]:
            out = "ok\n"
        else:
            code, err = 127, f"fake box: unknown command {argv[0]}\n"
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    def push(self, paths: Sequence[str], dest: str, *, workspace: Path, delete: bool = False) -> Dict[str, Any]:
        self.calls.append(("push", list(paths)))
        if self.fail_push:
            raise GpuBoxError("push_failed", self.fail_push, retryable=True)
        for rel in paths:
            _copy(Path(workspace) / rel, self.mirror(dest) / rel)
            self.pushed.append(rel)
        return {"pushed": list(paths)}

    def pull(self, paths: Sequence[str], src: str, *, workspace: Path) -> Dict[str, Any]:
        self.calls.append(("pull", list(paths)))
        if self.fail_pull:
            raise GpuBoxError("pull_failed", self.fail_pull, retryable=True)
        missing: List[str] = []
        for rel in paths:
            source = self.mirror(src) / rel
            if not source.exists():
                missing.append(rel)
                continue
            _copy(source, Path(workspace) / rel)
            self.pulled.append(rel)
        return {"pulled": [p for p in paths if p not in missing], "missing": missing}

    def submit(self, job_dir: str) -> Dict[str, Any]:
        self.calls.append(("submit", job_dir))
        with self._lock:
            if self.busy or any(job.final is None for job in self._jobs.values()):
                raise GpuBoxError("busy", "a job is already running on the box", retryable=True)
            job = _FakeJob(self, job_dir)
            self._jobs[job_dir] = job
        return job.start()

    def wait(self, job_dir: str, *, max_seconds: float) -> Dict[str, Any]:
        self.calls.append(("wait", job_dir, max_seconds))
        if self.crash_on_wait:
            raise RuntimeError("simulated harness crash while waiting")
        job = self._jobs.get(job_dir)
        if job is None:
            return {"state": "missing"}
        if self.interrupt_after_wait:
            return job.end("interrupted")
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            status = job.status()
            if status.get("state") in TERMINAL:
                return status
            time.sleep(0.02)
        return job.status()

    def status(self, job_dir: str) -> Dict[str, Any]:
        self.calls.append(("status", job_dir))
        job = self._jobs.get(job_dir)
        return job.status() if job else {"state": "missing"}

    def kill(self, job_dir: str) -> Dict[str, Any]:
        self.calls.append(("kill", job_dir))
        job = self._jobs.get(job_dir)
        return job.end("killed") if job else {"state": "missing"}

    def register_job(self, job_id: str, *, workspace: str, session_id: str, timeout_seconds: int) -> None:
        self.calls.append(("register_job", job_id))
        self.registered[job_id] = {"job_id": job_id, "workspace": workspace, "session_id": session_id,
                                   "timeout_seconds": timeout_seconds, "started_at": utc_now()}

    def release_job(self, job_id: str) -> None:
        self.calls.append(("release_job", job_id))
        self.registered.pop(job_id, None)
        self.released.append(job_id)

    def active_jobs(self) -> List[Dict[str, Any]]:
        return list(self.registered.values())

    def reap(self) -> Dict[str, Any]:
        self.calls.append(("reap", None))
        return {"reaped": [], "stop": self.stop_if_idle()}

    def wait_all(self, timeout: float = 30.0) -> None:
        for job in list(self._jobs.values()):
            job.join(timeout)

    def names(self) -> List[str]:
        return [call[0] for call in self.calls]
