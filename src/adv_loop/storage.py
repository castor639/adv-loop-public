from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .errors import IdempotencyConflict, IntegrityError

try:  # pragma: no cover - exercised on POSIX in the test suite
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore


GENESIS_HASH = "0" * 64


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Write and fsync a file before atomically replacing its projection."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class EventStore:
    """Single-writer, hash-chained event log with a tiny write-ahead journal."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.events_path = self.workspace / "events.jsonl"
        self.pending_path = self.workspace / ".pending-event.json"
        self.lock_path = self.workspace / ".adv-loop.lock"

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self, recover_pending: bool = True) -> List[Dict[str, Any]]:
        with self.lock():
            if recover_pending:
                self.recover_pending_unlocked()
            return self.read_unlocked()

    def read_unlocked(self) -> List[Dict[str, Any]]:
        if not self.events_path.exists():
            return []
        raw = self.events_path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise IntegrityError(
                "Event log has a torn trailing record",
                {"path": str(self.events_path), "repairable_with_pending_journal": self.pending_path.exists()},
            )
        events: List[Dict[str, Any]] = []
        previous_hash = GENESIS_HASH
        for index, raw_line in enumerate(raw.splitlines(), start=1):
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise IntegrityError("Event log contains invalid JSON", {"line": index}) from exc
            self._validate_event(event, index, previous_hash)
            events.append(event)
            previous_hash = event["hash"]
        return events

    def append_unlocked(
        self,
        event_type: str,
        payload: Dict[str, Any],
        request_id: str,
        at: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """Append exactly once. Returns (event, created). Caller must hold the lock."""

        self.recover_pending_unlocked()
        events = self.read_unlocked()
        for event in events:
            if event["request_id"] != request_id:
                continue
            if event["type"] == event_type and event["payload"] == payload:
                return event, False
            raise IdempotencyConflict(
                f"Request id {request_id!r} was already used for different content",
                {"existing_event": event["seq"], "existing_type": event["type"]},
            )

        unsigned = {
            "seq": len(events) + 1,
            "at": at or utc_now(),
            "type": event_type,
            "request_id": request_id,
            "payload": payload,
            "prev_hash": events[-1]["hash"] if events else GENESIS_HASH,
        }
        event = {**unsigned, "hash": object_hash(unsigned)}
        atomic_write_text(self.pending_path, canonical_json(event) + "\n")
        self._append_exact_unlocked(event)
        self.pending_path.unlink()
        _fsync_directory(self.workspace)
        return event, True

    def recover_pending_unlocked(self) -> bool:
        """Finish or acknowledge the one event whose durable append was interrupted."""

        if not self.pending_path.exists():
            return False
        try:
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("Pending event journal is corrupt", {"path": str(self.pending_path)}) from exc

        self._repair_torn_tail_from_pending(pending)
        events = self.read_unlocked()
        if events and events[-1]["hash"] == pending.get("hash"):
            self.pending_path.unlink()
            _fsync_directory(self.workspace)
            return True

        expected_seq = len(events) + 1
        expected_previous = events[-1]["hash"] if events else GENESIS_HASH
        self._validate_event(pending, expected_seq, expected_previous)
        self._append_exact_unlocked(pending)
        self.pending_path.unlink()
        _fsync_directory(self.workspace)
        return True

    def _repair_torn_tail_from_pending(self, pending: Dict[str, Any]) -> None:
        if not self.events_path.exists():
            return
        raw = self.events_path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        last_newline = raw.rfind(b"\n")
        prefix_end = last_newline + 1
        torn = raw[prefix_end:]
        intended = (canonical_json(pending) + "\n").encode("utf-8")
        if not intended.startswith(torn):
            raise IntegrityError(
                "Torn event tail does not match the pending journal",
                {"path": str(self.events_path), "tail_bytes": len(torn)},
            )
        recovery_dir = self.workspace / ".recovery"
        recovery_dir.mkdir(exist_ok=True)
        archive = recovery_dir / f"torn-tail-{pending.get('seq', 'unknown')}.bin"
        if not archive.exists():
            archive.write_bytes(torn)
        with self.events_path.open("r+b") as handle:
            handle.truncate(prefix_end)
            handle.flush()
            os.fsync(handle.fileno())

    def _append_exact_unlocked(self, event: Dict[str, Any]) -> None:
        line = (canonical_json(event) + "\n").encode("utf-8")
        descriptor = os.open(str(self.events_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            written = 0
            while written < len(line):
                written += os.write(descriptor, line[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_event(event: Dict[str, Any], expected_seq: int, expected_previous: str) -> None:
        required = {"seq", "at", "type", "request_id", "payload", "prev_hash", "hash"}
        if not isinstance(event, dict) or set(event) != required:
            raise IntegrityError("Event has an invalid envelope", {"expected_keys": sorted(required)})
        if event["seq"] != expected_seq:
            raise IntegrityError("Event sequence is not contiguous", {"expected": expected_seq, "actual": event["seq"]})
        if event["prev_hash"] != expected_previous:
            raise IntegrityError("Event hash chain is broken", {"event": expected_seq})
        if not isinstance(event["payload"], dict) or not isinstance(event["request_id"], str):
            raise IntegrityError("Event payload or request id has the wrong type", {"event": expected_seq})
        unsigned = {key: event[key] for key in ("seq", "at", "type", "request_id", "payload", "prev_hash")}
        expected_hash = object_hash(unsigned)
        if event["hash"] != expected_hash:
            raise IntegrityError("Event content hash does not match", {"event": expected_seq})
