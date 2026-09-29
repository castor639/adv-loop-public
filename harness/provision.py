"""Turn a freshly dropped workspace into a containerized one.

Writes the docker `sandbox` block, the checker hooks wrapped by the toolchain
stamp, the `harness` block, the attestation requirement, and the container
lock, then generates `.checker-key`. Existing keys in loop-config.json that the
harness does not own are left alone. The kernel is never told about docker;
it only ever executes the argv lists written here.

A workspace whose charter grants GPU compute (`harness.gpu.enabled`) also
gets the GPU lock, the two host-side GPU checkers, session budgets long
enough for a job, and the overlay templates a surgeon may propose. Without a
fleet lock the grant cannot be honoured, so provisioning fails loudly.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from adv_loop.pathsafe import hash_file
from adv_loop.storage import atomic_write_text

from . import architecture
from . import container as ct
from . import gpu as gpu_module
from .backends.base import SessionBudget
from .gpu import lock as gpu_lock_module

CHECKER_KEY = ".checker-key"
STAMP = "harness/container/toolchain_stamp.py"
CHECKERS = {
    "lean-kernel": ("checkers/lean_kernel.py", "kernel_proof", 900),
    "z3": ("checkers/z3_discharge.py", "smt_discharge", 300),
    "cert-replay": ("checkers/command_checker.py", "executable_spec", 600),
}
GPU_CHECKERS = {
    "gpu-replay": ("checkers/gpu_replay.py", "executable_spec", 600),
    "gpu-replicate": ("checkers/gpu_replicate.py", "replicated_experiment", 900),
}
GPU_BUDGET_KEYS = ("researcher/experiment", "verifier/independent_verification")
GPU_BUDGET_SLACK_SECONDS = 3600
GPU_SANDBOX_ID = "gpu-box"
SETUP_TIMEOUT = 5400
DEFAULT_HARNESS_BLOCK = {
    "roles": {},
    "notional_ceiling_usd": 15.0,
    "session": {"max_budget_usd": 15.0, "wall_clock_seconds": 3000, "max_turns": 80},
}
# What a drill without a box records as the GPU environment; `drill --gpu` replaces it with the fake box's lock.
FAKE_GPU_LOCK = gpu_lock_module.GpuLock(image=gpu_module.DEFAULTS["image"], image_digest="sha256:" + "00" * 32,
                                        cuda="0.0", driver="0.0", gpu="none", instance_type=gpu_module.INSTANCE_TYPE)


def read_config(ws: Path) -> Dict[str, Any]:
    path = ws / "loop-config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_config(ws: Path, config: Dict[str, Any]) -> None:
    atomic_write_text(ws / "loop-config.json", json.dumps(config, indent=2, sort_keys=True) + "\n")


def ensure_checker_key(ws: Path) -> bool:
    path = ws / CHECKER_KEY
    if path.exists():
        return False
    path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return True


def validator_block(repo_mount: str, hooks: Optional[List[str]] = None) -> Dict[str, Any]:
    block: Dict[str, Any] = {}
    for name in hooks or list(CHECKERS):
        script, rank, timeout = CHECKERS[name]
        block[name] = {
            "command": ["python3", f"{repo_mount}/{STAMP}", "python3", f"{repo_mount}/{script}"],
            "in_sandbox": True,
            "rank": rank,
            "timeout_seconds": timeout,
        }
    return block


def sandbox_block(ws_abs: Path, name: str, lockfile_hashes: Dict[str, str]) -> Dict[str, Any]:
    return {
        "kind": "docker",
        "root": "sandbox",
        "command": ct.exec_prefix(name, ws_abs),
        "setup": ct.exec_prefix(name, ws_abs / "sandbox", interactive=False) + ["bash", "setup.sh"],
        "setup_timeout_seconds": SETUP_TIMEOUT,
        "lockfile_hashes": lockfile_hashes,
    }


def install_setup_script(ws: Path) -> Path:
    target = ws / "sandbox" / "setup.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(Path(__file__).resolve().parent / "container" / "setup.sh", target)
        os.chmod(target, 0o755)
    return target


def overlay_template(ws: Path, lock: Optional[ct.ContainerLock], hooks: List[str],
                     gpu_lock: Optional[gpu_lock_module.GpuLock] = None) -> Path:
    """What a surgeon may propose to pin the toolchain; never adopted here.

    GPU verdicts carry the box toolchain hash, not the container's, so their
    pins name the GPU lock. `pin_toolchain_hash` is one-shot in the kernel: a
    driver or image change after adoption blocks GPU verdicts for the
    workspace, which is why the runner refuses `environment_drift` before
    spending hours rather than letting a job produce an unpinnable verdict.
    """

    ops = [{"op": "pin_toolchain_hash", "toolchain_id": hook, "artifact_hash": lock.toolchain_hash}
           for hook in hooks] if lock is not None else []
    if gpu_lock is not None:
        ops += [{"op": "pin_toolchain_hash", "toolchain_id": hook, "artifact_hash": gpu_lock.toolchain_hash}
                for hook in GPU_CHECKERS]
    path = ws / ".harness" / "overlay-templates" / "pin-toolchain.json"
    atomic_write_text(path, json.dumps({"ops": ops, "note": "surgeon template: pins every checker to the running container"},
                                       indent=2, sort_keys=True) + "\n")
    return path


def gpu_sandbox_template(ws: Path) -> Path:
    """The `register_sandbox` op naming the GPU box as a mechanism; proposed by a surgeon, never adopted here."""

    ops = [{"op": "register_sandbox", "sandbox_id": GPU_SANDBOX_ID,
            "description": "one g5.xlarge A10G box reached only through the gpu_run tool inside the pinned GPU image",
            "mechanism_locator": "harness/gpu/jobs.py"}]
    path = ws / ".harness" / "overlay-templates" / "register-gpu-sandbox.json"
    atomic_write_text(path, json.dumps({"ops": ops, "note": "surgeon template: registers the GPU box as a sandbox"},
                                       indent=2, sort_keys=True) + "\n")
    return path


def gpu_block(config: Dict[str, Any], gpu_config: Optional[Dict[str, Any]],
              gpu_lock: Optional[gpu_lock_module.GpuLock]) -> Optional[Dict[str, Any]]:
    """The `harness.gpu` block to write, or None without the grant; raises without a fleet lock."""

    merged = gpu_module.config_for(config)
    if gpu_config:
        merged.update(gpu_config)
    if merged.get("enabled") is not True:
        return None
    if gpu_lock is None:
        raise RuntimeError("no fleet GPU lock; run adv-harness gpu provision-box")
    merged.update({
        "image": gpu_lock.image or merged["image"], "image_digest": gpu_lock.image_digest, "cuda": gpu_lock.cuda,
        "driver": gpu_lock.driver, "instance_type": gpu_lock.instance_type or merged["instance_type"],
        "toolchain_hash": gpu_lock.toolchain_hash,
        "provisioned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return merged


def gpu_validator_block(repo: Optional[Path]) -> Dict[str, Any]:
    """Host-side hooks: the checkers read the authoritative record, which the container never sees."""

    block: Dict[str, Any] = {}
    for name, (script, rank, timeout) in GPU_CHECKERS.items():
        path = str(repo / script) if repo is not None else f"{ct.REPO_MOUNT}/{script}"
        block[name] = {"command": ["python3", path], "in_sandbox": False, "rank": rank, "timeout_seconds": timeout}
    return block


def gpu_budgets(existing: Optional[Dict[str, Any]], max_job_seconds: int) -> Dict[str, Any]:
    """Session budgets long enough for one job plus an hour, only where the operator set none."""

    budgets = dict(existing or {})
    for key in GPU_BUDGET_KEYS:
        if key not in budgets:
            budgets[key] = {**dataclasses.asdict(SessionBudget()),
                            "wall_clock_seconds": int(max_job_seconds) + GPU_BUDGET_SLACK_SECONDS}
    return budgets


def apply_gpu(ws_abs: Path, config: Dict[str, Any], block: Dict[str, Any], gpu_lock: gpu_lock_module.GpuLock,
              repo: Optional[Path]) -> str:
    """Write the workspace lock, the hooks, the budgets, and the templates; returns the lock's sha256."""

    lock_sha = gpu_lock_module.write_workspace_lock(ws_abs, gpu_lock)
    config["validators"] = {**(config.get("validators") or {}), **gpu_validator_block(repo)}
    harness_block = config["harness"]
    harness_block["gpu"] = block
    harness_block["budgets"] = gpu_budgets(harness_block.get("budgets"), int(block["max_job_seconds"]))
    gpu_sandbox_template(ws_abs)
    return lock_sha


