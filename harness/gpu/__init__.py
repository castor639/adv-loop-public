"""GPU jobs on a self-started AWS box.

A workspace whose charter grants GPU compute (`harness.gpu.enabled` in
loop-config.json) advertises three extra tools to its sessions. `gpu_run`
starts the box if needed, pushes only the declared inputs to a path-identical
mirror, runs the job inside the pinned GPU image, blocks until it ends, and
pulls only the declared outputs back into the workspace, where they are
hashed and ledgered like any other artifact. GPU hours are billed to the AWS
pot, never to the model-call ledger. The box stops itself when idle.
"""

from __future__ import annotations

from typing import Any, Dict, List

HOURLY_USD = 1.006
BOX_NAME = "adv-gpu-box"
INSTANCE_TYPE = "g5.xlarge"
MIRROR_ROOT = "/srv/adv-loop/workspaces"
PINS_PATH = "/opt/adv-loop/PINS"
CONTAINER_PREFIX = "adv-gpu-"
GPU_SPEND_FILE = ".gpu-spend.jsonl"
STATE_SPEND_FILE = "gpu-spend.jsonl"
STATE_JOBS_FILE = "gpu-jobs.jsonl"
STATE_BOX_FILE = "gpu-box.json"
STATE_LOCK_FILE = "gpu-box.lock.json"
WORKSPACE_LOCK_FILE = "sandbox/gpu.lock"

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "box": BOX_NAME,
    "instance_type": INSTANCE_TYPE,
    "image": "adv-loop-gpu:v1",
    "max_hours": 12.0,
    "max_job_seconds": 14400,
    "min_job_seconds": 60,
    "queue_wait_seconds": 3600,
    "max_output_bytes": 2 * 1024 * 1024 * 1024,
    "hourly_usd": HOURLY_USD,
    "network": "bridge",
    "idle_minutes": 10,
    "start_max_wait_seconds": 900,
    "ssh_ready_seconds": 420,
    "collect_grace_seconds": 600,
}

LIMITS: Dict[str, Any] = {
    "max_job_seconds": DEFAULTS["max_job_seconds"],
    "min_job_seconds": DEFAULTS["min_job_seconds"],
    "max_hours_default": DEFAULTS["max_hours"],
    "max_output_bytes": DEFAULTS["max_output_bytes"],
    "hourly_usd": HOURLY_USD,
    "idle_minutes_controller": DEFAULTS["idle_minutes"],
    "idle_minutes_box": 15,
    "max_uptime_minutes_box": 720,
    "container_memory": "13g",
    "container_cpus": "3.5",
    "container_pids_limit": 4096,
    "container_shm_size": "4g",
}

JOB_STATES = (
    "queued", "starting", "running", "finished", "timeout", "cancelled", "failed", "interrupted",
    "collected", "refused",
)
TERMINAL_STATES = ("finished", "timeout", "cancelled", "failed", "interrupted", "collected", "refused")
REFUSAL_REASONS = (
    "gpu_hours_cap", "aws_gpu_stop", "budget_unknown", "box_unavailable", "environment_drift", "busy",
    "push_failed", "launch_failed", "box_unreachable", "pull_failed", "output_missing", "output_too_large",
    "capacity", "quota", "not_configured", "invalid_input", "internal_error",
    "budget_stop", "not_found", "disk_full", "hostkey_mismatch", "ssh_unreachable", "stop_failed", "aws_error",
    "agent_error", "not_running", "path_refused", "locked", "bootstrap_failed", "keygen_failed",
)
NETWORKS = ("bridge", "none")
ENV_KEY_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
ENV_FORBIDDEN_PREFIXES = ("AWS_", "ANTHROPIC_", "AZURE_", "CLAUDE", "SSH_", "LD_")

JOB_FIELDS: Dict[str, str] = {
    "job_id": "g-<utc compact>-<hex8>; also the docker container suffix and the checker input key",
    "workspace": "absolute workspace path on harness-box, equal to the mirror path on the box",
    "ws_id": "workspace directory name",
    "session_id": "session that started the job",
    "directive_id": "directive the session was answering",
    "role": "role of the starting session",
    "mode": "mode of the starting session",
    "tool_use_id": "tool call id from the model, when the backend supplies one",
    "label": "free text from the model, at most 80 characters",
    "command": "argv list run inside the GPU image",
    "cwd": "workspace-relative working directory, default payload/scratch",
    "env": "extra environment for the job, validated keys only",
    "network": "bridge or none",
    "timeout_seconds": "per-job cap enforced on the box",
    "inputs": "declared inputs with sha256 and bytes as pushed",
    "outputs_declared": "declared output paths, workspace-relative",
    "status": "one of JOB_STATES",
    "reason": "refusal or failure reason, null otherwise",
    "returncode": "exit code of the job process, null until finished",
    "box": "instance_id, instance_type, started_at of the box that ran it",
    "environment": "image, image_digest, cuda, torch, python, driver, gpu, toolchain_hash observed at launch",
    "created_at": "record creation time",
    "launched_at": "time the agent reported running",
    "finished_at": "time the agent reported a terminal state",
    "collected_at": "time the outputs were pulled and hashed",
    "collected_by_session": "session that collected the outputs",
    "outputs": "pulled outputs with sha256 and bytes",
    "missing_outputs": "declared outputs the job did not produce",
    "stdout_sha256": "sha256 of the full stdout log",
    "stderr_sha256": "sha256 of the full stderr log",
    "log_hash": "sha256 of stdout, a separator line, and stderr",
    "seconds_billed": "box seconds attributed to this job",
    "hours_billed": "seconds_billed / 3600 rounded up to the minute",
    "usd": "hours_billed times hourly_usd",
    "outcome": "terminal run state kept after collection: finished, timeout, cancelled, failed, interrupted",
    "outputs_ready": "true once logs and outputs sit in the host job dir awaiting collection",
    "pid": "harness process that owns the running job, host-only",
    "message": "human-readable detail for a refusal or failure",
}

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "gpu_run",
        "description": (
            "Run one argv command on the granted GPU box (one NVIDIA A10G) inside the pinned GPU image."
            " Blocks until the job ends; the default cap is four hours. Only the files named in `inputs`"
            " are copied to the box and only `outputs` are copied back; nothing else exists there."
            " The result is an observation: cite a pulled output as artifact_path, or run the validate"
            " hook gpu-replay on it to obtain a checker verdict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cwd": {"type": "string"},
                "inputs": {"type": "array", "items": {"type": "string"}},
                "outputs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "timeout_seconds": {"type": "number", "minimum": 60},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "network": {"type": "string", "enum": ["bridge", "none"]},
                "label": {"type": "string", "maxLength": 80},
            },
            "required": ["command", "outputs"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gpu_collect",
        "description": (
            "Pull and hash the declared outputs of a GPU job that finished after the session that"
            " started it ended. The directive's supplemental block lists jobs awaiting collection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gpu_status",
        "description": (
            "Report the GPU grant for this workspace: hours used and remaining, whether the fleet"
            " budget allows a start, and the recorded jobs. Never starts the box."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = tuple(t["name"] for t in TOOL_DEFINITIONS)


def config_for(config: Dict[str, Any]) -> Dict[str, Any]:
    """The workspace's `harness.gpu` block over the defaults."""

    harness = config.get("harness") if isinstance(config, dict) else None
    block = harness.get("gpu") if isinstance(harness, dict) else None
    merged = dict(DEFAULTS)
    if isinstance(block, dict):
        merged.update(block)
    return merged


def enabled(config: Dict[str, Any]) -> bool:
    return config_for(config).get("enabled") is True
