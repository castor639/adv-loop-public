"""The tools an http_chat session may call, and the runner that records them.

Every call lands in the session ledger before its result goes back to the
model, so the attempt's observation is derived from what actually ran.
Execution goes through an `Exec` callable: a local subprocess for tests and
drills, `docker exec` into the workspace container in production.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adv_loop.pathsafe import ensure_within, hash_file

from . import ledger
from .gpu import TOOL_DEFINITIONS as GPU_TOOL_DEFINITIONS
from .gpu import TOOL_NAMES as GPU_TOOL_NAMES

DEFAULT_TOOL_TIMEOUT = 600.0
OUTPUT_TAIL = 12000
READ_LIMIT = 200_000

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "run_in_sandbox",
        "description": (
            "Run an argv command inside the workspace container. cwd is relative to the workspace root"
            " (default '.'; use 'sandbox' for the toolchain, 'payload/scratch' for attempts). Append"
            " '; echo EXIT=$?' when you run through bash -c so the exit status is recorded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Write a UTF-8 text file inside the workspace (path relative to the workspace root). Returns its sha256.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file inside the workspace (path relative to the workspace root), up to 200000 bytes.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hash_artifact",
        "description": "SHA-256 of a file inside the workspace. For information only: evidence names artifact_path and the harness hashes it.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate",
        "description": (
            "Run a registered checker hook (for example lean-kernel, z3, cert-replay) through the host broker."
            " Returns a verdict with a verdict_id; cite that id as evidence.verdict_id. Only this counts as a checker acceptance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"hook": {"type": "string"}, "input": {"type": "object"}},
            "required": ["hook"],
            "additionalProperties": False,
        },
    },
]

Exec = Callable[[List[str], Path, float], "subprocess.CompletedProcess[str]"]


def tool_definitions(*, gpu: bool = False, allowed=None) -> List[Dict[str, Any]]:
    """What a session is offered: the base tools, the GPU tools only with a grant, narrowed by `allowed`."""

    defs = list(TOOL_DEFINITIONS)
    if gpu:
        defs += list(GPU_TOOL_DEFINITIONS)
    if allowed is not None:
        wanted = set(allowed)
        defs = [d for d in defs if d["name"] in wanted]
    return defs


def local_exec(argv: List[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)


def container_exec(docker, name: str) -> Exec:
    def run(argv: List[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
        return docker.exec(name, cwd, argv, timeout=int(timeout))
    return run


class ToolRunner:
    def __init__(self, workspace: Path, ledger_path: Path, *, exec_fn: Exec = local_exec,
                 broker=None, default_timeout: float = DEFAULT_TOOL_TIMEOUT, allowed=None, gpu=None) -> None:
        self.workspace = workspace.resolve()
        self.ledger_path = ledger_path
        self.exec_fn = exec_fn
        self.broker = broker
        self.default_timeout = default_timeout
        self.allowed = set(allowed) if allowed is not None else None
        # A GpuService, present only when the workspace holds the grant; its absence is the refusal.
        self.gpu = gpu
        self.entries: List[Dict[str, Any]] = []

    @property
    def definitions(self) -> List[Dict[str, Any]]:
        return tool_definitions(gpu=self.gpu is not None, allowed=self.allowed)

    def _record(self, record: Dict[str, Any]) -> None:
        self.entries.append(record)
        ledger.append(self.ledger_path, record)

    def _inside(self, relative: str) -> Path:
        return ensure_within(self.workspace, self.workspace / relative)

    def run(self, name: str, args: Dict[str, Any], tool_use_id: Optional[str] = None) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            outcome, stdout, stderr, extra = self._dispatch(name, args, tool_use_id)
        except Exception as exc:  # tool failure is information for the model, never a crash
            outcome, stdout, stderr, extra = {"error": str(exc)}, "", str(exc), {"error": str(exc)}
        duration = int((time.monotonic() - started) * 1000)
        self._record(ledger.entry(name, args, stdout=stdout, stderr=stderr, tool_use_id=tool_use_id,
                                  cwd=str(self.workspace), duration_ms=duration, **extra))
        return outcome

    def _dispatch(self, name: str, args: Dict[str, Any], tool_use_id: Optional[str] = None):
        if self.allowed is not None and name not in self.allowed:
            raise PermissionError(f"tool {name} is not available in this session (read-only)")
        if name == "run_in_sandbox":
            command = args.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
                raise ValueError("command must be a non-empty argv list of strings")
            cwd = self._inside(str(args.get("cwd") or "."))
            timeout = float(args.get("timeout_seconds") or self.default_timeout)
            try:
                done = self.exec_fn(command, cwd, timeout)
            except subprocess.TimeoutExpired as exc:
                out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                err = (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                return ({"error": f"timed out after {timeout}s", "stdout": out[-OUTPUT_TAIL:], "stderr": err[-OUTPUT_TAIL:]},
                        out, err, {"interrupted": True})
            return ({"returncode": done.returncode, "stdout": done.stdout[-OUTPUT_TAIL:], "stderr": done.stderr[-OUTPUT_TAIL:]},
                    done.stdout, done.stderr, {})
        if name == "write_file":
            path = self._inside(str(args["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args["content"]), encoding="utf-8")
            digest, size = hash_file(path)
            return {"path": args["path"], "sha256": digest, "bytes": size}, "", "", {"file_sha256": digest}
        if name == "read_file":
            path = self._inside(str(args["path"]))
            if not path.is_file():
                raise FileNotFoundError(f"no such file: {args['path']}")
            data = path.read_bytes()[:READ_LIMIT]
            text = data.decode("utf-8", errors="replace")
            return {"path": args["path"], "content": text, "truncated": path.stat().st_size > READ_LIMIT}, text, "", {}
        if name == "hash_artifact":
            path = self._inside(str(args["path"]))
            if not path.is_file():
                raise FileNotFoundError(f"no such file: {args['path']}")
            digest, size = hash_file(path)
            return {"path": args["path"], "sha256": digest, "bytes": size}, digest, "", {}
        if name == "validate":
            if self.broker is None:
                raise RuntimeError("no checker broker is attached to this session")
            view = self.broker.request(str(args["hook"]), args.get("input"))
            text = json.dumps(view, sort_keys=True)
            return view, text, "", {}
        if name in GPU_TOOL_NAMES:
            if self.gpu is None:
                raise PermissionError("GPU compute is not granted for this workspace")
            return getattr(self.gpu, name[4:])(args, tool_use_id=tool_use_id)
        raise ValueError(f"unknown tool: {name}")


def openai_tools(defs: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}}
            for t in (TOOL_DEFINITIONS if defs is None else defs)]


def anthropic_tools(defs: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in (TOOL_DEFINITIONS if defs is None else defs)]
