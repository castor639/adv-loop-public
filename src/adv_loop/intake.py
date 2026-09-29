"""Intake: sort an arbitrary drop into isolated task workspaces (supervisor plane).

The classifier is structural and field-neutral: it partitions by directory
shape and filename hints, never by interpreting content. A model adapter may
propose a better partition through the same JSON contract; its proposal is
validated exactly like the heuristic one and interpreted no further. Dry-run
(the default) writes nothing anywhere; apply creates one workspace per
candidate and copies the assigned files into ``workspaces/<id>/payload/``,
which is evidence source material, never a projection of the event log.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple

from .engine import DEFAULT_CRITERION, LoopEngine, SAFE_ID, slug
from .errors import AdapterError, ValidationError
from .pathsafe import copy_stream, ensure_within, hash_stream, safe_relative
from .storage import object_hash, utc_now

INTAKE_VERSION = "1"
INTAKE_PROTOCOL = "adv-loop-intake/1"
INTAKE_POLICY_VERSION = "5.0"
DEFAULT_MAX_FILES = 10000
DEFAULT_MAX_BYTES = 500_000_000
BRIEF_SUFFIXES = {".md", ".txt", ".rst"}
DOC_STEMS = {"README", "LICENSE", "NOTICE", "CHANGELOG", "CONTRIBUTING", "CHARTER"}
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
BRIEF_READ_CAP = 1_000_000
SAMPLE_LIMIT = 32
SAMPLE_HEAD_CHARS = 4096

PARTITION_CRITERION = (
    "The drop is partitioned into isolated follow-on tasks, each with its own"
    " workspace, measurable acceptance criteria, and assigned payload files"
)

# Filename-presence hints only: intake suggests, it never interprets content.
SANDBOX_HINT_NAMES = {"uv.lock", "pyproject.toml", "requirements.txt"}
FORMALIZATION_HINT = {
    "names": {"lean-toolchain", "lakefile.lean"},
    "suffixes": {".lean"},
    "backend": "lean4",
    "profile": "formal-mathematics",
}
SANDBOX_HINT_PROFILE = "systems-software"


class DropReader:
    """Uniform access to a drop's files: a sorted manifest plus per-file streams."""

    def __init__(self, kind: str, entries: List[Dict[str, Any]],
                 opener: Callable[[str], BinaryIO], warnings: List[str],
                 closer: Optional[Callable[[], None]] = None):
        self.kind = kind
        self.entries = entries
        self._opener = opener
        self.warnings = warnings
        self._closer = closer

    def open(self, path: str) -> BinaryIO:
        return self._opener(path)

    def close(self) -> None:
        if self._closer is not None:
            self._closer()


def intake_report(
    source: str,
    root: Path = Path("workspaces"),
    apply: bool = False,
    link: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    adapter: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    stdin_text: Optional[str] = None,
) -> Dict[str, Any]:
    if max_files <= 0 or max_bytes <= 0:
        raise ValidationError("Intake caps must be positive", {"max_files": max_files, "max_bytes": max_bytes})
    caps = {"max_files": max_files, "max_bytes": max_bytes}
    reader, brief_text = _read_drop(source, caps, stdin_text)
    try:
        manifest = _manifest(source, reader)
        proposal = _heuristic_proposal(source, reader, manifest, brief_text)
        adapter_used = False
        if adapter is not None:
            envelope = {
                "protocol": INTAKE_PROTOCOL,
                "source": manifest,
                "heuristic_proposal": proposal,
                "samples": _collect_samples(reader),
            }
            candidate_proposal = adapter(envelope)
            _validate_proposal(candidate_proposal, manifest)
            proposal = candidate_proposal
            adapter_used = True
        else:
            _validate_proposal(proposal, manifest)
        proposal["adapter_used"] = adapter_used
        for candidate in proposal["candidates"]:
            candidate["existing"] = (Path(root) / candidate["task_id"] / "events.jsonl").is_file()
        report: Dict[str, Any] = {"mode": "apply" if apply else "dry_run", "proposal": proposal, "exit_code": 0}
        if apply:
            results = [_apply_candidate(candidate, reader, manifest, Path(root), link)
                       for candidate in proposal["candidates"]]
            report["results"] = results
            if any(not row["ok"] for row in results):
                report["exit_code"] = 1
        return report
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# Reading the drop
# ---------------------------------------------------------------------------


