#!/usr/bin/env python3
"""Fireworks AI adapter for ADV Loop: the same harness guarantees, a different brain.

Reads one drive envelope from stdin ({workspace, directive, rejections, retry,
charter?}), calls a model on Fireworks' OpenAI-compatible chat-completions API
(stdlib urllib — no extra dependency), runs the same contained sandbox tools,
and prints exactly one attempt-submission JSON object to stdout. As in the
Claude adapter, identity is mechanical: fresh ``context_id`` per invocation,
``directive_id``/``role``/``mode``/``actor`` overwritten by the harness.

Usage:      adv-loop drive workspaces/<id> -- python3 adapters/fireworks_adapter.py
            adv-loop fleet --root workspaces --watch -- python3 adapters/fireworks_adapter.py
Requires:   FIREWORKS_API_KEY in the environment (fw_... key).
Env knobs:  FIREWORKS_MODEL   (default accounts/fireworks/models/deepseek-v4-flash-0731)
            FIREWORKS_BASE_URL (default https://api.fireworks.ai/inference/v1)
            ADV_LOOP_MAX_TOKENS (16000), FIREWORKS_TEMPERATURE (optional)
            ADV_LOOP_PRICE_IN_USD / ADV_LOOP_PRICE_OUT_USD — $ per 1M tokens for
            the chosen model; set these so spend ledgers and fleet spend
            ceilings can bind (unset -> tokens recorded, estimated cost null).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_adapter import (  # noqa: E402  (shared harness pieces, provider-neutral)
    BASE_SYSTEM,
    MAX_TOOL_TURNS,
    ROLE_GUIDANCE,
    TOOL_DEFINITIONS,
    _extract_json,
    _now,
    run_tool,
    scaffold_hint,
)

DEFAULT_MODEL = "accounts/fireworks/models/deepseek-v4-flash-0731"
DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MAX_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 600

# The same tools, expressed in the OpenAI-compatible function schema.
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }
    for tool in TOOL_DEFINITIONS
]


def _record_spend(workspace: Path, model: str, usage: Dict[str, Any]) -> None:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    estimated = None
    try:
        price_in = float(os.environ["ADV_LOOP_PRICE_IN_USD"])
        price_out = float(os.environ["ADV_LOOP_PRICE_OUT_USD"])
        estimated = round((prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)
    except (KeyError, ValueError):
        pass
    record = {
        "at": _now(),
        "model": model,
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "estimated_usd": estimated,
    }
    try:
        with open(workspace / ".spend.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def _http_transport() -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    api_key = os.environ.get("FIREWORKS_API_KEY", "").strip()
    if not api_key:
        print("fireworks_adapter: FIREWORKS_API_KEY is not set", file=sys.stderr)
        raise SystemExit(3)
    base_url = os.environ.get("FIREWORKS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def transport(request_body: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"Fireworks API error {exc.code}: {body}") from exc

    return transport


def build_submission(
    envelope: Dict[str, Any],
    transport: Callable[[Dict[str, Any]], Dict[str, Any]],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """One directive in, one submission out (the engine re-validates everything)."""

    directive = envelope["directive"]
    workspace = Path(envelope["workspace"])
    model = model or os.environ.get("FIREWORKS_MODEL", DEFAULT_MODEL)
    max_tokens = int(os.environ.get("ADV_LOOP_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    context_id = f"{directive.get('mode', 'attempt')}-{uuid.uuid4().hex[:12]}"

    system_text = BASE_SYSTEM
    guidance = ROLE_GUIDANCE.get(directive.get("mode", ""))
    if guidance:
        system_text += f"\nRole guidance for {directive.get('role')}/{directive.get('mode')}: {guidance}\n"
    charter = envelope.get("charter")
    if charter:
        system_text += (
            "\n--- CHARTER (standing authorization; everything granted here is"
            " pre-authorized — do not pause to ask for it) ---\n" + charter
        )

    user_parts = [
        "DIRECTIVE (authoritative; follow its instructions exactly):",
        json.dumps(directive, indent=2, sort_keys=True),
    ]
    hint = scaffold_hint(directive)
    if hint:
        user_parts.append(hint)
    rejections = envelope.get("rejections") or []
    if rejections:
        user_parts.append(
            "PREVIOUS SUBMISSIONS WERE REJECTED. Fix exactly these contract errors"
            " without discarding the underlying work:")
        user_parts.append(json.dumps(rejections, indent=2, sort_keys=True))
    user_parts.append(
        "Reply with the single attempt-submission JSON object now, or use the tools"
        " first if the work needs real execution.")

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    request_base: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "tools": OPENAI_TOOLS,
        "messages": messages,
    }
    temperature = os.environ.get("FIREWORKS_TEMPERATURE")
    if temperature is not None:
        request_base["temperature"] = float(temperature)

    final_text = None
    for _turn in range(MAX_TOOL_TURNS):
        response = transport({**request_base, "messages": messages})
        _record_spend(workspace, model, response.get("usage") or {})
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            messages.append(message)
            for call in tool_calls:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    outcome: Dict[str, Any] = {"error": f"unparseable tool arguments: {exc}"}
                else:
                    outcome = run_tool(workspace, function.get("name", ""), arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(outcome, sort_keys=True),
                })
            continue
        if finish_reason == "length":
            raise RuntimeError("model output was truncated at max_tokens; raise ADV_LOOP_MAX_TOKENS")
        # Reasoning-split models may leave content empty and put everything in
        # reasoning_content; the submission JSON is wherever the text is.
        final_text = message.get("content") or message.get("reasoning_content") or ""
        break
    if not final_text:
        raise RuntimeError("model produced no final submission text")

    submission = _extract_json(final_text)
    # The harness guarantees these mechanically; the model cannot mislabel them.
    submission["directive_id"] = directive.get("directive_id")
    submission["role"] = directive.get("role")
    submission["mode"] = directive.get("mode")
    submission["actor"] = {
        "agent_id": "fireworks-reference-adapter",
        "model": model,
        "context_id": context_id,
    }
    submission.setdefault(
        "request_id",
        f"fireworks:{directive.get('directive_id', 'D-none')[-12:]}:{uuid.uuid4().hex[:8]}",
    )
    return submission


def main() -> None:
    envelope = json.load(sys.stdin)
    submission = build_submission(envelope, _http_transport())
    print(json.dumps(submission))


if __name__ == "__main__":
    main()
