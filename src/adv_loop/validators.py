"""Checker-adapter runner: registered validator hooks produce attested verdicts.

A hook is an external checker (a proof kernel, a solver, a spec runner — the
kernel neither knows nor cares which) reached the same way any adapter is:
argv subprocess, JSON envelope in, one JSON verdict out. The verdict is an
attestation: the engine later verifies its shape and hashes when it lands as
evidence, never its truth. Hooks adopted through a workspace overlay live in
replayed state and win over machine-local ``loop-config.json`` entries.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from .driver import SubprocessAdapter
from .engine import CHECKER_KEY_FILE, HEX_256, LoopEngine, SAFE_ID, checker_attestation_mac
from .errors import AdapterError, ValidationError
from .policy import FORMALIZATION_RANKS
from .sandbox import PAYLOAD_DIR, read_loop_config, require_workspace, sandbox_declaration

VALIDATE_PROTOCOL = "adv-loop-validate/1"
DEFAULT_HOOK_TIMEOUT_SECONDS = 300
VERDICT_REQUIRED = {"accepted", "checker_id", "checker_version", "artifact_hash", "log_hash"}
VERDICT_ALLOWED = VERDICT_REQUIRED | {"details", "toolchain_hash"}


def _config_hook(workspace: Path, name: str) -> Optional[Dict[str, Any]]:
    hooks = read_loop_config(workspace).get("validators", {})
    if not isinstance(hooks, dict):
        raise ValidationError("loop-config.json validators must be an object of named hooks")
    declared = hooks.get(name)
    if declared is None:
        return None
    if not isinstance(declared, dict):
        raise ValidationError("A validator hook must be an object", {"hook": name})
    unknown = set(declared) - {"command", "timeout_seconds", "in_sandbox", "rank"}
    if unknown:
        raise ValidationError("Validator hook contains unknown fields",
                              {"hook": name, "unknown": sorted(unknown)})
    command = declared.get("command")
    if (not isinstance(command, list) or not command
            or any(not isinstance(item, str) or not item.strip() for item in command)):
        raise ValidationError("Validator hook command must be a non-empty argv list", {"hook": name})
    timeout = declared.get("timeout_seconds", DEFAULT_HOOK_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValidationError("Validator hook timeout_seconds must be positive", {"hook": name})
    in_sandbox = declared.get("in_sandbox", False)
    if not isinstance(in_sandbox, bool):
        raise ValidationError("Validator hook in_sandbox must be a boolean", {"hook": name})
    rank = declared.get("rank")
    if rank is not None and rank not in FORMALIZATION_RANKS:
        raise ValidationError("Validator hook rank is unknown",
                              {"hook": name, "allowed": list(FORMALIZATION_RANKS)})
    resolved = {"command": list(command), "timeout_seconds": timeout, "rank": rank, "source": "loop-config"}
    if in_sandbox:
        declaration = sandbox_declaration(workspace)
        if not declaration["command"]:
            raise ValidationError(
                "Hook declares in_sandbox but the workspace sandbox has no command",
                {"hook": name},
            )
        resolved["command"] = declaration["command"] + resolved["command"]
    return resolved


def _state_hook(state: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for entry in state.get("validator_hooks", []):
        if entry["hook_id"] == name:
            return {
                "command": list(entry["command"]),
                "timeout_seconds": entry["timeout_seconds"],
                "rank": entry["rank"],
                "source": f"overlay:{entry['overlay_id']}",
            }
    return None


def _validate_verdict(verdict: Any) -> Dict[str, Any]:
    if not isinstance(verdict, dict):
        raise ValidationError("Checker verdict must be a JSON object")
    missing = VERDICT_REQUIRED - set(verdict)
    unknown = set(verdict) - VERDICT_ALLOWED
    if missing or unknown:
        raise ValidationError(
            "Checker verdict violates the validate contract",
            {"missing": sorted(missing), "unknown": sorted(unknown)},
        )
    if not isinstance(verdict["accepted"], bool):
        raise ValidationError("Checker verdict accepted must be a boolean")
    if not isinstance(verdict["checker_id"], str) or not SAFE_ID.fullmatch(verdict["checker_id"]):
        raise ValidationError("Checker verdict checker_id must be a safe identifier")
    if not isinstance(verdict["checker_version"], str) or not verdict["checker_version"].strip():
        raise ValidationError("Checker verdict checker_version must be a non-empty string")
    for field in ("artifact_hash", "log_hash") + (("toolchain_hash",) if "toolchain_hash" in verdict else ()):
        value = verdict.get(field)
        if not isinstance(value, str) or not HEX_256.fullmatch(value.lower()):
            raise ValidationError(f"Checker verdict {field} must be a SHA-256 hex digest")
        verdict[field] = value.lower()
    if "details" in verdict and not isinstance(verdict["details"], dict):
        raise ValidationError("Checker verdict details must be an object")
    return verdict


def validate_report(
    workspace: Path,
    hook_name: str,
    input_value: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    workspace = require_workspace(workspace)
    if not isinstance(hook_name, str) or not SAFE_ID.fullmatch(hook_name):
        raise ValidationError("Hook name must be a safe identifier", {"hook": hook_name})
    state = LoopEngine(workspace).load()
    hook = _state_hook(state, hook_name) or _config_hook(workspace, hook_name)
    if hook is None:
        raise ValidationError(
            "Unknown validator hook: register it in loop-config.json or adopt it via an overlay",
            {"hook": hook_name},
        )
    declaration = sandbox_declaration(workspace)
    envelope = {
        "protocol": VALIDATE_PROTOCOL,
        "workspace": str(workspace.resolve()),
        "hook": hook_name,
        "payload_root": str((workspace / PAYLOAD_DIR).resolve()),
        "sandbox": {
            "kind": declaration["kind"],
            "root": str(declaration["root"]),
            "command": declaration["command"],
        } if declaration["declared"] else None,
        "input": input_value,
        "config": {"rank": hook["rank"], "source": hook["source"]},
    }
    effective_timeout = timeout if timeout is not None else hook["timeout_seconds"]
    started = time.monotonic()
    try:
        verdict = _validate_verdict(SubprocessAdapter(hook["command"], effective_timeout)(envelope))
        # The runner — not the hook, not the adapter — signs the verdict when
        # this workspace holds an attestation key (adv-loop keygen).
        try:
            key_hex = (workspace / CHECKER_KEY_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            key_hex = None
        if key_hex and HEX_256.fullmatch(key_hex):
            verdict["attestation"] = checker_attestation_mac(key_hex, verdict)
    except (AdapterError, ValidationError) as exc:
        return {
            "workspace": str(workspace),
            "hook": hook_name,
            "ok": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "failure": exc.as_dict(),
            "exit_code": 1,
        }
    hint: Dict[str, Any] = {
        "kind": "checker-attested",
        "quality": "direct",
        "fingerprint": verdict["artifact_hash"],
        "locator": f"validate:{hook_name}:{verdict['checker_id']}@{verdict['checker_version']}",
        "method": f"registered validator hook '{hook_name}'",
        "checker": {key: verdict[key] for key in verdict if key != "details"},
    }
    if hook["rank"] is not None and verdict["accepted"]:
        hint["formalization_rank"] = hook["rank"]
    return {
        "workspace": str(workspace),
        "hook": hook_name,
        "ok": True,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "result": verdict,
        "evidence_hint": hint,
        "exit_code": 0,
    }