def provision(
    ws: Path,
    docker: ct.Docker,
    *,
    image: str = ct.DEFAULT_IMAGE,
    repo: Optional[Path] = None,
    hooks: Optional[List[str]] = None,
    limits: Optional[Dict[str, Any]] = None,
    roles: Optional[Dict[str, Any]] = None,
    mask_file: str = "/dev/null",
    gpu_lock: Optional[gpu_lock_module.GpuLock] = None,
    gpu_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ws_abs = ws.resolve()
    name = ct.container_name(ws_abs.name)
    hooks = hooks or list(CHECKERS)
    config = read_config(ws)
    gpu = gpu_block(config, gpu_config, gpu_lock)

    digest = docker.image_digest(image)
    if not digest:
        raise RuntimeError(f"image {image} is not present on this host")
    pins = docker.read_image_pins(image)
    lock = ct.ContainerLock(
        image=image, image_digest=digest, lean_toolchain=pins.get("lean", ""),
        mathlib_rev=pins.get("mathlib", ""), z3_version=pins.get("z3", ""),
        claude_code_version=pins.get("claude-code", ""),
    )

    (ws_abs / ".harness" / "validate").mkdir(parents=True, exist_ok=True)
    (ws_abs / ".harness" / "host").mkdir(parents=True, exist_ok=True)
    (ws_abs / "payload").mkdir(exist_ok=True)
    architecture.ensure(ws_abs)
    keygen = ensure_checker_key(ws_abs)
    setup = install_setup_script(ws_abs)
    lock_sha = ct.write_lock(ws_abs, lock)

    lockfile_hashes = dict((config.get("sandbox") or {}).get("lockfile_hashes") or {})
    lockfile_hashes[f"sandbox/{ct.LOCK_NAME}"] = lock_sha
    lockfile_hashes["sandbox/setup.sh"] = hash_file(setup)[0]
    for pinned in ("sandbox/lean-toolchain", "sandbox/uv.lock"):
        candidate = ws_abs / pinned
        if candidate.is_file():
            lockfile_hashes[pinned] = hash_file(candidate)[0]

    env = {"ADV_LOOP_TOOLCHAIN_HASH": lock.toolchain_hash, "ADV_LOOP_WORKSPACE": str(ws_abs)}
    state = docker.ensure_container(ws_abs, name, image, limits=limits, env=env, repo=repo,
                                    masks=ct.default_masks(ws_abs, mask_file))

    config["sandbox"] = sandbox_block(ws_abs, name, lockfile_hashes)
    config["validators"] = {**(config.get("validators") or {}), **validator_block(ct.REPO_MOUNT, hooks)}
    config["checker_attestation"] = {"require": True}
    harness_block = {**DEFAULT_HARNESS_BLOCK, **(config.get("harness") or {})}
    harness_block.update({"image": image, "container": name, "toolchain_hash": lock.toolchain_hash,
                          "limits": {**ct.DEFAULT_LIMITS, **(limits or {})}})
    if roles:
        harness_block["roles"] = {**harness_block.get("roles", {}), **roles}
    config["harness"] = harness_block
    if gpu is not None:
        lockfile_hashes[gpu_module.WORKSPACE_LOCK_FILE] = apply_gpu(ws_abs, config, gpu, gpu_lock, repo)
        config["sandbox"]["lockfile_hashes"] = lockfile_hashes
        hooks = hooks + list(GPU_CHECKERS)
    write_config(ws_abs, config)
    template = overlay_template(ws_abs, lock, [h for h in hooks if h in CHECKERS], gpu_lock if gpu else None)

    return {
        "workspace": str(ws_abs), "container": name, "container_state": state, "image": image,
        "image_digest": digest, "toolchain_hash": lock.toolchain_hash, "checker_key_created": keygen,
        "lockfile_hashes": lockfile_hashes, "hooks": hooks, "overlay_template": str(template),
        "gpu": gpu_report(gpu),
    }


def gpu_report(block: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if block is None:
        return {"enabled": False}
    return {"enabled": True, "toolchain_hash": block.get("toolchain_hash"), "image": block.get("image"),
            "max_hours": block.get("max_hours"), "max_job_seconds": block.get("max_job_seconds")}


def provision_local(ws: Path, *, repo: Optional[Path] = None, hooks: Optional[List[str]] = None,
                    gpu: bool = False, gpu_lock: Optional[gpu_lock_module.GpuLock] = None) -> Dict[str, Any]:
    """No container: hooks run on the host with absolute checker paths. For drills and development boxes.

    `gpu` (or the grant in loop-config) writes the same GPU block a containerized
    workspace gets, against `gpu_lock` or a placeholder lock when no box exists.
    """

    ws_abs = ws.resolve()
    repo = (repo or Path(__file__).resolve().parents[1]).resolve()
    hooks = hooks or ["cert-replay"]
    config = read_config(ws_abs)
    gpu_config = {"enabled": True} if gpu else None
    block = gpu_block(config, gpu_config, gpu_lock or FAKE_GPU_LOCK)
    (ws_abs / ".harness" / "validate").mkdir(parents=True, exist_ok=True)
    (ws_abs / ".harness" / "host").mkdir(parents=True, exist_ok=True)
    (ws_abs / "payload").mkdir(exist_ok=True)
    (ws_abs / "sandbox").mkdir(exist_ok=True)
    architecture.ensure(ws_abs)
    keygen = ensure_checker_key(ws_abs)
    validators = {}
    for name in hooks:
        script, rank, timeout = CHECKERS[name]
        validators[name] = {"command": ["python3", str(repo / script)], "in_sandbox": False, "rank": rank,
                            "timeout_seconds": timeout}
    config["validators"] = {**(config.get("validators") or {}), **validators}
    config["checker_attestation"] = {"require": True}
    config["harness"] = {**DEFAULT_HARNESS_BLOCK, **(config.get("harness") or {}), "container": None, "mode": "local"}
    config.setdefault("sandbox", {"kind": "local", "root": "sandbox"})
    if block is not None:
        lock_sha = apply_gpu(ws_abs, config, block, gpu_lock or FAKE_GPU_LOCK, repo)
        config["sandbox"]["lockfile_hashes"] = {**(config["sandbox"].get("lockfile_hashes") or {}),
                                                gpu_module.WORKSPACE_LOCK_FILE: lock_sha}
        hooks = hooks + list(GPU_CHECKERS)
        overlay_template(ws_abs, None, [], gpu_lock or FAKE_GPU_LOCK)
    write_config(ws_abs, config)
    return {"workspace": str(ws_abs), "container": None, "mode": "local", "hooks": hooks, "checker_key_created": keygen,
            "gpu": gpu_report(block)}
