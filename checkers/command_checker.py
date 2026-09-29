#!/usr/bin/env python3
"""Generic command checker: accepted iff the declared command exits 0.

Register it as a validator hook and pass the checked command via --input:

  loop-config.json:  "validators": {"suite": {"command": ["python3", "checkers/command_checker.py"],
                                              "rank": "executable_spec"}}
  adv-loop validate <ws> suite --input run.json
  run.json:          {"command": ["python3", "-m", "unittest"], "cwd": "sandbox",
                      "artifact": "payload/derived/result.txt"}

The verdict's artifact_hash is the SHA-256 of the named artifact (or of the
command's stdout when no artifact is declared); log_hash covers stdout+stderr.
Stdlib only; lives outside src/ like every checker.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    envelope = json.load(sys.stdin)
    workspace = Path(envelope["workspace"]).resolve()
    config = envelope.get("input") or {}
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item.strip() for item in command
    ):
        print("command_checker: input.command must be a non-empty argv list", file=sys.stderr)
        raise SystemExit(2)
    cwd = (workspace / str(config.get("cwd", "."))).resolve()
    if cwd != workspace and workspace not in cwd.parents:
        print("command_checker: cwd escapes the workspace", file=sys.stderr)
        raise SystemExit(2)
    timeout = float(config.get("timeout_seconds", 600))
    result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                            timeout=timeout, check=False)
    log_hash = hashlib.sha256(
        (result.stdout + "\n---\n" + result.stderr).encode("utf-8", "replace")
    ).hexdigest()
    artifact = config.get("artifact")
    if artifact:
        artifact_path = (workspace / str(artifact)).resolve()
        if artifact_path != workspace and workspace not in artifact_path.parents:
            print("command_checker: artifact escapes the workspace", file=sys.stderr)
            raise SystemExit(2)
        if not artifact_path.is_file():
            print(f"command_checker: artifact not found: {artifact}", file=sys.stderr)
            raise SystemExit(2)
        artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    else:
        artifact_hash = hashlib.sha256(result.stdout.encode("utf-8", "replace")).hexdigest()
    print(json.dumps({
        "accepted": result.returncode == 0,
        "checker_id": "command-checker",
        "checker_version": "1.0",
        "artifact_hash": artifact_hash,
        "log_hash": log_hash,
        "details": {"returncode": result.returncode, "stdout_tail": result.stdout[-2000:]},
    }))


if __name__ == "__main__":
    main()
