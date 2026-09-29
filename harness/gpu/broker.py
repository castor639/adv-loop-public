"""File broker for GPU tools in claude_code sessions.

Claude Code's tool list is fixed, so a session reaches the GPU through the
`adv-gpu-run` shim, which drops a request file into `<ws>/.harness/gpu` and
waits for the answer. The broker on the host serves it with the session's
GpuService and writes the authoritative ledger row itself, so the row exists
whether or not the shim lived to print the result. A `run` blocks for hours,
so it gets its own thread and the poll loop keeps answering `status`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adv_loop.storage import atomic_write_text

from .. import ledger

REQUEST_KINDS = ("run", "collect", "status")
GPU_DIR = ".harness/gpu"


class GpuBroker:
    def __init__(self, service: Any, gpu_dir: Path, session_id: str, *, ledger_path: Optional[Path] = None,
                 ledger_append: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        if ledger_append is None and ledger_path is None:
            raise ValueError("GpuBroker needs a ledger_path or a ledger_append callable")
        self.service = service
        self.gpu_dir = Path(gpu_dir)
        self.session_id = session_id
        self.ledger_append = ledger_append or (lambda record: ledger.append(Path(ledger_path), record))
        self.served: List[str] = []
        self.threads: List[threading.Thread] = []
        self._seen: set = set()
        self._lock = threading.Lock()

    def _answer(self, request_path: Path, request_id: str, result: Dict[str, Any]) -> None:
        atomic_write_text(self.gpu_dir / f"{request_id}.result.json", json.dumps(result, sort_keys=True) + "\n")
        try:
            request_path.rename(self.gpu_dir / f"{request_id}.request.done")
        except OSError:
            pass

    def serve(self, request_path: Path) -> Optional[Dict[str, Any]]:
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(request, dict):
            return None
        request_id = str(request.get("request_id") or request_path.name.split(".")[0])
        if request.get("session_id") != self.session_id:
            result = {"error": "foreign_session", "request_id": request_id}
            self._answer(request_path, request_id, result)
            return result
        kind = request.get("kind")
        if kind not in REQUEST_KINDS:
            result = {"error": "unknown_kind", "request_id": request_id, "kind": kind}
            self._answer(request_path, request_id, result)
            return result
        args = request.get("args")
        args = args if isinstance(args, dict) else {}
        if kind == "run":
            thread = threading.Thread(target=self._serve, args=(request_path, request_id, kind, args),
                                      daemon=True, name=f"gpu-run-{request_id[:8]}")
            with self._lock:
                self.threads.append(thread)
            thread.start()
            return None
        return self._serve(request_path, request_id, kind, args)

    def _serve(self, request_path: Path, request_id: str, kind: str, args: Dict[str, Any]) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            result, stdout, stderr, extra = getattr(self.service, kind)(args, tool_use_id=request_id)
        except Exception as exc:  # the service already turns faults into refusals; this is the last net
            message = f"{type(exc).__name__}: {exc}"
            result, stdout, stderr, extra = ({"status": "refused", "reason": "internal_error", "message": message},
                                             "", message, {"error": message})
        duration = int((time.monotonic() - started) * 1000)
        record = ledger.entry(f"gpu_{kind}", args, stdout=stdout, stderr=stderr, tool_use_id=request_id,
                              cwd=str(getattr(self.service, "ws", "")) or None, duration_ms=duration, **extra)
        try:
            self.ledger_append(record)
        except Exception:
            pass
        with self._lock:
            self.served.append(request_id)
        self._answer(request_path, request_id, result)
        return result

    def poll(self) -> int:
        if not self.gpu_dir.is_dir():
            return 0
        count = 0
        for path in sorted(self.gpu_dir.glob("*.request.json")):
            if path.name in self._seen:
                continue
            self._seen.add(path.name)
            self.serve(path)
            count += 1
        return count

    def join(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(timeout)


class GpuBrokerThread(threading.Thread):
    def __init__(self, broker: GpuBroker, interval: float = 0.5) -> None:
        super().__init__(daemon=True, name=f"gpu-broker-{broker.session_id[:8]}")
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
        try:
            self.broker.poll()
        except Exception:
            pass

    def stop(self, timeout: float = 10.0) -> None:
        self._halt.set()
        self.join(timeout)
        self.broker.join(timeout)
