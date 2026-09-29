"""The tool ledger: what the tools actually did, recorded by the harness.

Every backend appends one entry per tool call. The attempt's ``observation``
is generated from this record, never from the model's account of it, and
evidence fingerprints are computed here from real files. The model contributes
``observation_notes`` and names artifacts; it never supplies a hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from adv_loop.errors import ValidationError
from adv_loop.pathsafe import ensure_within, hash_file, safe_relative
from adv_loop.storage import utc_now

STDOUT_TAIL = 400
STDERR_TAIL = 200
STORED_TAIL = 2000
EXIT_MARK = re.compile(r"EXIT=(\d+)\s*$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def entry(
    tool_name: str,
    tool_input: Dict[str, Any],
    stdout: str = "",
    stderr: str = "",
    *,
    tool_use_id: Optional[str] = None,
    cwd: Optional[str] = None,
    duration_ms: Optional[int] = None,
    interrupted: bool = False,
    file_sha256: Optional[str] = None,
    error: Optional[str] = None,
    at: Optional[str] = None,
    exit_code: Optional[int] = None,
    job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize one tool call into the ledger shape shared by every backend.

    `exit_code` is what the harness observed directly (a GPU job's return code);
    when it is not given the `EXIT=N` marker in stdout is the only source.
    """

    if exit_code is None:
        match = EXIT_MARK.search(stdout.rstrip())
        if match:
            exit_code = int(match.group(1))
    record: Dict[str, Any] = {
        "at": at or utc_now(),
        "tool_use_id": tool_use_id or f"t{len(stdout) + len(stderr):x}-{_sha(json.dumps(tool_input, sort_keys=True))[:8]}",
        "tool": tool_name,
        "cwd": cwd,
        "input": tool_input,
        "stdout_tail": _tail(stdout, STORED_TAIL),
        "stderr_tail": _tail(stderr, STORED_TAIL),
        "stdout_sha256": _sha(stdout),
        "stderr_sha256": _sha(stderr),
        "stdout_bytes": len(stdout.encode("utf-8", "replace")),
        "stderr_bytes": len(stderr.encode("utf-8", "replace")),
        "exit_code": exit_code,
        "interrupted": interrupted,
        "duration_ms": duration_ms,
    }
    if file_sha256:
        record["file_sha256"] = file_sha256
    if error:
        record["error"] = error
    if job is not None:
        record["job"] = job
    return record


