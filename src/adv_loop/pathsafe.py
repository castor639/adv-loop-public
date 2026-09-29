"""Write-containment helpers shared by the supervisor-plane tools.

Every filesystem write the supervisor tools perform (intake, sandbox setup,
validator envelopes) resolves its destination through this module, so the
per-workspace containment contract has one enforcement point. This is a
contract against accidents and hostile archive names, not an OS-level jail:
a declared subprocess still runs with the invoking user's permissions.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Optional, Tuple

from .errors import ValidationError


def safe_relative(name: str) -> PurePosixPath:
    """Normalize an externally supplied path into a strictly relative one.

    Rejects absolute paths, drive prefixes, parent-directory components, and
    empty results. Backslashes are treated as separators so archive members
    written on other platforms cannot smuggle a single hostile component.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValidationError("Path must be a non-empty string", {"path": name})
    raw = name.replace("\\", "/")
    if raw.startswith("/"):
        raise ValidationError("Path escapes its containment root", {"path": name})
    parts = [part for part in PurePosixPath(raw).parts if part != "."]
    if not parts:
        raise ValidationError("Path must name a file or directory", {"path": name})
    for part in parts:
        if part == ".." or ":" in part or "\x00" in part:
            raise ValidationError("Path escapes its containment root", {"path": name})
    return PurePosixPath(*parts)


def ensure_within(base: Path, target: Path) -> Path:
    """Resolve target and require it to live under base; return the resolved path."""

    base_resolved = Path(base).resolve()
    target_resolved = Path(target).resolve()
    try:
        common = os.path.commonpath([str(base_resolved), str(target_resolved)])
    except ValueError as exc:
        raise ValidationError(
            "Path escapes its containment root",
            {"base": str(base_resolved), "path": str(target)},
        ) from exc
    if common != str(base_resolved):
        raise ValidationError(
            "Path escapes its containment root",
            {"base": str(base_resolved), "path": str(target_resolved)},
        )
    return target_resolved


def hash_stream(handle: BinaryIO, limit: Optional[int] = None) -> Tuple[str, int]:
    """SHA-256 and byte count of a stream; enforces limit on actual bytes read."""

    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if limit is not None and total > limit:
            raise ValidationError("Stream exceeds the configured byte limit", {"limit": limit})
        digest.update(chunk)
    return digest.hexdigest(), total


def hash_file(path: Path) -> Tuple[str, int]:
    with open(path, "rb") as handle:
        return hash_stream(handle)


def copy_stream(handle: BinaryIO, destination: Path, limit: Optional[int] = None) -> Tuple[str, int]:
    """Stream a file into destination with default permissions; returns (sha256, bytes).

    The destination is written with the process umask (never with mode bits
    taken from an archive), and actual bytes are counted so a lying header
    cannot bypass the limit.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with open(destination, "wb") as out:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if limit is not None and total > limit:
                out.close()
                destination.unlink(missing_ok=True)
                raise ValidationError("Stream exceeds the configured byte limit", {"limit": limit})
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest(), total
