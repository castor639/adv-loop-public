"""The three GPU tools as one object per session.

`ToolRunner._dispatch` calls `run`, `collect`, or `status` and forwards the
fourth element of the tuple into the ledger row. A problem anywhere below is
data for the model (a refusal with a reason), never an exception, so a GPU
fault cannot end a session that could still finish on CPU.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import DEFAULTS
from .box_api import Box
from .jobs import JobRunner, status_report

ToolTuple = Tuple[Dict[str, Any], str, str, Dict[str, Any]]


class GpuService:
    def __init__(self, box: Box, ws: Path, state_dir: Path, config: Optional[Dict[str, Any]], *,
                 session_id: str, directive_id: Optional[str] = None, role: Optional[str] = None,
                 mode: Optional[str] = None, guard: Any = None, runner: Optional[JobRunner] = None) -> None:
        self.box = box
        self.ws = Path(ws).resolve()
        self.state_dir = Path(state_dir)
        self.config = {**DEFAULTS, **(config or {})}
        self.session_id = session_id
        self.guard = guard
        self.runner = runner or JobRunner(box, self.ws, self.state_dir, self.config, session_id=session_id,
                                          directive_id=directive_id, role=role, mode=mode, guard=guard)

    @staticmethod
    def _failure(exc: Exception) -> ToolTuple:
        message = f"{type(exc).__name__}: {exc}"
        return ({"status": "refused", "reason": "internal_error", "message": message}, "", message,
                {"exit_code": None, "interrupted": False, "error": message})

    def run(self, args: Any, tool_use_id: Optional[str] = None) -> ToolTuple:
        try:
            return self.runner.run(args, tool_use_id=tool_use_id)
        except Exception as exc:
            return self._failure(exc)

    def collect(self, args: Any, tool_use_id: Optional[str] = None) -> ToolTuple:
        try:
            job_id = args.get("job_id") if isinstance(args, dict) else None
            return self.runner.collect(job_id, tool_use_id=tool_use_id)
        except Exception as exc:
            return self._failure(exc)

    def status(self, args: Any, tool_use_id: Optional[str] = None) -> ToolTuple:
        try:
            job_id = args.get("job_id") if isinstance(args, dict) else None
            report = status_report(self.ws, self.state_dir, self.config, self.guard, self.box,
                                   job_id=str(job_id) if job_id is not None else None)
        except Exception as exc:
            return self._failure(exc)
        text = json.dumps(report, sort_keys=True)
        return report, text, "", {"exit_code": None, "interrupted": False}
