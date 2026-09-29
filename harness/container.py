"""Docker container per workspace.

The workspace is bind-mounted at its host absolute path, so the kernel's
validator hooks, which run on the host with host paths, execute unchanged
inside the container when `sandbox.command` is the `docker exec` prefix this
module builds. Nothing here reads a credential.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from adv_loop.pathsafe import hash_file
from adv_loop.storage import atomic_write_text, object_hash

CONTAINER_PREFIX = "adv-ws-"
DEFAULT_IMAGE = "adv-loop-ws:v4.29.0-2"
REPO_MOUNT = "/srv/adv-loop/repo"
# The supervisor's own uid runs inside the container so the path-identical mount stays writable
# (the image's non-root user only matters for `claude --dangerously-skip-permissions`, which refuses root).
SANDBOX_USER = f"{os.getuid()}:{os.getgid()}"
LOCK_NAME = "container.lock"

DEFAULT_LIMITS = {
    "memory": "5g",
    "cpus": "2",
    "pids_limit": 1024,
    "tmp_size": "2g",
}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def container_name(ws_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in ws_id)
    return CONTAINER_PREFIX + safe[:60]


def default_masks(ws_abs: Path, mask_file: str = "/dev/null") -> List[List[str]]:
    """Mounts that hide host-only material from the container.

    The checker key is bind-masked with an empty file and the host-only
    directory (broker verdicts, session records) is shadowed by a tmpfs, so a
    session with write access to the workspace still cannot read the key or
    forge a verdict the host would recognise.
    """

    return [
        ["-v", f"{mask_file}:{ws_abs / '.checker-key'}:ro"],
        ["--tmpfs", f"{ws_abs / '.harness' / 'host'}:rw,size=1m"],
    ]


def run_args(
    ws_abs: Path,
    name: str,
    image: str,
    *,
    limits: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
    repo: Optional[Path] = None,
    masks: Optional[List[List[str]]] = None,
) -> List[str]:
    """`docker run` argv for a long-lived workspace container."""

    limits = {**DEFAULT_LIMITS, **(limits or {})}
    argv = [
        "docker", "run", "-d", "--name", name, "--restart", "unless-stopped", "--init",
        "--user", SANDBOX_USER,
        "--memory", str(limits["memory"]), "--memory-swap", str(limits["memory"]),
        "--cpus", str(limits["cpus"]), "--pids-limit", str(limits["pids_limit"]),
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "-v", f"{ws_abs}:{ws_abs}:rw",
        "--tmpfs", f"/tmp:rw,size={limits['tmp_size']}",
    ]
    if repo is not None:
        argv += ["-v", f"{repo}:{REPO_MOUNT}:ro"]
    for mask in masks or []:
        argv += mask
    for key in sorted(env or {}):
        argv += ["-e", f"{key}={env[key]}"]
    argv += [image, "sleep", "infinity"]
    return argv


def exec_prefix(name: str, cwd: Path, *, env: Optional[Dict[str, str]] = None, interactive: bool = True) -> List[str]:
    """The `docker exec` prefix used as `sandbox.command` and for backend sessions."""

    argv = ["docker", "exec"]
    if interactive:
        argv.append("-i")
    argv += ["-u", SANDBOX_USER, "-w", str(cwd)]
    for key in sorted(env or {}):
        argv += ["-e", f"{key}={env[key]}"]
    argv.append(name)
    return argv


@dataclass
class Docker:
    """Thin wrapper so tests can substitute a fake runner."""

    runner: Runner = subprocess.run
    timeout: int = 120

    def call(self, argv: Sequence[str], *, check: bool = False, timeout: Optional[int] = None,
             stdin: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
        return self.runner(list(argv), capture_output=True, text=True, check=check,
                           timeout=timeout or self.timeout, input=stdin)

    def inspect(self, name: str) -> Optional[Dict[str, Any]]:
        done = self.call(["docker", "inspect", name])
        if done.returncode != 0:
            return None
        try:
            data = json.loads(done.stdout)
        except ValueError:
            return None
        return data[0] if data else None

    def image_digest(self, image: str) -> Optional[str]:
        done = self.call(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        if done.returncode != 0:
            return None
        return done.stdout.strip() or None

    def running(self, name: str) -> bool:
        info = self.inspect(name)
        return bool(info and info.get("State", {}).get("Running"))

    def ensure_container(self, ws_abs: Path, name: str, image: str, *, limits=None, env=None,
                         repo: Optional[Path] = None, masks: Optional[List[List[str]]] = None) -> str:
        """Create or start the workspace container; returns its state word."""

        info = self.inspect(name)
        if info is None:
            self.call(run_args(ws_abs, name, image, limits=limits, env=env, repo=repo, masks=masks), check=True)
            return "created"
        if info.get("State", {}).get("Running"):
            return "running"
        self.call(["docker", "start", name], check=True)
        return "started"

    def exec(self, name: str, cwd: Path, argv: Sequence[str], *, env=None, timeout: Optional[int] = None,
             stdin: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
        return self.call(exec_prefix(name, cwd, env=env) + list(argv), timeout=timeout, stdin=stdin)

    def read_pins(self, name: str) -> Dict[str, str]:
        return parse_pins(self.exec(name, Path("/"), ["cat", "/opt/lean/PINS"]))

    def read_image_pins(self, image: str) -> Dict[str, str]:
        """Pins from the image itself, before any container exists."""

        return parse_pins(self.call(["docker", "run", "--rm", "--entrypoint", "cat", image, "/opt/lean/PINS"]))

    def stop(self, name: str) -> None:
        self.call(["docker", "stop", "-t", "20", name])

    def kill_processes(self, name: str, pattern: str) -> None:
        self.exec(name, Path("/"), ["pkill", "-TERM", "-f", pattern])


def parse_pins(done: "subprocess.CompletedProcess[str]") -> Dict[str, str]:
    pins: Dict[str, str] = {}
    if done.returncode == 0:
        for line in done.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                pins[key.strip()] = value.strip()
    return pins


@dataclass(frozen=True)
class ContainerLock:
    image: str
    image_digest: str
    lean_toolchain: str
    mathlib_rev: str
    z3_version: str = ""
    claude_code_version: str = ""
    extra: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        data = {
            "image": self.image,
            "image_digest": self.image_digest,
            "lean_toolchain": self.lean_toolchain,
            "mathlib_rev": self.mathlib_rev,
            "z3_version": self.z3_version,
            "claude_code_version": self.claude_code_version,
        }
        data.update(self.extra)
        return data

    @property
    def toolchain_hash(self) -> str:
        return toolchain_hash(self.image_digest, self.lean_toolchain, self.mathlib_rev)


def toolchain_hash(image_digest: str, lean_toolchain: str, mathlib_rev: str) -> str:
    """What a checker verdict must carry; the kernel's `pin_toolchain_hash` overlay enforces it."""

    return object_hash({"image_digest": image_digest, "lean_toolchain": lean_toolchain, "mathlib_rev": mathlib_rev})


