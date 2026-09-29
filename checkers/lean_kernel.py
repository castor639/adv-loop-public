#!/usr/bin/env python3
"""Lean proof-kernel checker: accepted iff the Lean kernel elaborates the file.

Register as a validator hook (rank kernel_proof) and pass the file via --input:

  run.json: {"file": "payload/scratch/Candidate.lean", "cwd": "sandbox"}

Runs `lake env lean <file>` when a lakefile is present in cwd (so Mathlib and
the pinned toolchain resolve), else `lean <file>`. The artifact_hash is the
SHA-256 of the .lean source the kernel accepted — the same hash the evidence
fingerprint must carry. Requires lean/lake on PATH (typically via the
workspace sandbox); a missing toolchain is a hook failure, not a fake verdict.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    envelope = json.load(sys.stdin)
    workspace = Path(envelope["workspace"]).resolve()
    config = envelope.get("input") or {}
    target = config.get("file")
    if not isinstance(target, str) or not target.strip():
        print("lean_kernel: input.file must name the .lean file to check", file=sys.stderr)
        raise SystemExit(2)
    file_path = (workspace / target).resolve()
    if workspace not in file_path.parents or not file_path.is_file():
        print(f"lean_kernel: file not found inside the workspace: {target}", file=sys.stderr)
        raise SystemExit(2)
    cwd = (workspace / str(config.get("cwd", "sandbox"))).resolve()
    if cwd != workspace and workspace not in cwd.parents or not cwd.is_dir():
        print("lean_kernel: cwd must be a directory inside the workspace", file=sys.stderr)
        raise SystemExit(2)
    # A workspace-local elan (sandbox/.elan, as provisioned by a declared
    # sandbox setup) takes precedence over whatever is on the ambient PATH, so
    # the verdict always comes from the workspace's pinned toolchain.
    env = dict(os.environ)
    local_elan = workspace / "sandbox" / ".elan"
    if (local_elan / "bin").is_dir():
        env["ELAN_HOME"] = str(local_elan)
        env["PATH"] = str(local_elan / "bin") + os.pathsep + env.get("PATH", "")
    which = lambda name: shutil.which(name, path=env.get("PATH"))
    extra = config.get("lean_args", [])
    if not isinstance(extra, list) or any(not isinstance(a, str) for a in extra):
        print("lean_kernel: input.lean_args must be a list of strings", file=sys.stderr)
        raise SystemExit(2)
    use_lake = (cwd / "lakefile.lean").is_file() or (cwd / "lakefile.toml").is_file()
    if use_lake and which("lake"):
        command = ["lake", "env", "lean", *extra, str(file_path)]
    elif which("lean"):
        command = ["lean", *extra, str(file_path)]
    else:
        print("lean_kernel: neither lake nor lean is on PATH", file=sys.stderr)
        raise SystemExit(2)
    # A broken toolchain is a hook failure, never a rejection verdict: the
    # kernel that did not run has judged nothing.
    probe = subprocess.run([command[0], "--version"], capture_output=True, text=True,
                           check=False, env=env, cwd=str(cwd))
    if probe.returncode != 0:
        print(f"lean_kernel: toolchain does not run: {probe.stderr.strip()[:200]}", file=sys.stderr)
        raise SystemExit(2)
    timeout = float(config.get("timeout_seconds", 900))
    result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                            timeout=timeout, check=False, env=env)
    print(json.dumps({
        "accepted": result.returncode == 0,
        "checker_id": "lean-kernel",
        "checker_version": "1.0",
        "artifact_hash": hashlib.sha256(file_path.read_bytes()).hexdigest(),
        "log_hash": hashlib.sha256(
            (result.stdout + "\n---\n" + result.stderr).encode("utf-8", "replace")
        ).hexdigest(),
        "details": {"returncode": result.returncode, "stderr_tail": result.stderr[-2000:]},
    }))


if __name__ == "__main__":
    main()
