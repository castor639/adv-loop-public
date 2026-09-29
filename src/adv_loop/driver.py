from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence

from .engine import LoopEngine
from .errors import AdapterError, LoopError, TransitionError
from .storage import object_hash


Adapter = Callable[[Dict[str, Any]], Dict[str, Any]]


def fault_signature(feedback: List[Dict[str, Any]]) -> str:
    """Hash of the normalized error classes in one exhausted retry burst.

    Digit runs collapse so counters and ids do not split one recurring
    condition into many signatures; the kernel only ever counts repeats of
    the resulting opaque hash.
    """

    classes = sorted({
        (str(item.get("error", "unknown")), re.sub(r"\d+", "0", str(item.get("message", ""))[:200]))
        for item in feedback
    })
    return object_hash([list(pair) for pair in classes])


def drive(
    engine: LoopEngine,
    adapter: Adapter,
    max_cycles: Optional[int] = None,
    adapter_retries: int = 3,
) -> Dict[str, Any]:
    """Drive legal directives until a terminal state, or an explicit supervisor pause."""

    if max_cycles is not None and max_cycles <= 0:
        raise AdapterError("max_cycles must be positive when configured")
    if adapter_retries < 0:
        raise AdapterError("adapter_retries must not be negative")
    try:
        # Standing authorization travels with every directive: the adapter puts
        # the charter in front of the model so pre-granted decisions are never
        # re-asked. Absence is fine; a charter is optional.
        charter = (engine.workspace / "payload" / "charter.md").read_text(encoding="utf-8")[:65536]
    except (OSError, UnicodeDecodeError):
        charter = None
    accepted = 0
    total_rejections = 0
    while True:
        directive = engine.next_instruction()
        if directive["action"] == "stop":
            return {
                "driver_status": "terminal",
                "accepted_attempts": accepted,
                "adapter_rejections": total_rejections,
                "state": engine.load(),
            }
        if directive["action"] == "finalize":
            state = engine.finalize()
            return {
                "driver_status": "terminal",
                "accepted_attempts": accepted,
                "adapter_rejections": total_rejections,
                "state": state,
            }
        if directive["action"] == "await_human":
            return {
                "driver_status": "paused",
                "reason": directive["reason"],
                "accepted_attempts": accepted,
                "adapter_rejections": total_rejections,
                "state": engine.load(),
                "next": directive,
            }
        if directive["action"] != "attempt":
            raise AdapterError("Controller returned an unsupported action", {"directive": directive})
        if max_cycles is not None and accepted >= max_cycles:
            return {
                "driver_status": "paused",
                "reason": "supervisor max_cycles reached; task remains resumable",
                "accepted_attempts": accepted,
                "adapter_rejections": total_rejections,
                "state": engine.load(),
                "next": directive,
                "guard": (
                    "the task is still active and this directive is still owed; "
                    f"`adv-loop guard {engine.workspace}` exits nonzero until it is consumed"
                ),
            }

        feedback: List[Dict[str, Any]] = []
        stale = False
        for retry in range(adapter_retries + 1):
            envelope = {
                "workspace": str(engine.workspace.resolve()),
                "directive": directive,
                "rejections": feedback,
                "retry": retry,
            }
            if charter is not None:
                envelope["charter"] = charter
            try:
                submission = adapter(envelope)
                if not isinstance(submission, dict):
                    raise AdapterError("Adapter output must be a JSON object")
                engine.record_attempt(submission)
                accepted += 1
                break
            except TransitionError as exc:
                current = engine.next_instruction()
                if current.get("directive_id") != directive.get("directive_id"):
                    stale = True
                    break
                feedback.append(exc.as_dict())
                total_rejections += 1
            except LoopError as exc:
                feedback.append(exc.as_dict())
                total_rejections += 1
            except Exception as exc:
                feedback.append({"error": "adapter_invocation", "message": str(exc), "details": {}})
                total_rejections += 1
            if retry == adapter_retries:
                signature = fault_signature(feedback)
                try:
                    # 4.0 workspaces turn the exhausted burst into a chain
                    # observation; enough repeats of one signature schedule a
                    # harness diagnosis instead of a human question. Pre-4.0
                    # workspaces refuse the event and lose nothing.
                    engine.record_process_fault({
                        "request_id": f"fault:{directive['directive_id']}",
                        "signature": signature,
                        "source": "driver:adapter_exhausted",
                        "detail": str(feedback[-1].get("message", "adapter failure"))[:200],
                    })
                except LoopError:
                    pass
                raise AdapterError(
                    "Adapter exhausted its correction retries; task remains active and resumable",
                    {
                        "directive_id": directive["directive_id"],
                        "fault_signature": signature,
                        "rejections": feedback,
                        "guard": (
                            "the directive is still owed; "
                            f"`adv-loop guard {engine.workspace}` exits nonzero until it is consumed"
                        ),
                    },
                )
        if stale:
            continue


class SubprocessAdapter:
    """JSON-over-stdin adapter for any model runner or agent executable."""

    def __init__(self, command: Sequence[str], timeout_seconds: float = 600.0):
        command = list(command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise AdapterError("A subprocess adapter command is required")
        if timeout_seconds <= 0:
            raise AdapterError("Adapter timeout must be positive")
        self.command = command
        self.timeout_seconds = timeout_seconds

    def __call__(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                self.command,
                input=json.dumps(envelope),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterError("Adapter process could not complete", {"command": self.command, "cause": str(exc)}) from exc
        if result.returncode != 0:
            raise AdapterError(
                "Adapter process returned a non-zero exit status",
                {"command": self.command, "returncode": result.returncode, "stderr": result.stderr[-4000:]},
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                "Adapter stdout was not valid JSON",
                {"stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
            ) from exc
        if not isinstance(value, dict):
            raise AdapterError("Adapter JSON must be an object")
        return value