def _read_drop(source: str, caps: Dict[str, int],
               stdin_text: Optional[str]) -> Tuple[DropReader, Optional[str]]:
    """Build a reader for the drop; returns (reader, brief text when textual)."""

    if source == "-":
        text = stdin_text if stdin_text is not None else sys.stdin.read(BRIEF_READ_CAP + 1)
        if len(text) > BRIEF_READ_CAP:
            raise ValidationError("A text brief on stdin is capped at 1 MB")
        if not text.strip():
            raise ValidationError("The drop brief is empty")
        data = text.encode("utf-8")
        entry = {"path": "brief.md", "bytes": len(data), "sha256": _bytes_sha256(data)}
        reader = DropReader("brief", [entry], lambda _path: _BytesStream(data), [])
        return reader, text
    path = Path(source)
    if path.is_dir():
        return _directory_reader(path, caps), None
    if not path.is_file():
        raise ValidationError("Intake source must be a directory, archive, file, or '-'", {"source": source})
    name = path.name.lower()
    if name.endswith(".zip"):
        return _zip_reader(path, caps), None
    if any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES if suffix != ".zip"):
        return _tar_reader(path, caps), None
    data = path.read_bytes()
    _enforce_caps(caps, files=1, total_bytes=len(data))
    entry = {"path": str(safe_relative(path.name)), "bytes": len(data), "sha256": _bytes_sha256(data)}
    brief_text: Optional[str] = None
    if path.suffix.lower() in BRIEF_SUFFIXES and len(data) <= BRIEF_READ_CAP:
        try:
            brief_text = data.decode("utf-8")
        except UnicodeDecodeError:
            brief_text = None
    kind = "brief" if brief_text is not None else "file"
    reader = DropReader(kind, [entry], lambda _path: _BytesStream(data), [])
    return reader, brief_text


class _BytesStream:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:  # uniform with real file objects
        pass


def _bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _enforce_caps(caps: Dict[str, int], files: int, total_bytes: int) -> None:
    if files > caps["max_files"]:
        raise ValidationError("Drop exceeds the file-count cap", {"max_files": caps["max_files"]})
    if total_bytes > caps["max_bytes"]:
        raise ValidationError("Drop exceeds the byte cap", {"max_bytes": caps["max_bytes"]})


