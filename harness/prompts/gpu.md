---
prompt_id: gpu
version: 1
applies_to: gpu-enabled workspaces
---
# GPU compute

This workspace holds a GPU grant. The box is one AWS g5.xlarge with a single NVIDIA A10G (24 GB), started by the harness for a job and stopped when idle. Jobs run inside the pinned image recorded in `sandbox/gpu.lock` together with the driver and CUDA version; a job is refused before any hours are spent when the box no longer matches that lock. Nothing persists on the box between jobs except a shared pip and Hugging Face cache.

## Running a job

`gpu_run` runs one argv command on the box. Declare `inputs` (workspace-relative files pushed to the box; nothing else exists there) and `outputs` (workspace-relative paths pulled back, hashed, and written to the session ledger). `cwd` defaults to `payload/scratch`. A worked call:

```json
{"command": ["python3", "payload/scratch/train.py", "--seed", "7", "--out", "payload/scratch/run7"],
 "inputs": ["payload/scratch/train.py", "payload/data/train.npz"],
 "outputs": ["payload/scratch/run7/metrics.json", "payload/scratch/run7/model.pt"],
 "timeout_seconds": 7200, "label": "seed 7 baseline"}
```

The tool blocks until the job ends; never poll or sleep around it, and never start a second job to check on the first. The result carries `status`, `returncode`, stdout and stderr tails, the pulled `outputs` with their sha256, `missing_outputs`, the observed `environment`, `hours_billed`, `hours_remaining`, and a `validate_hint` naming the replay hook.

## Caps

- The charter grants a number of GPU hours (`harness.gpu.max_hours`); a job whose timeout would cross the remaining hours is refused with `gpu_hours_cap`.
- One job runs at most `max_job_seconds` (four hours by default); `timeout_seconds` is clamped to it and the box kills the job past it.
- Pulled outputs total at most `max_output_bytes` (2 GiB); a larger result has to be summarized by the job itself.
- A refused job costs nothing and names its reason (`gpu_hours_cap`, `aws_gpu_stop`, `busy`, `environment_drift`, and the rest in the system reference).

## What a job proves

A pulled output is an observation until a checker accepts it. Cite it as `artifact_path` and the harness hashes it; run `validate` with hook `gpu-replay` and the job id to obtain a verdict id, the only form the kernel counts as checker evidence. Replication is a second job with the recorded config and a fresh seed, compared through the `gpu-replicate` hook. Every job is recorded under `.harness/gpu/jobs/<job_id>/` with `job.json`, both logs, and an immutable per-job copy of its outputs.

## Recovering a job

A job keeps running when the session that started it ends first. The supplemental block lists jobs awaiting collection; `gpu_collect` with the `job_id` pulls and hashes the declared outputs once and marks the job collected. Call `gpu_status` before deciding to start a job: it reports hours used and remaining, whether the fleet budget allows a start, and every recorded job, without starting the box.

## Claude Code sessions

In a Claude Code session the same request goes through the `adv-gpu-run` command (`adv-gpu-run --help` lists its flags). It blocks for the length of the job, so pass the Bash tool's `timeout` parameter in milliseconds as the job timeout plus 900 seconds.
