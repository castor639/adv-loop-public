"""What the GPU box looked like when a job ran, and what it must still look like.

A checker verdict on a GPU output carries `toolchain_hash`; the kernel's
`pin_toolchain_hash` overlay pins it once per workspace. The hash covers the
parts that change what a job computes (image digest, CUDA, driver, instance
type) and nothing cosmetic, so a rebuilt tag with the same digest still
verifies while a driver upgrade is refused before any hours are spent.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from adv_loop.pathsafe import hash_file
from adv_loop.storage import atomic_write_text, object_hash

from .. import container
from . import PINS_PATH, STATE_LOCK_FILE, WORKSPACE_LOCK_FILE
from .box_api import Box, GpuBoxError

LOCK_FIELDS = ("image", "image_digest", "cuda", "torch", "python", "driver", "gpu", "instance_type", "ami_id")
SMI_ARGV = ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]


@dataclass(frozen=True)
class GpuLock:
    image: str
    image_digest: str
    cuda: str = ""
    torch: str = ""
    python: str = ""
    driver: str = ""
    gpu: str = ""
    instance_type: str = ""
    ami_id: str = ""

    @property
    def toolchain_hash(self) -> str:
        return toolchain_hash(self.image_digest, self.cuda, self.driver, self.instance_type)

    def as_dict(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in LOCK_FIELDS}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GpuLock":
        return cls(**{name: str(data.get(name, "") or "") for name in LOCK_FIELDS})


def toolchain_hash(image_digest: str, cuda: str, driver: str, instance_type: str) -> str:
    return object_hash({"image_digest": image_digest, "cuda": cuda, "driver": driver,
                        "instance_type": instance_type})


def _probe(box: Box, argv: list) -> str:
    try:
        done = box.exec_remote(argv, timeout=120.0)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GpuBoxError("box_unreachable", f"{argv[0]} probe did not answer: {exc}", retryable=True)
    if done.returncode != 0:
        raise GpuBoxError("environment_drift", f"{argv[0]} probe failed on the box: {done.stderr.strip()[:300]}")
    return done.stdout


def observe(box: Box, *, image: str) -> GpuLock:
    """Read the environment from the running box; raises GpuBoxError when a probe fails."""

    first = _probe(box, SMI_ARGV).strip().splitlines()
    gpu, driver = "", ""
    if first:
        parts = [part.strip() for part in first[0].split(",")]
        gpu = parts[0]
        driver = parts[1] if len(parts) > 1 else ""
    digest = _probe(box, ["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    pins = container.parse_pins(box.exec_remote(["docker", "run", "--rm", "--entrypoint", "cat", image, PINS_PATH],
                                                timeout=300.0))
    info = box.describe(cached_ok=True)
    return GpuLock(image=image, image_digest=digest, cuda=pins.get("cuda", ""), torch=pins.get("torch", ""),
                   python=pins.get("python", ""), driver=driver, gpu=gpu,
                   instance_type=info.instance_type, ami_id=info.ami_id or "")


def _dump(lock: GpuLock) -> str:
    return json.dumps(lock.as_dict(), indent=2, sort_keys=True) + "\n"


def _load(path: Path) -> Optional[GpuLock]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return GpuLock.from_dict(data) if isinstance(data, dict) else None


def fleet_lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_LOCK_FILE


def write_fleet_lock(state_dir: Path, lock: GpuLock) -> Path:
    path = fleet_lock_path(state_dir)
    atomic_write_text(path, _dump(lock))
    return path


def read_fleet_lock(state_dir: Path) -> Optional[GpuLock]:
    return _load(fleet_lock_path(state_dir))


def workspace_lock_path(ws: Path) -> Path:
    return Path(ws) / WORKSPACE_LOCK_FILE


def write_workspace_lock(ws: Path, lock: GpuLock) -> str:
    """Write `sandbox/gpu.lock`; returns its sha256 for `lockfile_hashes`."""

    path = workspace_lock_path(ws)
    atomic_write_text(path, _dump(lock))
    return hash_file(path)[0]


def read_workspace_lock(ws: Path) -> Optional[GpuLock]:
    return _load(workspace_lock_path(ws))


def lock_sha256(ws: Path) -> Optional[str]:
    path = workspace_lock_path(ws)
    return hash_file(path)[0] if path.is_file() else None