def _directory_reader(source: Path, caps: Dict[str, int]) -> DropReader:
    entries: List[Dict[str, Any]] = []
    warnings: List[str] = []
    paths: Dict[str, Path] = {}
    total = 0
    for base, dirs, files in os.walk(source, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            file_path = Path(base) / name
            if file_path.is_symlink():
                warnings.append(f"symlink skipped: {file_path.relative_to(source)}")
                continue
            relative = str(PurePosixPath(*file_path.relative_to(source).parts))
            relative = str(safe_relative(relative))
            with open(file_path, "rb") as handle:
                sha256, size = hash_stream(handle)
            total += size
            _enforce_caps(caps, files=len(entries) + 1, total_bytes=total)
            entries.append({"path": relative, "bytes": size, "sha256": sha256})
            paths[relative] = file_path
    if not entries:
        raise ValidationError("Drop contains no files", {"source": str(source)})
    entries.sort(key=lambda item: item["path"])
    return DropReader("directory", entries, lambda path: open(paths[path], "rb"), warnings)


def _zip_reader(source: Path, caps: Dict[str, int]) -> DropReader:
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise ValidationError("Drop archive is not readable", {"source": str(source)}) from exc
    entries: List[Dict[str, Any]] = []
    warnings: List[str] = []
    members: Dict[str, str] = {}
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            warnings.append(f"symlink skipped: {info.filename}")
            continue
        try:
            relative = str(safe_relative(info.filename))
        except ValidationError:
            warnings.append(f"unsafe member name skipped: {info.filename}")
            continue
        if relative in members:
            warnings.append(f"duplicate member skipped: {info.filename}")
            continue
        with archive.open(info) as handle:
            sha256, size = hash_stream(handle, limit=caps["max_bytes"])
        total += size
        _enforce_caps(caps, files=len(entries) + 1, total_bytes=total)
        entries.append({"path": relative, "bytes": size, "sha256": sha256})
        members[relative] = info.filename
    if not entries:
        raise ValidationError("Drop contains no files", {"source": str(source)})
    entries.sort(key=lambda item: item["path"])
    return DropReader("archive", entries, lambda path: archive.open(members[path]), warnings,
                      closer=archive.close)


def _tar_reader(source: Path, caps: Dict[str, int]) -> DropReader:
    try:
        archive = tarfile.open(source)
    except tarfile.TarError as exc:
        raise ValidationError("Drop archive is not readable", {"source": str(source)}) from exc
    entries: List[Dict[str, Any]] = []
    warnings: List[str] = []
    members: Dict[str, str] = {}
    total = 0
    for member in archive.getmembers():
        if not member.isfile():
            if member.issym() or member.islnk():
                warnings.append(f"link member skipped: {member.name}")
            elif not member.isdir():
                warnings.append(f"special member skipped: {member.name}")
            continue
        try:
            relative = str(safe_relative(member.name))
        except ValidationError:
            warnings.append(f"unsafe member name skipped: {member.name}")
            continue
        if relative in members:
            warnings.append(f"duplicate member skipped: {member.name}")
            continue
        handle = archive.extractfile(member)
        if handle is None:
            warnings.append(f"unreadable member skipped: {member.name}")
            continue
        sha256, size = hash_stream(handle, limit=caps["max_bytes"])
        total += size
        _enforce_caps(caps, files=len(entries) + 1, total_bytes=total)
        entries.append({"path": relative, "bytes": size, "sha256": sha256})
        members[relative] = member.name
    if not entries:
        raise ValidationError("Drop contains no files", {"source": str(source)})
    entries.sort(key=lambda item: item["path"])

    def opener(path: str) -> BinaryIO:
        handle = archive.extractfile(members[path])
        if handle is None:
            raise ValidationError("Archive member became unreadable", {"path": path})
        return handle

    return DropReader("archive", entries, opener, warnings, closer=archive.close)


def _manifest(source: str, reader: DropReader) -> Dict[str, Any]:
    entries = reader.entries
    source_kind = {
        "directory": "directory",
        "archive": "archive",
        "brief": "brief",
        "file": "file",
    }[reader.kind]
    return {
        "source": str(Path(source).resolve()) if source != "-" else "-",
        "source_kind": source_kind,
        "generated_at": utc_now(),
        "file_count": len(entries),
        "total_bytes": sum(item["bytes"] for item in entries),
        "entries": entries,
        "manifest_hash": object_hash(entries),
    }


# ---------------------------------------------------------------------------
# Heuristic classification (structural only)
# ---------------------------------------------------------------------------


def _is_doc_like(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.suffix.lower() in BRIEF_SUFFIXES or pure.stem.upper() in DOC_STEMS


def _candidate_id(name: str, entries: List[Dict[str, Any]]) -> str:
    stamp = object_hash(sorted(item["sha256"] for item in entries))[:8]
    return f"{slug(name)}-{stamp}"


def _suggestions(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    names = {PurePosixPath(item["path"]).name for item in entries}
    suffixes = {PurePosixPath(item["path"]).suffix.lower() for item in entries}
    sandbox = {"kind": "uv", "root": "sandbox"} if names & SANDBOX_HINT_NAMES else None
    formalization = None
    profile = None
    if names & FORMALIZATION_HINT["names"] or suffixes & FORMALIZATION_HINT["suffixes"]:
        formalization = FORMALIZATION_HINT["backend"]
        profile = FORMALIZATION_HINT["profile"]
    elif sandbox:
        profile = SANDBOX_HINT_PROFILE
    return {"sandbox": sandbox, "formalization": formalization, "profile": profile}


def _candidate(name: str, task: str, criteria: List[str], entries: List[Dict[str, Any]],
               notes: str) -> Dict[str, Any]:
    return {
        "task_id": _candidate_id(name, entries),
        "task": task,
        "criteria": criteria,
        "controls": [],
        **_suggestions(entries),
        "payload_paths": [item["path"] for item in entries],
        "manifest": entries,
        "notes": notes,
    }


def _url_list_lines(text: str) -> Optional[List[str]]:
    lines = [line.strip() for line in text.splitlines()]
    urls = [line for line in lines if line and not line.startswith("#")]
    if urls and all(line.startswith(("http://", "https://")) for line in urls):
        return urls
    return None


def _brief_fields(text: str) -> Tuple[str, List[str]]:
    task = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        task = stripped.lstrip("#").strip() or stripped
        break
    if not task:
        raise ValidationError("The drop brief has no usable first line")
    criteria: List[str] = []
    in_criteria = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_criteria = "criteri" in stripped.lower()
            continue
        if in_criteria and stripped[:2] in ("- ", "* "):
            item = stripped[2:].strip()
            if item and item not in criteria:
                criteria.append(item)
    return task[:200], criteria or [DEFAULT_CRITERION]


def _heuristic_proposal(source: str, reader: DropReader, manifest: Dict[str, Any],
                        brief_text: Optional[str]) -> Dict[str, Any]:
    entries = manifest["entries"]
    drop_name = Path(source).name if source != "-" else "brief"
    warnings = list(reader.warnings)
    if brief_text is not None:
        urls = _url_list_lines(brief_text)
        if urls is not None:
            manifest["source_kind"] = "url_list"
            candidate = _candidate(
                drop_name,
                f"Work through the {len(urls)} listed sources in '{drop_name}'",
                [DEFAULT_CRITERION],
                entries,
                f"list of {len(urls)} URLs; intake never fetches them",
            )
            return _proposal("single", [candidate], manifest, warnings)
        task, criteria = _brief_fields(brief_text)
        candidate = _candidate(drop_name, task, criteria, entries, "text brief")
        return _proposal("single", [candidate], manifest, warnings)
    if reader.kind == "file":
        candidate = _candidate(
            drop_name,
            f"Deliver the outcome of drop '{drop_name}'",
            [DEFAULT_CRITERION],
            entries,
            "single file",
        )
        return _proposal("single", [candidate], manifest, warnings)

    # Directory-shaped drop (directory or archive): partition by structure.
    prefix: List[str] = []
    while True:
        scoped = [_strip_prefix(item["path"], prefix) for item in entries]
        top_dirs = sorted({path.split("/", 1)[0] for path in scoped if "/" in path})
        loose = [path for path in scoped if "/" not in path]
        if len(top_dirs) == 1 and not loose:
            prefix.append(top_dirs[0])
            continue
        break
    if prefix:
        warnings.append(f"wrapper directory stripped: {'/'.join(prefix)}")
    by_top: Dict[str, List[Dict[str, Any]]] = {}
    loose_entries: List[Dict[str, Any]] = []
    for item in entries:
        scoped = _strip_prefix(item["path"], prefix)
        if "/" in scoped:
            by_top.setdefault(scoped.split("/", 1)[0], []).append(item)
        else:
            loose_entries.append(item)
    if len(by_top) >= 2 and all(_is_doc_like(_strip_prefix(item["path"], prefix)) for item in loose_entries):
        candidates = []
        for top in sorted(by_top):
            assigned = by_top[top] + loose_entries
            assigned = sorted(assigned, key=lambda item: item["path"])
            candidates.append(_candidate(
                top,
                f"Deliver the outcome of drop component '{top}'",
                [DEFAULT_CRITERION],
                assigned,
                f"top-level directory '{top}' ({len(by_top[top])} files)"
                + (f" plus {len(loose_entries)} shared doc files" if loose_entries else ""),
            ))
        return _proposal("partitioned", candidates, manifest, warnings)
    if len(by_top) >= 2:
        candidate = _candidate(
            drop_name,
            f"Sort intake drop '{drop_name}' into isolated follow-on tasks",
            [PARTITION_CRITERION, DEFAULT_CRITERION],
            entries,
            "mixed drop; splitting it safely needs its own task",
        )
        return _proposal("partition_task", [candidate], manifest, warnings)
    candidate = _candidate(
        drop_name,
        f"Deliver the outcome of drop '{drop_name}'",
        [DEFAULT_CRITERION],
        entries,
        "single coherent drop",
    )
    return _proposal("single", [candidate], manifest, warnings)


def _strip_prefix(path: str, prefix: List[str]) -> str:
    if not prefix:
        return path
    joined = "/".join(prefix) + "/"
    return path[len(joined):] if path.startswith(joined) else path


def _proposal(classification: str, candidates: List[Dict[str, Any]],
              manifest: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    return {
        "intake_version": INTAKE_VERSION,
        "policy_version": INTAKE_POLICY_VERSION,
        "source": manifest,
        "classification": classification,
        "candidates": candidates,
        "warnings": warnings,
    }


def _collect_samples(reader: DropReader) -> List[Dict[str, Any]]:
    ranked = sorted(
        reader.entries,
        key=lambda item: (not _is_doc_like(item["path"]), item["path"].count("/"), item["path"]),
    )
    samples = []
    for item in ranked[:SAMPLE_LIMIT]:
        sample = {"path": item["path"], "sha256": item["sha256"], "bytes": item["bytes"], "head": None}
        handle = reader.open(item["path"])
        try:
            head = handle.read(SAMPLE_HEAD_CHARS)
        finally:
            handle.close()
        try:
            sample["head"] = head.decode("utf-8")
        except UnicodeDecodeError:
            sample["head"] = None
        samples.append(sample)
    return samples


# ---------------------------------------------------------------------------
# Proposal validation (identical for heuristic and adapter proposals)
# ---------------------------------------------------------------------------

_CANDIDATE_KEYS = {
    "task_id", "task", "criteria", "controls", "profile", "sandbox",
    "formalization", "payload_paths", "manifest", "notes",
}


def _validate_proposal(proposal: Any, manifest: Dict[str, Any]) -> None:
    if not isinstance(proposal, dict):
        raise AdapterError("Intake proposal must be a JSON object")
    known_paths = {item["path"]: item for item in manifest["entries"]}
    candidates = proposal.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AdapterError("Intake proposal must contain at least one candidate")
    if proposal.get("classification") not in {"partitioned", "single", "partition_task"}:
        raise AdapterError("Intake proposal classification is unknown",
                           {"received": proposal.get("classification")})
    seen_ids = set()
    covered = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise AdapterError("Every intake candidate must be an object", {"index": index})
        unknown = set(candidate) - _CANDIDATE_KEYS
        if unknown:
            raise AdapterError("Intake candidate contains unknown fields",
                               {"index": index, "unknown": sorted(unknown)})
        task_id = candidate.get("task_id")
        if not isinstance(task_id, str) or not SAFE_ID.fullmatch(task_id) or task_id in seen_ids:
            raise AdapterError("Intake candidate task_id must be unique and filesystem-safe",
                               {"index": index, "task_id": task_id})
        seen_ids.add(task_id)
        if not isinstance(candidate.get("task"), str) or not candidate["task"].strip():
            raise AdapterError("Intake candidate needs a task statement", {"index": index})
        criteria = candidate.get("criteria")
        if (not isinstance(criteria, list) or not criteria
                or any(not isinstance(item, str) or not item.strip() for item in criteria)
                or len({item.strip() for item in criteria}) != len(criteria)):
            raise AdapterError("Intake candidate needs unique, non-empty acceptance criteria",
                               {"index": index})
        controls = candidate.get("controls", [])
        if not isinstance(controls, list) or any(not isinstance(item, str) or not item.strip() for item in controls):
            raise AdapterError("Intake candidate controls must be non-empty strings", {"index": index})
        for optional in ("profile", "formalization", "notes"):
            value = candidate.get(optional)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise AdapterError(f"Intake candidate {optional} must be a non-empty string or null",
                                   {"index": index})
        declared_sandbox = candidate.get("sandbox")
        if declared_sandbox is not None:
            if (not isinstance(declared_sandbox, dict)
                    or not isinstance(declared_sandbox.get("kind"), str)
                    or not declared_sandbox["kind"].strip()
                    or set(declared_sandbox) - {"kind", "root"}):
                raise AdapterError("Intake candidate sandbox must be {kind[, root]}", {"index": index})
        paths = candidate.get("payload_paths")
        if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths):
            raise AdapterError("Intake candidate payload_paths must be a unique, non-empty list",
                               {"index": index})
        unknown_paths = set(paths) - set(known_paths)
        if unknown_paths:
            raise AdapterError("Intake candidate references files outside the manifest",
                               {"index": index, "unknown": sorted(unknown_paths)[:5]})
        covered.update(paths)
        candidate_manifest = candidate.get("manifest")
        expected = [known_paths[path] for path in sorted(paths)]
        if candidate_manifest != expected:
            raise AdapterError(
                "Intake candidate manifest must be the sorted manifest entries of its payload_paths",
                {"index": index},
            )
    missing = set(known_paths) - covered
    if missing:
        raise AdapterError("Every manifest file must be assigned to at least one candidate",
                           {"unassigned": sorted(missing)[:5]})


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _apply_candidate(candidate: Dict[str, Any], reader: DropReader,
                     manifest: Dict[str, Any], root: Path, link: bool) -> Dict[str, Any]:
    task_id = candidate["task_id"]
    if (Path(root) / task_id / "events.jsonl").is_file():
        # The id embeds the payload content hash, so an existing workspace with
        # this id IS this drop: re-dropping joins it instead of forking a twin.
        return {
            "task_id": task_id,
            "ok": True,
            "existing": True,
            "workspace": str(Path(root) / task_id),
            "payload_files": 0,
            "payload_bytes": 0,
            "linked": False,
        }
    try:
        engine = LoopEngine.create(
            root,
            candidate["task"],
            list(candidate["criteria"]),
            {},
            task_id=task_id,
            controls=list(candidate.get("controls") or []),
            policy_version=INTAKE_POLICY_VERSION,
        )
        payload_root = engine.workspace / "payload"
        payload_root.mkdir()
        entries = {item["path"]: item for item in candidate["manifest"]}
        copied_bytes = 0
        linked = False
        for path in sorted(entries):
            destination = ensure_within(payload_root, payload_root / safe_relative(path))
            destination.parent.mkdir(parents=True, exist_ok=True)
            expected = entries[path]
            if link and reader.kind == "directory":
                source_path = None
                try:
                    handle = reader.open(path)
                    source_path = getattr(handle, "name", None)
                    handle.close()
                    if source_path:
                        os.link(source_path, destination)
                        linked = True
                        continue
                except OSError:
                    pass  # cross-device or unsupported: fall through to a copy
            handle = reader.open(path)
            try:
                sha256, size = copy_stream(handle, destination)
            finally:
                handle.close()
            if sha256 != expected["sha256"] or size != expected["bytes"]:
                raise ValidationError(
                    "Copied payload file does not match its manifest entry",
                    {"path": path},
                )
            copied_bytes += size
        intake_manifest = {
            "intake_version": INTAKE_VERSION,
            "generated_at": utc_now(),
            "source": {
                "path": manifest["source"],
                "kind": manifest["source_kind"],
                "manifest_hash": manifest["manifest_hash"],
            },
            "entries": candidate["manifest"],
        }
        (payload_root / ".intake-manifest.json").write_text(
            json.dumps(intake_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        config: Dict[str, Any] = {}
        for key in ("profile", "formalization"):
            if candidate.get(key):
                config[key] = candidate[key]
        if candidate.get("sandbox"):
            config["sandbox"] = candidate["sandbox"]
        config_path = engine.workspace / "loop-config.json"
        if config and not config_path.exists():
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {
            "task_id": task_id,
            "ok": True,
            "workspace": str(engine.workspace),
            "payload_files": len(entries),
            "payload_bytes": copied_bytes,
            "linked": linked,
        }
    except (ValidationError, OSError) as exc:
        error = exc.as_dict() if isinstance(exc, ValidationError) else {
            "error": "io_error", "message": str(exc), "details": {},
        }
        return {"task_id": task_id, "ok": False, "error": error}
