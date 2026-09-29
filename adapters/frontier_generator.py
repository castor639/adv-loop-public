#!/usr/bin/env python3
"""Reference frontier generator: portfolio signals in, new goal briefs out.

Reads the frontier envelope from stdin ({protocol, fleet, max_proposals}),
asks Claude to propose the highest-leverage next problems — reopening blocked
premises from a new angle, attacking named barriers, cashing in wishes,
extending completed results — and prints {"proposals": [{name, brief,
charter?}]}. Each brief is written in the shape the charter compiler expects
(task statement, measurable criteria under a "## Acceptance criteria" heading),
so intake can consume it directly.

Usage:    adv-loop frontier --root workspaces --out drops/incoming --apply -- python3 adapters/frontier_generator.py
Requires: pip install anthropic  (and ANTHROPIC_API_KEY or `ant auth login`)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict

DEFAULT_MODEL = "claude-opus-5"

SYSTEM = """You are the frontier generator of an autonomous research fleet.
Given signals from every workspace (completed summaries, open wishes, named barriers,
blocked premises), propose the highest-leverage NEXT problems as new goal briefs.

Rules:
- At most max_proposals proposals; fewer is fine; zero is honest when nothing is worth opening.
- Each proposal: {"name": "<kebab-case-slug>", "brief": "<markdown>", "charter": {...} | null}.
- A brief starts with a one-line "# <task statement>" and MUST contain a
  "## Acceptance criteria" section with measurable bullet criteria — never aspirational verbs.
- Do not re-propose work an existing workspace already covers; extend, connect, or reopen it.
- Output EXACTLY ONE JSON object: {"proposals": [...]}. No prose, no code fences."""


def _sdk_transport() -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    try:
        import anthropic
    except ImportError:
        print("frontier_generator: pip install anthropic", file=sys.stderr)
        raise SystemExit(3)
    client = anthropic.Anthropic()

    def transport(request: Dict[str, Any]) -> Dict[str, Any]:
        return client.messages.create(**request).to_dict()

    return transport


def generate(envelope: Dict[str, Any],
             transport: Callable[[Dict[str, Any]], Dict[str, Any]],
             model: str = None) -> Dict[str, Any]:
    model = model or os.environ.get("ADV_LOOP_MODEL", DEFAULT_MODEL)
    response = transport({
        "model": model,
        "max_tokens": int(os.environ.get("ADV_LOOP_MAX_TOKENS", 16000)),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": os.environ.get("ADV_LOOP_EFFORT", "high")},
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "messages": [{
            "role": "user",
            "content": "FLEET SIGNALS:\n" + json.dumps(envelope, indent=2, sort_keys=True)
                       + f"\n\nPropose at most {envelope.get('max_proposals', 5)} new goal briefs now.",
        }],
    })
    if response.get("stop_reason") == "refusal":
        details = response.get("stop_details") or {}
        raise RuntimeError(f"model refused (category={details.get('category')})")
    text = "".join(block.get("text", "") for block in response.get("content", [])
                   if block.get("type") == "text").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    if start > 0:
        text = text[start:]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise RuntimeError("generator output must be a JSON object")
    return value


def main() -> None:
    envelope = json.load(sys.stdin)
    print(json.dumps(generate(envelope, _sdk_transport())))


if __name__ == "__main__":
    main()
