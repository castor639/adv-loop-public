#!/usr/bin/env python3
"""SMT discharge checker: accepted iff z3 reports `unsat` for the goal file.

The convention: encode the *negation* of the claim in SMT-LIB2; `unsat` means
no counterexample exists under the stated encoding, which is what the
smt_discharge rank asserts. Register as a validator hook (rank smt_discharge)
and pass the file via --input: {"file": "payload/scratch/goal.smt2"}.
Requires z3 on PATH; a missing solver is a hook failure, never a verdict.
"""

import hashlib
import json
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
        print("z3_discharge: input.file must name the .smt2 goal file", file=sys.stderr)
        raise SystemExit(2)
    file_path = (workspace / target).resolve()
    if workspace not in file_path.parents or not file_path.is_file():
        print(f"z3_discharge: file not found inside the workspace: {target}", file=sys.stderr)
        raise SystemExit(2)
    if not shutil.which("z3"):
        print("z3_discharge: z3 is not on PATH", file=sys.stderr)
        raise SystemExit(2)
    probe = subprocess.run(["z3", "--version"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        print(f"z3_discharge: solver does not run: {probe.stderr.strip()[:200]}", file=sys.stderr)
        raise SystemExit(2)
    timeout = float(config.get("timeout_seconds", 300))
    result = subprocess.run(["z3", str(file_path)], capture_output=True, text=True,
                            timeout=timeout, check=False)
    verdict = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    print(json.dumps({
        "accepted": verdict == "unsat",
        "checker_id": "z3-discharge",
        "checker_version": "1.0",
        "artifact_hash": hashlib.sha256(file_path.read_bytes()).hexdigest(),
        "log_hash": hashlib.sha256(
            (result.stdout + "\n---\n" + result.stderr).encode("utf-8", "replace")
        ).hexdigest(),
        "details": {"solver_output": verdict, "returncode": result.returncode},
    }))


if __name__ == "__main__":
    main()
