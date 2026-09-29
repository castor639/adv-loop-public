"""Every session's transcript stays with its workspace.

`persist` runs before the attempt is submitted, on every backend, and
writes the raw stream or the full message list, the ledger, the prompt
manifest, the rendered prompts, the result minus secrets, and the
submission or the rejection. `digest` turns one session into the
mechanical facts the improvement loops consume: tool sequence, repeated
commands, retrieval before an unassisted attempt, rejection loops, and
turns to a valid submission. Fixation measures are computed here from what
ran, never from what the model said about itself.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .backends.base import AssembledPrompt, SessionResult

TRANSCRIPTS = "transcripts"
GZIP_OVER_BYTES = 5 * 1024 * 1024
KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"fw_[A-Za-z0-9]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)(api[-_]?key|authorization|x-api-key)([\"':=\s]+)[A-Za-z0-9._\-]{16,}"),
]
RETRIEVAL_TOOLS = {"WebSearch", "WebFetch", "read_file", "Read", "Grep", "Glob"}
EXECUTION_TOOLS = {"run_in_sandbox", "Bash", "write_file", "Write", "Edit", "validate", "gpu_run", "gpu_collect"}
REFERENCE_DIR = ".harness/architecture/"


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    for pattern in KEY_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + m.group(2) if m.lastindex else "") + "[REDACTED]", text)
    return text


def session_dir(workspace: Path, session_id: str) -> Path:
    return workspace / TRANSCRIPTS / "sessions" / session_id


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if len(data) > GZIP_OVER_BYTES:
        with gzip.open(str(path) + ".gz", "wb") as handle:
            handle.write(data)
    else:
        path.write_bytes(data)


def persist(
    workspace: Path,
    result: SessionResult,
    prompt: AssembledPrompt,
    *,
    directive: Dict[str, Any],
    model: str,
    submission: Optional[Dict[str, Any]],
    rejection: Optional[List[str]],
    secrets: Iterable[str] = (),
    accepted: Optional[bool] = None,
    event_head_after: Optional[str] = None,
    spend_usd: Optional[float] = None,
) -> Path:
    target = session_dir(workspace, result.session_id)
    secrets = list(secrets)
    if result.messages is not None:
        _write(target / "messages.json", scrub(json.dumps(result.messages, indent=1, sort_keys=True), secrets))
    if result.session_dir:
        for name in ("stream.jsonl", "ledger.jsonl", "stderr.txt"):
            source = Path(result.session_dir) / name
            if source.is_file():
                _write(target / name, scrub(source.read_text(encoding="utf-8", errors="replace"), secrets))
    if not (target / "ledger.jsonl").exists() and not (target / "ledger.jsonl.gz").exists():
        _write(target / "ledger.jsonl", "".join(json.dumps(e, sort_keys=True) + "\n" for e in result.tool_ledger))
    _write(target / "prompt.manifest.json", json.dumps(prompt.manifest, indent=2, sort_keys=True) + "\n")
    _write(target / "system.md", scrub(prompt.system_text, secrets))
    _write(target / "prompt.md", scrub(prompt.user_text, secrets))
    record = asdict(result)
    record.pop("messages", None)
    record.pop("tool_ledger", None)
    record["session_dir"] = str(record["session_dir"]) if record.get("session_dir") else None
    _write(target / "result.json", scrub(json.dumps(record, indent=2, sort_keys=True, default=str), secrets))
    _write(target / "submission.json", json.dumps(
        {"submission": submission, "rejection": rejection, "accepted": accepted}, indent=2, sort_keys=True) + "\n")
    usage = result.usage or {}
    index_row = {
        "session_id": result.session_id, "directive_id": directive.get("directive_id"),
        "role": directive.get("role"), "mode": directive.get("mode"), "model": model,
        "started": result.started_at, "ended": result.finished_at, "ending": result.ended,
        "tokens": {"input": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
                   "output": usage.get("output_tokens") or usage.get("completion_tokens") or 0},
        "estimated_usd": spend_usd if spend_usd is not None else result.cost_usd,
        "accepted": accepted, "rejected": bool(rejection), "event_head_after": event_head_after,
        "tool_calls": len(result.tool_ledger),
    }
    index = workspace / TRANSCRIPTS / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with open(index, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(index_row, sort_keys=True) + "\n")
    return target


def mark(workspace: Path, session_id: str, *, accepted: bool, event_head: Optional[str] = None,
         note: Optional[str] = None) -> None:
    """Record the kernel's verdict on a persisted session: rewrite submission.json and append to the index."""

    target = session_dir(workspace, session_id)
    path = target / "submission.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {"submission": None, "rejection": None}
    record["accepted"] = accepted
    if note:
        record["note"] = note
    if event_head:
        record["event_head_after"] = event_head
    _write(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    rows = load_index(workspace)
    for row in rows:
        if row.get("session_id") == session_id:
            row["accepted"] = accepted
            row["event_head_after"] = event_head
            if note:
                row["note"] = note
    index = workspace / TRANSCRIPTS / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def load_index(workspace: Path) -> List[Dict[str, Any]]:
    path = workspace / TRANSCRIPTS / "index.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _read_ledger(target: Path) -> List[Dict[str, Any]]:
    plain, packed = target / "ledger.jsonl", target / "ledger.jsonl.gz"
    if plain.is_file():
        text = plain.read_text(encoding="utf-8")
    elif packed.is_file():
        with gzip.open(packed, "rt", encoding="utf-8") as handle:
            text = handle.read()
    else:
        return []
    entries = []
    for line in text.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _command_key(entry: Dict[str, Any]) -> str:
    tool = entry.get("tool")
    if tool in ("run_in_sandbox", "Bash"):
        command = entry.get("input", {}).get("command")
        text = " ".join(command) if isinstance(command, list) else str(command or "")
        text = re.sub(r"\b[0-9a-f]{12,}\b", "<hash>", text)
        text = re.sub(r"\d+", "<n>", text)
        return f"{tool}:{text[:120]}"
    return f"{tool}:{entry.get('input', {}).get('path') or entry.get('input', {}).get('file_path') or ''}"


def _reference_read(entry: Dict[str, Any]) -> bool:
    """A read of the exported system reference is orientation, not retrieval before an attempt."""

    if entry.get("tool") not in ("read_file", "Read"):
        return False
    args = entry.get("input") or {}
    path = str(args.get("path") or args.get("file_path") or "")
    return path.startswith(REFERENCE_DIR) or f"/{REFERENCE_DIR}" in path


def digest(target: Path) -> Dict[str, Any]:
    entries = _read_ledger(target)
    sequence = [e.get("tool") for e in entries]
    keys = [_command_key(e) for e in entries]
    repeats = {k: keys.count(k) for k in set(keys) if keys.count(k) > 1}
    first_exec = next((i for i, t in enumerate(sequence) if t in EXECUTION_TOOLS), None)
    first_retrieval = next((i for i, e in enumerate(entries) if e.get("tool") in RETRIEVAL_TOOLS and not _reference_read(e)), None)
    retrieval_before_attempt = first_retrieval is not None and (first_exec is None or first_retrieval < first_exec)
    failures = sum(1 for e in entries if isinstance(e.get("exit_code"), int) and e["exit_code"] != 0)
    schema_text = "|".join(sequence)
    try:
        submission = json.loads((target / "submission.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        submission = {}
    try:
        result = json.loads((target / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {}
    return {
        "session": target.name,
        "tool_calls": len(entries),
        "tool_sequence": sequence,
        "procedure_schema": hashlib.sha256(schema_text.encode("utf-8")).hexdigest()[:16],
        "repeated_commands": repeats,
        "retrieval_before_attempt": retrieval_before_attempt,
        "failed_commands": failures,
        "ending": result.get("ended"),
        "rejected": bool(submission.get("rejection")),
        "rejection": submission.get("rejection"),
        "errors": result.get("errors", []),
    }


def workspace_digests(workspace: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = load_index(workspace)
    if limit:
        rows = rows[-limit:]
    out = []
    for row in rows:
        target = session_dir(workspace, row["session_id"])
        if target.is_dir():
            summary = digest(target)
            summary.update({"role": row.get("role"), "mode": row.get("mode"), "model": row.get("model"),
                            "directive_id": row.get("directive_id"), "accepted": row.get("accepted")})
            out.append(summary)
    return out


def fixation_flags(workspace: Path, mode: str = "experiment", window: int = 3) -> Dict[str, Any]:
    """Prompt-level fixation signals for one workspace, from transcripts alone."""

    attempts = [d for d in workspace_digests(workspace) if d.get("mode") == mode and d.get("accepted")]
    recent = attempts[-window:]
    same_schema = len(recent) == window and len({d["procedure_schema"] for d in recent}) == 1
    retrieval_first = [d["session"] for d in attempts if d["retrieval_before_attempt"]]
    return {
        "mode": mode,
        "attempts_seen": len(attempts),
        "same_procedure_schema_last_n": same_schema,
        "retrieval_before_attempt_sessions": retrieval_first,
        "first_attempt_retrieved_first": bool(attempts) and attempts[0]["retrieval_before_attempt"],
        "rejection_loops": sum(1 for d in attempts if d["rejected"]),
    }