def append(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _describe(record: Dict[str, Any]) -> str:
    tool_input = record.get("input") or {}
    for key in ("command", "file_path", "path", "code", "query", "url"):
        if key in tool_input:
            value = str(tool_input[key])
            return value if len(value) <= 300 else value[:300] + "..."
    return json.dumps(tool_input, sort_keys=True)[:300]


def observation_digest(entries: Iterable[Dict[str, Any]], limit: int = 8000) -> str:
    """Deterministic text built only from recorded tool results."""

    blocks: List[str] = []
    for index, record in enumerate(entries, start=1):
        head = f"#{index} {record.get('tool')}"
        if record.get("tool_use_id"):
            head += f" [{record['tool_use_id']}]"
        if record.get("cwd"):
            head += f" cwd={record['cwd']}"
        if record.get("duration_ms") is not None:
            head += f" ({record['duration_ms']} ms)"
        if record.get("exit_code") is not None:
            head += f" exit={record['exit_code']}"
        if record.get("interrupted"):
            head += " interrupted"
        lines = [head, f"$ {_describe(record)}"]
        stdout = record.get("stdout_tail") or ""
        stderr = record.get("stderr_tail") or ""
        if stdout:
            lines.append(f"stdout[sha256:{record.get('stdout_sha256', '')[:12]}]: {_tail(stdout, STDOUT_TAIL)}")
        if stderr:
            lines.append(f"stderr[sha256:{record.get('stderr_sha256', '')[:12]}]: {_tail(stderr, STDERR_TAIL)}")
        if record.get("file_sha256"):
            lines.append(f"file sha256: {record['file_sha256']}")
        if record.get("error"):
            lines.append(f"error: {record['error']}")
        job = record.get("job")
        if isinstance(job, dict):
            lines.append(f"job {job.get('job_id')} status={job.get('status')} outputs={len(job.get('outputs') or [])}"
                         f" toolchain={str(job.get('toolchain_hash') or '')[:12]}")
        blocks.append("\n".join(lines))
    if not blocks:
        return "No tool calls were recorded in this session."
    text = "\n\n".join(blocks)
    if len(text) <= limit:
        return text
    keep = max(limit // max(len(blocks), 1), 200)
    trimmed = [block if len(block) <= keep else block[:keep] + "\n[truncated]" for block in blocks]
    text = "\n\n".join(trimmed)
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def touched_paths(entries: Iterable[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for record in entries:
        tool_input = record.get("input") or {}
        for key in ("file_path", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value not in seen:
                seen.append(value)
        command = tool_input.get("command")
        if isinstance(command, str):
            seen.append(command)
        for key in ("inputs", "outputs"):
            for item in tool_input.get(key) or []:
                if isinstance(item, str) and item not in seen:
                    seen.append(item)
        job = record.get("job")
        if isinstance(job, dict):
            for output in job.get("outputs") or []:
                path = output.get("path") if isinstance(output, dict) else None
                if isinstance(path, str) and path not in seen:
                    seen.append(path)
    return seen


def _mentioned(path: str, touched: List[str]) -> bool:
    name = Path(path).name
    return any(path in item or name in item for item in touched)


def verify_evidence(
    workspace: Path,
    model_evidence: Any,
    verdicts: Optional[Dict[str, Dict[str, Any]]] = None,
    ledger: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Turn model evidence (paths or verdict ids) into engine evidence with harness-computed fingerprints.

    Returns (evidence, problems, warnings). A problem rejects the submission
    and is fed back to the model; a warning is appended to ``uncertainties``.
    """

    verdicts = verdicts or {}
    touched = touched_paths(ledger or [])
    evidence: List[Dict[str, Any]] = []
    problems: List[str] = []
    warnings: List[str] = []
    if model_evidence is None:
        model_evidence = []
    if not isinstance(model_evidence, list):
        return [], ["evidence must be a list"], []
    for index, item in enumerate(model_evidence):
        if not isinstance(item, dict):
            problems.append(f"evidence[{index}] must be an object")
            continue
        item = dict(item)
        artifact = item.pop("artifact_path", None)
        verdict_id = item.pop("verdict_id", None)
        claimed = item.pop("fingerprint", None)
        if (artifact is None) == (verdict_id is None):
            problems.append(f"evidence[{index}] needs exactly one of artifact_path or verdict_id")
            continue
        if artifact is not None:
            try:
                rel = safe_relative(str(artifact))
                target = ensure_within(workspace, workspace / rel)
            except ValidationError as exc:
                problems.append(f"evidence[{index}] artifact_path rejected: {exc}")
                continue
            if not target.is_file():
                problems.append(f"evidence[{index}] artifact_path does not exist: {artifact}")
                continue
            digest, _ = hash_file(target)
            item["fingerprint"] = digest
            item.setdefault("locator", str(rel))
            if not _mentioned(str(rel), touched):
                warnings.append(f"artifact {rel} was not observed in this session's tool ledger")
        else:
            verdict = verdicts.get(str(verdict_id))
            if verdict is None:
                problems.append(f"evidence[{index}] verdict_id is not from this session: {verdict_id}")
                continue
            result = verdict.get("result") or verdict
            checker = {
                key: result[key]
                for key in ("checker_id", "checker_version", "accepted", "artifact_hash", "log_hash",
                            "toolchain_hash", "attestation")
                if key in result
            }
            item["checker"] = checker
            item["fingerprint"] = checker["artifact_hash"]
            hint = verdict.get("evidence_hint") or {}
            for key in ("kind", "method", "formalization_rank"):
                if key in hint and key not in item:
                    item[key] = hint[key]
            if hint.get("locator"):
                item["locator"] = hint["locator"]  # the signed verdict names itself; the model's wording never does
            if checker.get("accepted") is not True:
                item.pop("formalization_rank", None)  # a rejecting verdict is an observation, not ranked proof
        if claimed and claimed != item["fingerprint"]:
            problems.append(f"evidence[{index}] claimed fingerprint does not match the artifact")
            continue
        evidence.append(item)
    return evidence, problems, warnings
