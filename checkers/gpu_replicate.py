#!/usr/bin/env python3
"""GPU replication checker: two independent jobs produced the same artifact.

  loop-config.json:  "validators": {"gpu-replicate": {"command": ["python3", "checkers/gpu_replicate.py"],
                                                      "rank": "replicated_experiment", "in_sandbox": false,
                                                      "timeout_seconds": 900}}
  input:             {"job_ids": ["g-...", "g-..."], "artifact": "payload/derived/out.txt",
                      "compare": ["python3", "payload/scratch/compare.py", "{a}", "{b}"]}

Reads only the authoritative records. Accepted iff both jobs were collected
with return code 0, share command, cwd, image digest and toolchain hash,
have distinct ids, their per-job copies of the artifact under
.harness/gpu/jobs/<id>/outputs/ still hash to the recorded values, and the
copies are byte-identical or the `compare` argv exits 0. `compare` runs
through the workspace's declared sandbox command prefix (the CPU container),
with {a} and {b} replaced by the two copies' paths, under a 600 s cap. Exit 2
on contract problems. Stdlib only; lives outside src/.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

HEX_256 = re.compile(r"^[0-9a-f]{64}$")
JOB_ID = re.compile(r"^g-\d{8}T\d{6}Z-[0-9a-f]{8}$")
HOST_RECORDS = ".harness/host/gpu/jobs"
PUBLIC_JOBS = ".harness/gpu/jobs"
SEPARATOR = "\n---\n"
COMPARE_TIMEOUT = 600
CHECKER_ID = "gpu-replicate"
CHECKER_VERSION = "1.0"
MUST_MATCH = ("command", "cwd")


def fail(message: str) -> None:
    print(f"gpu_replicate: {message}", file=sys.stderr)
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
    if path.is_dir():
        entries = []
        for member in sorted(p for p in path.rglob("*") if p.is_file()):
            digest = hashlib.sha256(member.read_bytes()).hexdigest()
            entries.append([member.relative_to(path).as_posix(), digest, member.stat().st_size])
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_record(workspace: Path, job_id) -> dict:
    if not isinstance(job_id, str) or not JOB_ID.match(job_id):
        fail("input.job_ids must hold two GPU job ids")
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


def recorded_hash(record: dict, artifact: str):
    for output in record.get("outputs") or []:
        if isinstance(output, dict) and output.get("path") == artifact:
            return output.get("sha256")
    return None


def main() -> None:
    envelope = json.load(sys.stdin)
    workspace = Path(envelope["workspace"]).resolve()
    sandbox = envelope.get("sandbox") or {}
    prefix = list(sandbox.get("command") or []) if isinstance(sandbox, dict) else []
    config = envelope.get("input") or {}
    if not isinstance(config, dict):
        fail("input must be an object with job_ids and artifact")
    job_ids = config.get("job_ids")
    if not isinstance(job_ids, list) or len(job_ids) != 2:
        fail("input.job_ids must hold exactly two GPU job ids")
    compare = config.get("compare")
    if compare is not None and (not isinstance(compare, list) or not compare
                                or not all(isinstance(item, str) and item.strip() for item in compare)):
        fail("input.compare must be a non-empty argv list when given")
    records = [load_record(workspace, job_id) for job_id in job_ids]
    artifact = relative(config.get("artifact"), "artifact")
    copies = [within(workspace, f"{PUBLIC_JOBS}/{job_id}/outputs/{artifact}") for job_id in job_ids]

    problems = []
    if job_ids[0] == job_ids[1]:
        problems.append("the two job ids are the same job")
    hashes = []
    for job_id, record, copy in zip(job_ids, records, copies):
        if record.get("status") != "collected":
            problems.append(f"{job_id}: status is {record.get('status')!r}, not collected")
        if record.get("outcome") not in (None, "finished"):
            problems.append(f"{job_id}: job ended as {record.get('outcome')!r}")
        if record.get("returncode") != 0:
            problems.append(f"{job_id}: return code is {record.get('returncode')!r}")
        expected = recorded_hash(record, artifact)
        if expected is None:
            problems.append(f"{job_id}: artifact is not a recorded output")
        if not copy.exists():
            problems.append(f"{job_id}: per-job copy of the artifact is missing")
            hashes.append(expected)
            continue
        actual = sha256_path(copy)
        if expected is not None and actual != expected:
            problems.append(f"{job_id}: per-job copy changed since collection")
        hashes.append(actual)
    for key in MUST_MATCH:
        if records[0].get(key) != records[1].get(key):
            problems.append(f"jobs differ in {key}")
    environments = [record.get("environment") or {} for record in records]
    if environments[0].get("image_digest") != environments[1].get("image_digest"):
        problems.append("jobs ran different images")
    toolchains = [str(env.get("toolchain_hash") or "").lower() for env in environments]
    toolchain = toolchains[0] if toolchains[0] == toolchains[1] and HEX_256.match(toolchains[0]) else ""
    if not toolchain:
        problems.append("jobs do not share a recorded toolchain hash")

    compare_rc = None
    stdout = stderr = ""
    if hashes[0] is not None and hashes[0] == hashes[1]:
        identical = True
    elif compare is not None and all(copy.exists() for copy in copies):
        argv = prefix + [item.replace("{a}", str(copies[0])).replace("{b}", str(copies[1])) for item in compare]
        try:
            done = subprocess.run(argv, cwd=str(workspace), capture_output=True, text=True,
                                  timeout=COMPARE_TIMEOUT, check=False)
            compare_rc, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            compare_rc = None
            stderr = f"compare timed out after {COMPARE_TIMEOUT}s: {exc}"
        identical = compare_rc == 0
        if not identical:
            problems.append(f"compare exited {compare_rc!r}")
    else:
        identical = False
        problems.append("the artifacts differ and no compare command accepted them")

    if copies[0].exists():
        artifact_hash = sha256_path(copies[0])
    else:
        fallback = within(workspace, artifact)
        if not fallback.exists():
            fail(f"artifact not found in either job copy or the workspace: {artifact}")
        artifact_hash = sha256_path(fallback)
    log_hash = hashlib.sha256((stdout + SEPARATOR + stderr).encode("utf-8", "replace")).hexdigest()
    verdict = {
        "accepted": identical and not problems,
        "checker_id": CHECKER_ID,
        "checker_version": CHECKER_VERSION,
        "artifact_hash": artifact_hash,
        "log_hash": log_hash,
        "details": {
            "job_ids": list(job_ids),
            "command": records[0].get("command"),
            "cwd": records[0].get("cwd"),
            "image_digest": environments[0].get("image_digest"),
            "hashes": hashes,
            "compare_returncode": compare_rc,
            "compare_stdout_tail": stdout[-2000:],
            "problems": problems,
        },
    }
    if toolchain:
        verdict["toolchain_hash"] = toolchain
    print(json.dumps(verdict))


if __name__ == "__main__":
    main()
