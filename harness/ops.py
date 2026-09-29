"""Operator actions the console performs, callable locally or through `adv-harness op` on the box.

Each function takes the same JSON-shaped payload the remote console sends and
returns a JSON-shaped result, so the local console and the ssh path run the
identical code. Nothing here bypasses the kernel: answers become chain events
through `provide_human_input`, guidance lands in the supplemental memory the
assembler already reads, and a step is one `supervisor.run_directive` call.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict

from . import pause, supervisor

OPS = ("new", "message", "pause", "resume", "step")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _workspace(root: Path, payload: Dict[str, Any]) -> Path:
    name = str(payload.get("workspace") or "")
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("workspace must be a directory name under the root")
    ws = (Path(root) / name).resolve()
    if ws.parent != Path(root).resolve() or not (ws / "events.jsonl").is_file():
        raise ValueError(f"no such workspace: {name}")
    return ws


def op_new(root: Path, state: Path, repo: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    from .tui import create_workspace
    ws = create_workspace(Path(root), str(payload.get("task", "")), str(payload.get("criteria", "")),
                          str(payload.get("attempts", "20")))
    return {"workspace": ws.name, "message": "Workspace created. Click Run step to start the agent."}


def op_message(root: Path, state: Path, repo: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    from .tui import submit_message
    ws = _workspace(root, payload)
    return {"workspace": ws.name,
            "message": submit_message(ws, str(payload.get("text", "")), payload.get("question"))}


def op_pause(root: Path, state: Path, repo: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    ws = _workspace(root, payload)
    pause.human_hold(ws, str(payload.get("note") or "Paused from the console"))
    return {"workspace": ws.name, "message": "Paused future sessions for " + ws.name}


def op_resume(root: Path, state: Path, repo: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    ws = _workspace(root, payload)
    record = pause.read(ws / pause.PAUSE_FILE) or {}
    if record.get("reason") != "human_hold":
        return {"workspace": ws.name, "message": "Only an operator hold can be cleared here."}
    pause.clear(ws / pause.PAUSE_FILE)
    return {"workspace": ws.name, "message": "Operator hold cleared. Click Run step to continue."}


def op_step(root: Path, state: Path, repo: Path, payload: Dict[str, Any],
            *, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    ws = _workspace(root, payload)
    root, state = Path(root), Path(state)
    if (root / supervisor.FLEET_STOP).is_file():
        raise ValueError("Fleet stop file is active")
    runtime = supervisor.Runtime(root=root, state_dir=state, repo=Path(repo) if repo else None)
    runtime.api_guard.refresh(root, extra_ledgers=[state / ".spend.jsonl"])
    if not runtime.aws_guard.allows_sessions():
        raise ValueError("AWS budget stop is active")
    if not supervisor.workspace_ready(ws):
        supervisor.provision_workspace(runtime, ws)
        until = time.monotonic() + float(payload.get("setup_wait_seconds", 15))
        while not supervisor.workspace_ready(ws):
            ready = _read_json(ws / supervisor.READY_FILE)
            if ready.get("sandbox_init") == "failed":
                raise ValueError("Sandbox setup failed; inspect .harness/ready.json")
            if time.monotonic() >= until:
                return {"workspace": ws.name, "message": "Setup still running. Click Run step again after it finishes."}
            sleep(0.2)
    if pause.workspace_paused(ws):
        return {"workspace": ws.name, "message": "Workspace paused before the session began."}
    result = supervisor.run_directive(runtime, ws, max_cycles=1)
    return {"workspace": ws.name, "result": result,
            "message": (f"Step finished: {result.get('accepted', 0)} accepted. "
                        + str(result.get("reason") or result.get("driver_status", "saved"))
                        + ". See Conversation for recorded work.")}


HANDLERS = {"new": op_new, "message": op_message, "pause": op_pause, "resume": op_resume, "step": op_step}


def run(name: str, root: Path, state: Path, repo: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    if name not in HANDLERS:
        raise ValueError(f"unknown op {name!r}; one of {OPS}")
    return HANDLERS[name](root, state, repo, payload)
