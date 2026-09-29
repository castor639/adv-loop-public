"""Host-side validate broker.

A session inside the container asks for a checker run by dropping a request
file; the broker runs the kernel's validate_report on the host (which signs
with `.checker-key`, a file the container cannot read), keeps the full report
in memory and under the host-only directory, and hands the container a
verdict carrying a session-scoped `verdict_id`. Evidence verification later
copies the checker record from the broker's memory, never from anything the
model wrote.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adv_loop.storage import atomic_write_text
from adv_loop.validators import validate_report

VALIDATE_DIR = ".harness/validate"
HOST_DIR = ".harness/host"

Validator = Callable[..., Dict[str, Any]]


class Broker:
    def __init__(self, workspace: Path, session_id: str, *, validator: Validator = validate_report,
                 allowed_hooks: Optional[List[str]] = None) -> None:
        self.workspace = workspace
        self.session_id = session_id
        self.validator = validator
        self.allowed_hooks = allowed_hooks
        self.request_dir = workspace / VALIDATE_DIR
        self.host_dir = workspace / HOST_DIR / "sessions" / session_id
        self.verdicts: Dict[str, Dict[str, Any]] = {}
        self.served: List[str] = []
        self._seen: set = set()
        self._lock = threading.Lock()

    def _next_id(self) -> str:
        return f"V{len(self.verdicts) + 1:04d}"

    def serve(self, request_path: Path) -> Optional[Dict[str, Any]]:
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        request_id = request.get("request_id") or request_path.name.split(".")[0]
        if request.get("session_id") not in (None, self.session_id):
            answer = {"ok": False, "request_id": request_id, "error": "request is not from the active session"}
            self._answer(request_path, request_id, answer)
            return answer
        answer = self.request(request.get("hook"), request.get("input"), request_id=request_id)
        self._answer(request_path, request_id, answer)
        return answer

    def request(self, hook: Any, input_value: Any, *, request_id: Optional[str] = None) -> Dict[str, Any]:
        """Run one checker for this session; the in-process path used by the http_chat validate tool."""

        request_id = request_id or f"inproc-{len(self.verdicts) + 1}"
        if not isinstance(hook, str) or (self.allowed_hooks is not None and hook not in self.allowed_hooks):
            return {"ok": False, "accepted": False, "request_id": request_id, "error": f"hook {hook!r} is not registered"}
        try:
            report = self.validator(self.workspace, hook, input_value)
        except Exception as exc:  # the kernel raises on contract errors; the model gets the message, not a crash
            report = {"ok": False, "failure": {"message": str(exc)}, "exit_code": 1}
        with self._lock:
            verdict_id = self._next_id()
            record = {"verdict_id": verdict_id, "session_id": self.session_id, "hook": hook,
                      "request_id": request_id, "input": input_value, "report": report,
                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self.verdicts[verdict_id] = record
        self._persist(record)
        return self._model_view(record)

    @staticmethod
    def _model_view(record: Dict[str, Any]) -> Dict[str, Any]:
        report = record["report"]
        view: Dict[str, Any] = {"verdict_id": record["verdict_id"], "hook": record["hook"],
                                "request_id": record["request_id"], "ok": bool(report.get("ok"))}
        if report.get("ok"):
            result = dict(report.get("result", {}))
            result.pop("attestation", None)
            view["accepted"] = bool(result.get("accepted"))
            view["result"] = result
            hint = dict(report.get("evidence_hint", {}))
            hint.pop("checker", None)
            view["evidence_hint"] = hint
        else:
            view["accepted"] = False
            view["failure"] = report.get("failure")
        return view

    def _answer(self, request_path: Path, request_id: str, answer: Dict[str, Any]) -> None:
        atomic_write_text(self.request_dir / f"{request_id}.verdict.json", json.dumps(answer, sort_keys=True) + "\n")
        try:
            request_path.rename(self.request_dir / f"{request_id}.request.done")
        except OSError:
            pass

    def _persist(self, record: Dict[str, Any]) -> None:
        self.host_dir.mkdir(parents=True, exist_ok=True)
        with open(self.host_dir / "verdicts.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def poll(self) -> int:
        if not self.request_dir.is_dir():
            return 0
        count = 0
        for path in sorted(self.request_dir.glob("*.request.json")):
            if path.name in self._seen:
                continue
            self._seen.add(path.name)
            self.serve(path)
            count += 1
        return count

    def session_verdicts(self) -> Dict[str, Dict[str, Any]]:
        """What ledger.verify_evidence consumes: verdict_id -> validate_report output."""

        return {vid: rec["report"] for vid, rec in self.verdicts.items() if rec["report"].get("ok")}


class BrokerThread(threading.Thread):
    def __init__(self, broker: Broker, interval: float = 0.5) -> None:
        super().__init__(daemon=True, name=f"broker-{broker.session_id[:8]}")
        self.broker = broker
        self.interval = interval
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.broker.poll()
            except Exception:
                pass
            self._halt.wait(self.interval)
        self.broker.poll()

    def stop(self, timeout: float = 10.0) -> None:
        self._halt.set()
        self.join(timeout)
