#!/usr/bin/env python3
"""Wrap a checker so its verdict carries `toolchain_hash`.

Usage in a validator hook: `python3 toolchain_stamp.py <checker argv...>`.
The envelope on stdin is passed through unchanged; the inner verdict on stdout
gains `toolchain_hash` from $ADV_LOOP_TOOLCHAIN_HASH when the checker did not
set one. Exit code and stderr pass through, so a hook failure stays a failure.
The shipped checkers are never edited; the kernel's `pin_toolchain_hash`
overlay then rejects any verdict from a drifted container.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ENV = "ADV_LOOP_TOOLCHAIN_HASH"


def main(argv):
    if not argv:
        sys.stderr.write("toolchain_stamp: missing checker argv\n")
        return 2
    payload = sys.stdin.read()
    done = subprocess.run(argv, input=payload, text=True, capture_output=True)
    sys.stderr.write(done.stderr)
    if done.returncode != 0:
        sys.stdout.write(done.stdout)
        return done.returncode
    try:
        verdict = json.loads(done.stdout)
    except ValueError:
        sys.stdout.write(done.stdout)
        return done.returncode
    stamp = os.environ.get(ENV, "").strip().lower()
    if isinstance(verdict, dict) and stamp and not verdict.get("toolchain_hash"):
        verdict["toolchain_hash"] = stamp
    sys.stdout.write(json.dumps(verdict, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