def lock_path(ws: Path) -> Path:
    return ws / "sandbox" / LOCK_NAME


def write_lock(ws: Path, lock: ContainerLock) -> str:
    """Write `sandbox/container.lock`; returns its sha256 for `lockfile_hashes`."""

    path = lock_path(ws)
    atomic_write_text(path, json.dumps(lock.as_dict(), indent=2, sort_keys=True) + "\n")
    return hash_file(path)[0]


def read_lock(ws: Path) -> Optional[ContainerLock]:
    path = lock_path(ws)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {"image", "image_digest", "lean_toolchain", "mathlib_rev", "z3_version", "claude_code_version"}
    return ContainerLock(
        image=data["image"], image_digest=data["image_digest"], lean_toolchain=data["lean_toolchain"],
        mathlib_rev=data["mathlib_rev"], z3_version=data.get("z3_version", ""),
        claude_code_version=data.get("claude_code_version", ""),
        extra={k: v for k, v in data.items() if k not in known},
    )


def pin(ws: Path, docker: Docker, name: str, image: str) -> ContainerLock:
    """Read the running container's pins and write the lock."""

    digest = docker.image_digest(image)
    if not digest:
        raise RuntimeError(f"image {image} is not present; build it first")
    pins = docker.read_pins(name)
    lock = ContainerLock(
        image=image, image_digest=digest,
        lean_toolchain=pins.get("lean", ""), mathlib_rev=pins.get("mathlib", ""),
        z3_version=pins.get("z3", ""), claude_code_version=pins.get("claude-code", ""),
    )
    write_lock(ws, lock)
    return lock
