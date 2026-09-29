"""Workspace-local payload and sandbox helpers (supervisor plane).

A workspace may declare, in ``loop-config.json``, a sandbox: an operator
label (``kind``), a root directory inside the workspace, and argv lists for
setting it up and for running things inside it. The kernel never interprets
the label — the only things that ever execute are the declared argv lists,
so a machine without any particular tool behaves identically until an
operator-declared command runs. ``payload/`` and the sandbox root are
evidence source material, never projections of the event log.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import IntegrityError, ValidationError
from .pathsafe import ensure_within, hash_file, safe_relative
from .storage import atomic_write_text, canonical_json, utc_now

PAYLOAD_DIR = "payload"
DEFAULT_SANDBOX_ROOT = "sandbox"
DEFAULT_SETUP_TIMEOUT_SECONDS = 600
SANDBOX_STATUS_FILE = ".sandbox-status.json"
HEX_256_LENGTH = 64


def require_workspace(workspace: Path) -> Path:
    """A cheap existence check that never creates directories or lock files."""

    workspace = Path(workspace)
    if not (workspace / "events.jsonl").is_file():
        raise IntegrityError("Workspace has no event log", {"workspace": str(workspace)})
    return workspace


def read_loop_config(workspace: Path) -> Dict[str, Any]:
    """Tolerant read of loop-config.json, without constructing an engine."""

    config_path = Path(workspace) / "loop-config.json"
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(
            f"adv-loop: {config_path} is not valid JSON; ignoring the workspace config",
            file=sys.stderr,
        )
        return {}
    return value if isinstance(value, dict) else {}


def _argv_list(value: Any, name: str) -> Optional[list]:
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValidationError(f"{name} must be a non-empty argv list of strings", {"received": value})
    return list(value)


def sandbox_declaration(workspace: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize the sandbox block; anything that could run is validated strictly."""

    workspace = Path(workspace)
    if config is None:
        config = read_loop_config(workspace)
    declared = config.get("sandbox")
    if declared is None:
        declared = {}
        was_declared = False
    else:
        if not isinstance(declared, dict):
            raise ValidationError("loop-config.json sandbox must be an object", {"received": declared})
        was_declared = True
    kind = declared.get("kind", "none")
    if not isinstance(kind, str) or not kind.strip():
        raise ValidationError("sandbox.kind must be a non-empty label", {"received": kind})
    root_name = declared.get("root", DEFAULT_SANDBOX_ROOT)
    if not isinstance(root_name, str):
        raise ValidationError("sandbox.root must be a workspace-relative path", {"received": root_name})
    root = ensure_within(workspace, workspace / safe_relative(root_name))
    timeout = declared.get("setup_timeout_seconds", DEFAULT_SETUP_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValidationError("sandbox.setup_timeout_seconds must be a positive number", {"received": timeout})
    lockfile_hashes = declared.get("lockfile_hashes", {})
    if not isinstance(lockfile_hashes, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or len(value) != HEX_256_LENGTH
        for key, value in lockfile_hashes.items()
    ):
        raise ValidationError(
            "sandbox.lockfile_hashes must map workspace-relative paths to SHA-256 hex digests",
        )
    return {
        "declared": was_declared,
        "kind": kind.strip(),
        "root": root,
        "root_name": str(safe_relative(root_name)),
        "command": _argv_list(declared.get("command"), "sandbox.command"),
        "setup": _argv_list(declared.get("setup"), "sandbox.setup"),
        "setup_timeout_seconds": timeout,
        "lockfile_hashes": dict(lockfile_hashes),
    }


def _verify_lockfiles(workspace: Path, declaration: Dict[str, Any]) -> list:
    rows = []
    for relative, expected in sorted(declaration["lockfile_hashes"].items()):
        path = ensure_within(workspace, workspace / safe_relative(relative))
        row: Dict[str, Any] = {"path": str(safe_relative(relative)), "expected": expected}
        if path.is_file():
            actual, _ = hash_file(path)
            row["actual"] = actual
            row["match"] = actual == expected
        else:
            row["actual"] = None
            row["match"] = False
        rows.append(row)
    return rows


def _read_status_sidecar(workspace: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads((Path(workspace) / SANDBOX_STATUS_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def sandbox_report(workspace: Path, init: bool = False, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Status report on the payload/sandbox dirs; with init, ensure and set up.

    Init is idempotent: directories are created if missing, declared lockfile
    hashes are re-verified, and the declared setup argv (if any) is re-run in
    the sandbox root. Failure is an observation for the operator — it never
    writes a chain event and never changes task status.
    """

    workspace = require_workspace(workspace)
    declaration = sandbox_declaration(workspace)
    payload_dir = workspace / PAYLOAD_DIR
    if init:
        payload_dir.mkdir(exist_ok=True)
        declaration["root"].mkdir(parents=True, exist_ok=True)
    lockfiles = _verify_lockfiles(workspace, declaration)
    report: Dict[str, Any] = {
        "workspace": str(workspace),
        "declared": declaration["declared"],
        "kind": declaration["kind"],
        "root": declaration["root_name"],
        "payload_exists": payload_dir.is_dir(),
        "sandbox_root_exists": declaration["root"].is_dir(),
        "lockfiles": lockfiles,
        "last_setup": _read_status_sidecar(workspace),
        "setup": None,
        "exit_code": 0,
    }
    if init and declaration["setup"]:
        report["setup"] = _run_setup(workspace, declaration, timeout)
        report["last_setup"] = _read_status_sidecar(workspace)
    if any(not row["match"] for row in lockfiles):
        report["exit_code"] = 1
    if report["setup"] is not None and not report["setup"]["ok"]:
        report["exit_code"] = 1
    return report


def _run_setup(workspace: Path, declaration: Dict[str, Any], timeout: Optional[float]) -> Dict[str, Any]:
    command = declaration["setup"]
    effective_timeout = timeout if timeout is not None else declaration["setup_timeout_seconds"]
    outcome: Dict[str, Any] = {
        "at": utc_now(),
        "kind": declaration["kind"],
        "command": command,
        "ran": True,
    }
    try:
        result = subprocess.run(
            command,
            cwd=str(declaration["root"]),
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
        outcome["ok"] = result.returncode == 0
        outcome["returncode"] = result.returncode
        if result.returncode != 0:
            outcome["stderr"] = result.stderr[-2000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        outcome["ok"] = False
        outcome["error"] = str(exc)
    try:
        atomic_write_text(Path(workspace) / SANDBOX_STATUS_FILE, canonical_json(outcome) + "\n")
    except OSError:
        pass
    return outcome
