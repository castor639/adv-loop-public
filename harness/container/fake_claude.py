#!/usr/bin/env python3
"""A stand-in for the `claude` binary in drills and tests.

Accepts the real argv, reads the prompt on stdin, runs one Bash-shaped tool
call through the ledger hook so the hook path is exercised, and prints a
stream-json transcript. Scenario from $FAKE_CLAUDE_SCENARIO:
  ok          a valid structured output built from the schema's required keys
  rate_limit  a rejected rate_limit_event and an error result
  error       an error result with no output
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def arg(flag, default=None):
    argv = sys.argv
    return argv[argv.index(flag) + 1] if flag in argv else default


def fill(schema):
    """Smallest object satisfying required keys of a compiled model schema."""

    out = {}
    for key in schema.get("required", []):
        sub = schema.get("properties", {}).get(key, {})
        out[key] = value_for(sub)
    return out


def value_for(sub):
    kind = sub.get("type")
    if "const" in sub:
        return sub["const"]
    if "enum" in sub:
        return sub["enum"][0]
    if kind == "object":
        return fill(sub) if sub.get("required") else {}
    if kind == "array":
        count = sub.get("minItems", 0)
        if count > 0 and isinstance(sub.get("items"), dict):
            return [value_for(sub["items"]) for _ in range(count)]
        return []
    if kind == "string":
        pattern = sub.get("pattern", "")
        if pattern.startswith("^A[0-9]{6}"):
            return "A000001"
        if "[0-9a-f]{64}" in pattern:
            return "0" * 64
        return "fake-claude"
    if kind == "number" or kind == "integer":
        return sub.get("minimum", 0)
    if kind == "boolean":
        return False
    return None


def main():
    session_id = arg("--session-id", "fake-session")
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "ok")
    prompt_text = sys.stdin.read()
    model = arg("--model", "fake-model")
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id, "model": model}))
    # exercise the ledger hook the way Claude Code would
    settings_path = arg("--settings")
    if settings_path and os.path.exists(settings_path):
        settings = json.load(open(settings_path))
        hooks = settings.get("hooks", {}).get("PostToolUse", [])
        for hook in hooks:
            for cmd in hook.get("hooks", []):
                payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "fake-1",
                           "cwd": os.getcwd(), "tool_input": {"command": "echo fake; echo EXIT=$?"},
                           "tool_response": {"stdout": "fake\nEXIT=0\n", "stderr": "", "interrupted": False}}
                subprocess.run(["bash", "-c", cmd["command"]], input=json.dumps(payload), text=True, capture_output=True)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}))
    if scenario == "rate_limit":
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": int(time.time()) + 1800, "rateLimitType": "five_hour"}}))
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "You've hit your usage limit (429)", "session_id": session_id,
                          "usage": {"input_tokens": 0, "output_tokens": 0}, "total_cost_usd": 0.0}))
        return 1
    if scenario == "error":
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "something broke", "session_id": session_id, "usage": {}}))
        return 1
    schema = json.loads(arg("--json-schema", "{}"))
    output = fill(schema)
    # honor the directive the way a real session would: execute the active plan, target a real criterion
    import re
    props = schema.get("properties", {})
    plan = re.search(r'"active_plan_id": "([^"]+)"', prompt_text)
    if plan and "plan_id" in props:
        output["plan_id"] = plan.group(1)
    criteria = re.findall(r'"id": "(C[0-9]+)"', prompt_text)
    if criteria and "criterion_targets" in props:
        output["criterion_targets"] = [criteria[0]]
    if "evidence" in props and criteria:
        relative = os.path.join("payload", "scratch", f"fake-{session_id[:8]}.txt")
        os.makedirs(os.path.dirname(relative), exist_ok=True)
        with open(relative, "w") as handle:
            handle.write(f"fake claude artifact {session_id}\n")
        output["evidence"] = [{"ref": "e1", "kind": "artifact", "quality": "direct", "claim": "the fake artifact exists",
                               "locator": relative, "method": "write", "independence_key": f"fake:{session_id[:8]}",
                               "supports": [criteria[0]], "artifact_path": relative}]
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(output),
                      "structured_output": output, "session_id": session_id, "num_turns": 2,
                      "usage": {"input_tokens": 1200, "output_tokens": 300}, "total_cost_usd": 0.0123}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
