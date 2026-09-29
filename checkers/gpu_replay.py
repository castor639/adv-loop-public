#!/usr/bin/env python3
"""GPU replay checker: a pulled output is what the authoritative job record says it is.

Register it as a host-side validator hook (the harness's provision step does):

  loop-config.json:  "validators": {"gpu-replay": {"command": ["python3", "checkers/gpu_replay.py"],
                                                   "rank": "executable_spec", "in_sandbox": false,
                                                   "timeout_seconds": 600}}
  input:             {"job_id": "g-20260912T181300Z-0a1b2c3d", "artifact": "payload/derived/out.txt"}

Only the record under .harness/host/gpu/jobs/<id>/job.json is read; the copy
under .harness/gpu/ is what a session can write to and is never consulted.
Accepted iff the job was collected with return code 0, the artifact is a
declared output, every recorded output still hashes to its recorded sha256,
the logs are unchanged, and the record names the box toolchain. The verdict
carries that toolchain hash, so the kernel's pin_toolchain_hash overlay binds
GPU verdicts to the GPU box rather than to the CPU container. Exit 2 on
contract problems (missing record, escaping path) so validate_report reports
a hook failure instead of a fake rejection. Stdlib only; lives outside src/.
"""

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

HEX_256 = re.compile(r"^[0-9a-f]{64}$")
JOB_ID = re.compile(r"^g-\d{8}T\d{6}Z-[0-9a-f]{8}$")
HOST_RECORDS = ".harness/host/gpu/jobs"
PUBLIC_JOBS = ".harness/gpu/jobs"
SEPARATOR = "\n---\n"
CHECKER_ID = "gpu-replay"
CHECKER_VERSION = "1.0"


def fail(message: str) -> None:
    print(f"gpu_replay: {message}", file=sys.stderr)
    raise SystemExit(2)


def relative(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.startswith("/") or "\\" in value:
        fail(f"input.{name} must be a workspace-relative path")
    parts = [part for part in PurePosixPath(value).parts if part != "."]
    if not parts or any(part == ".." for part in parts):
        fail(f"input.{name} escapes the workspace")
    return PurePosixPath(*parts).as_posix()


def within(workspace: Path, rel: str) -> Path:
    path = (workspace / rel).resolve()
    if path != workspace and workspace not in path.parents:
        fail(f"path escapes the workspace: {rel}")
    return path


def sha256_path(path: Path) -> str:
    """Same rule as the harness: a file's bytes, or a directory's sorted file manifest."""

    if path.is_dir():
        entries = []
        for member in sorted(p for p in path.rglob("*") if p.is_file()):
            digest = hashlib.sha256(member.read_bytes()).hexdigest()
            entries.append([member.relative_to(path).as_posix(), digest, member.stat().st_size])
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def load_record(workspace: Path, job_id) -> dict:
    if not isinstance(job_id, str) or not JOB_ID.match(job_id):
        fail("input.job_id must be a GPU job id")
    path = workspace / HOST_RECORDS / job_id / "job.json"
    if not path.is_file():
        fail(f"no authoritative record for job {job_id}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        fail(f"record for job {job_id} is not valid JSON")
    if not isinstance(record, dict):
        fail(f"record for job {job_id} is not an object")
    return record


def check_outputs(workspace: Path, record: dict, problems: list) -> list:
    paths = []
    for output in record.get("outputs") or []:
        if not isinstance(output, dict) or not isinstance(output.get("path"), str):
            problems.append("record carries a malformed output entry")
            continue
        rel = output["path"]
        paths.append(rel)
        target = within(workspace, relative(rel, "outputs"))
        if not target.exists():
            problems.append(f"recorded output is missing: {rel}")
        elif sha256_path(target) != output.get("sha256"):
            problems.append(f"recorded output changed since collection: {rel}")
    return paths


def main() -> None:
    envelope = json.load(sys.stdin)
    workspace = Path(envelope["workspace"]).resolve()
    config = envelope.get("input") or {}
    if not isinstance(config, dict):
        fail("input must be an object with job_id and artifact")
    record = load_record(workspace, config.get("job_id"))
    job_id = config["job_id"]
    artifact = relative(config.get("artifact"), "artifact")
    artifact_path = within(workspace, artifact)
    if not artifact_path.exists():
        fail(f"artifact not found: {artifact}")
    artifact_hash = sha256_path(artifact_path)
    logs = workspace / PUBLIC_JOBS / job_id
    log_hash = hashlib.sha256(
        (read_text(logs / "stdout.log") + SEPARATOR + read_text(logs / "stderr.log")).encode("utf-8", "replace")
    ).hexdigest()

    problems = []
    if record.get("status") != "collected":
        problems.append(f"job status is {record.get('status')!r}, not collected")
    if record.get("outcome") not in (None, "finished"):
        problems.append(f"job ended as {record.get('outcome')!r}")
    if record.get("returncode") != 0:
        problems.append(f"job return code is {record.get('returncode')!r}")
    recorded = check_outputs(workspace, record, problems)
    if artifact not in recorded:
        problems.append(f"artifact is not a recorded output of the job: {artifact}")
    if record.get("log_hash") and record["log_hash"] != log_hash:
        problems.append("job logs changed since collection")
    environment = record.get("environment") or {}
    toolchain = str(environment.get("toolchain_hash") or "").lower()
    if not HEX_256.match(toolchain):
        problems.append("record carries no toolchain hash")
        toolchain = ""
    verdict = {
        "accepted": not problems,
        "checker_id": CHECKER_ID,
        "checker_version": CHECKER_VERSION,
        "artifact_hash": artifact_hash,
        "log_hash": log_hash,
        "details": {
            "job_id": job_id,
            "returncode": record.get("returncode"),
            "command": record.get("command"),
            "image_digest": environment.get("image_digest"),
            "instance_type": (record.get("box") or {}).get("instance_type"),
            "driver": environment.get("driver"),
            "problems": problems,
        },
    }
    if toolchain:
        verdict["toolchain_hash"] = toolchain
    print(json.dumps(verdict))


if __name__ == "__main__":
    main()
