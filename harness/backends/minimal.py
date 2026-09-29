"""Deterministic backend for drills and tests: no model, bash only.

Runs one recorded command, writes one artifact, and returns the shape the
mode needs from the engine's own scaffold. With `lie=True` it names a
fingerprint it did not compute, so a drill can prove the harness rejects it
before the kernel is asked. With `gpu=True` the researcher session runs one
job through `gpu_run`, asks `gpu-replay` to attest the pulled output, and
cites the verdict, so a GPU drill exercises the whole evidence path.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from adv_loop.cli import submission_scaffold

from .. import schema, tools
from .base import AssembledPrompt, ModelSpec, SessionBudget, SessionResult, WorkspaceHandle


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


GPU_OUTPUT = "payload/scratch/out.txt"
GPU_COMMAND = ["bash", "-c", "echo gpu-drill > out.txt; nvidia-smi -L 2>/dev/null; echo EXIT=$?"]
# A drill job is seconds long; the cap keeps the projected hours inside a small drill grant.
GPU_TIMEOUT_SECONDS = 600


class MinimalBackend:
    name = "minimal"

    def __init__(self, *, lie: bool = False, gpu: bool = False, tool_runner_factory=None,
                 policy_version: str = "5.0") -> None:
        self.lie = lie
        self.gpu = gpu
        self.tool_runner_factory = tool_runner_factory
        self.policy_version = policy_version

    @staticmethod
    def _gpu_round_trip(runner: tools.ToolRunner) -> Dict[str, Any]:
        """One job, one attestation; the verdict view is what the evidence cites."""

        job = runner.run("gpu_run", {"command": GPU_COMMAND, "cwd": "payload/scratch", "outputs": [GPU_OUTPUT],
                                     "timeout_seconds": GPU_TIMEOUT_SECONDS, "label": "drill"}, tool_use_id="m3")
        return runner.run("validate", {"hook": "gpu-replay",
                                       "input": {"job_id": job.get("job_id"), "artifact": GPU_OUTPUT}}, tool_use_id="m4")

    def run(self, directive: Dict[str, Any], handle: WorkspaceHandle, spec: ModelSpec,
            budget: SessionBudget, prompt: AssembledPrompt) -> SessionResult:
        handle.session_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = handle.session_dir / "ledger.jsonl"
        runner = self.tool_runner_factory(handle, ledger_path) if self.tool_runner_factory \
            else tools.ToolRunner(handle.path, ledger_path)
        result = SessionResult(session_id=handle.session_id, started_at=utc_now(), resolved_model=spec.model,
                               session_dir=handle.session_dir)
        role, mode = directive["role"], directive["mode"]
        relative = f"payload/scratch/minimal-{handle.session_id[:8]}.txt"
        runner.run("run_in_sandbox", {"command": ["bash", "-c", "echo minimal-backend; echo EXIT=$?"]}, tool_use_id="m1")
        written = runner.run("write_file", {"path": relative, "content": f"minimal backend artifact {handle.session_id}\n"},
                             tool_use_id="m2")
        verdict = self._gpu_round_trip(runner) if self.gpu and role == "researcher" else None

        scaffold = submission_scaffold(directive)
        allowed = schema.allowed_model_keys(role, mode, self.policy_version)
        output: Dict[str, Any] = {k: v for k, v in scaffold.items() if k in allowed}
        if "observation_notes" in allowed:
            output["observation_notes"] = "minimal backend ran one echo and wrote one artifact"
        if "outcome" in allowed:
            output["outcome"] = "no_progress"
        if "uncertainties" in allowed:
            output["uncertainties"] = ["this is a drill"]
        if role == "researcher":
            entry: Dict[str, Any] = {
                "ref": "e1", "kind": "artifact", "quality": "direct", "claim": "the drill artifact exists",
                "locator": relative, "method": "write_file", "independence_key": f"minimal:{handle.session_id[:8]}",
                "supports": [c["id"] for c in directive.get("open_criteria", [])[:1]],
                "artifact_path": relative,
            }
            if self.lie:
                entry["fingerprint"] = "0" * 64
            elif verdict is not None:
                entry.pop("artifact_path")
                entry.update({"kind": "checker-attested", "claim": "the GPU job output is what the job record says",
                              "locator": GPU_OUTPUT, "method": "gpu_run then validate gpu-replay",
                              "verdict_id": verdict.get("verdict_id")})
            output["evidence"] = [entry]
            output.setdefault("criterion_updates", [])
        result.model_output = output
        result.tool_ledger = list(runner.entries)
        result.usage = {"input_tokens": 0, "output_tokens": 0}
        result.cost_usd = 0.0
        result.finished_at = utc_now()
        result.messages = [{"role": "harness", "content": "minimal backend", "artifact": written}]
        return result
