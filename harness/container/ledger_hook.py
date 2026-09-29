#!/usr/bin/env python3
"""Claude Code PostToolUse / PostToolUseFailure hook: append one ledger record.

Reads the hook payload on stdin, writes one JSON line to $ADV_LOOP_LEDGER under
an exclusive lock, and always exits 0 so a ledger problem never blocks the
model. Uses harness.ledger when the repo mount is importable so the record
shape is identical to what the http_chat backend writes; otherwise falls back
to the same fields computed here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import time

REPO = os.environ.get("ADV_LOOP_REPO", "/srv/adv-loop/repo")
TAIL = 2000


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _file_sha(path: str):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _fallback_entry(payload: dict, stdout: str, stderr: str, file_sha, error, at: str) -> dict:
    exit_code = None
    for line in reversed(stdout.splitlines()):
        if line.startswith("EXIT="):
            try:
                exit_code = int(line[5:].strip())
            except ValueError:
                pass
            break
    response = payload.get("tool_response") or {}
    record = {
        "at": at,
        "tool_use_id": payload.get("tool_use_id"),
        "tool": payload.get("tool_name"),
        "cwd": payload.get("cwd"),
        "input": payload.get("tool_input") or {},
        "stdout_tail": stdout[-TAIL:],
        "stderr_tail": stderr[-TAIL:],
        "stdout_sha256": _sha(stdout),
        "stderr_sha256": _sha(stderr),
        "stdout_bytes": len(stdout.encode("utf-8", "replace")),
        "stderr_bytes": len(stderr.encode("utf-8", "replace")),
        "exit_code": exit_code,
        "interrupted": bool(response.get("interrupted")) if isinstance(response, dict) else False,
        "duration_ms": None,
    }
    if file_sha:
        record["file_sha256"] = file_sha
    if error:
        record["error"] = error
    return record


def main() -> int:
    ledger_path = os.environ.get("ADV_LOOP_LEDGER")
    if not ledger_path:
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    response = payload.get("tool_response") or {}
    if isinstance(response, str):
        stdout, stderr = response, ""
    else:
        stdout = str(response.get("stdout") or response.get("content") or "")
        stderr = str(response.get("stderr") or "")
    error = payload.get("error") if payload.get("hook_event_name") == "PostToolUseFailure" else None
    tool_input = payload.get("tool_input") or {}
    file_sha = None
    if payload.get("tool_name") in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if target:
            file_sha = _file_sha(target)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    record = None
    try:
        sys.path.insert(0, REPO)
        from harness import ledger  # type: ignore

        record = ledger.entry(
            payload.get("tool_name") or "unknown", tool_input, stdout=stdout, stderr=stderr,
            tool_use_id=payload.get("tool_use_id"), cwd=payload.get("cwd"),
            interrupted=bool(response.get("interrupted")) if isinstance(response, dict) else False,
            file_sha256=file_sha, error=error, at=at,
        )
    except Exception:
        record = _fallback_entry(payload, stdout, stderr, file_sha, error, at)

    try:
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
