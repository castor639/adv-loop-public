"""From a session result to an attempt submission.

The harness owns identity and observation; the model owns the research
fields. Nothing the model wrote in a harness-owned field survives, evidence
fingerprints are recomputed, and the whole object is checked against the
mode's schema before the kernel ever sees it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import assembler, jsonschema_lite, ledger, schema
from .backends.base import AssembledPrompt, ModelSpec, SessionResult

# The engine rejects an `observation` key on these; their record is proof-inert.
PROOF_INERT_MODES = ("ideation", "triage", "overlay_diagnosis", "overlay_review")


def build_submission(
    directive: Dict[str, Any],
    result: SessionResult,
    prompt: AssembledPrompt,
    *,
    workspace: Path,
    spec: ModelSpec,
    agent_id: str,
    verdicts: Optional[Dict[str, Dict[str, Any]]] = None,
    policy_version: str = "5.0",
) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """Returns (submission, problems, warnings); submission is None when the model gave no output."""

    output = result.model_output
    if not isinstance(output, dict):
        return None, ["the session produced no JSON object"], []
    role, mode = directive["role"], directive["mode"]
    problems = jsonschema_lite.validate(output, prompt.schema)
    allowed = schema.allowed_model_keys(role, mode, policy_version)
    body = {k: v for k, v in output.items() if k in allowed}

    warnings: List[str] = []
    if "evidence" in allowed:
        evidence, ev_problems, ev_warnings = ledger.verify_evidence(
            workspace, body.get("evidence") or [], verdicts=verdicts, ledger=result.tool_ledger)
        problems.extend(ev_problems)
        warnings.extend(ev_warnings)
        body["evidence"] = evidence

    submission: Dict[str, Any] = {
        "request_id": directive["directive_id"],
        "directive_id": directive["directive_id"],
        "role": role,
        "mode": mode,
        "actor": {
            "agent_id": agent_id,
            "model": result.resolved_model or spec.model,
            "context_id": result.session_id,
            "session_id": result.session_id,
            "provider": spec.provider,
            "billing": spec.billing,
            **assembler.actor_provenance(prompt.manifest),
        },
    }
    notes = body.pop("observation_notes", None)
    submission.update(body)
    if mode not in PROOF_INERT_MODES:
        observation = ledger.observation_digest(result.tool_ledger)
        if isinstance(notes, str) and notes.strip():
            observation += "\n\nModel notes on the results (not verified by the harness):\n" + notes.strip()
        submission["observation"] = observation
    return submission, problems, warnings
