#!/usr/bin/env python3
"""ADV Loop adapter backed by the `claude` CLI (Claude Code) instead of the raw SDK.

Same contract as adapters/claude_adapter.py: one drive envelope on stdin
({workspace, directive, rejections, retry, charter?}), exactly one attempt-submission
JSON object on stdout.  Every invocation is a fresh `claude -p` session with its own
context, run with the workspace as cwd, so a critic or verifier can never share
context with the attempt it judges.  The model may use Claude Code's own tools
(Read/Bash/Grep/Glob) to inspect artifacts and re-run checks; it cannot mislabel its
identity because directive_id/role/mode/actor are overwritten mechanically here.

Env knobs: ADV_LOOP_MODEL (default claude-opus-5), ADV_LOOP_EFFORT (high),
           ADV_LOOP_CLI_TIMEOUT (3000 s), ADV_LOOP_MAX_BUDGET_USD (15).
"""
from __future__ import annotations
import json, os, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_adapter import BASE_SYSTEM, ROLE_GUIDANCE, REVIEW_CONTEXT_GUIDANCE, _extract_json  # noqa: E402

TOOL_NOTE = """
You are running as a Claude Code session whose working directory is the task workspace.
You may use Read/Grep/Glob/Bash to inspect payload/, artifacts/, sources and to RE-RUN any
script or checker (python interpreter: the repo venv at <REPO>/.venv/bin/python; the
`adv-loop` CLI is at <REPO>/.venv/bin/adv-loop).  Evidence fingerprints must be SHA-256
hashes of files that exist (compute them with `sha256sum`).  Never fabricate a run you did
not perform.  When you are done, your FINAL message must be the single JSON object of the
attempt submission and nothing else (no prose, no code fences).
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_spend(workspace: Path, model: str, out: dict) -> None:
    rec = {"at": _now(), "model": model, "estimated_usd": out.get("total_cost_usd"),
           "usage": out.get("usage"), "session_id": out.get("session_id")}
    try:
        with open(workspace / ".spend.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass


def main() -> None:
    envelope = json.load(sys.stdin)
    directive = envelope["directive"]
    workspace = Path(envelope["workspace"]).resolve()
    repo = Path(__file__).resolve().parent.parent
    model = os.environ.get("ADV_LOOP_MODEL", "claude-opus-5")
    effort = os.environ.get("ADV_LOOP_EFFORT", "high")
    timeout = float(os.environ.get("ADV_LOOP_CLI_TIMEOUT", "3000"))
    budget = os.environ.get("ADV_LOOP_MAX_BUDGET_USD", "15")
    context_id = f"{directive.get('mode', 'attempt')}-{uuid.uuid4().hex[:12]}"

    system_text = BASE_SYSTEM + TOOL_NOTE.replace("<REPO>", str(repo))
    guidance = ROLE_GUIDANCE.get(directive.get("mode", ""))
    if guidance:
        system_text += f"\nRole guidance for {directive.get('role')}/{directive.get('mode')}: {guidance}\n"
    if directive.get("mode") == "attempt_review" and "review_context" in directive:
        system_text += REVIEW_CONTEXT_GUIDANCE
    charter = envelope.get("charter")
    if charter:
        system_text += ("\n--- CHARTER (standing authorization; everything granted here is"
                        " pre-authorized — do not pause to ask for it) ---\n" + charter)

    parts = ["DIRECTIVE (authoritative; follow its instructions exactly):",
             json.dumps(directive, indent=2, sort_keys=True)]
    rejections = envelope.get("rejections") or []
    if rejections:
        parts.append("PREVIOUS SUBMISSIONS WERE REJECTED. Fix exactly these contract errors"
                     " without discarding the underlying work:")
        parts.append(json.dumps(rejections, indent=2, sort_keys=True))
    parts.append("Inspect the workspace with your tools as needed, then reply with the single"
                 " attempt-submission JSON object as your final message.")
    prompt = "\n\n".join(parts)

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",)}
    cmd = ["claude", "-p", "--output-format", "json", "--model", model, "--effort", effort,
           "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions",
           "--allowedTools", "Read", "Bash", "Grep", "Glob", "WebFetch", "WebSearch",
           "--max-budget-usd", str(budget), "--no-session-persistence",
           "--add-dir", str(repo),
           "--append-system-prompt", system_text, prompt]
    result = subprocess.run(cmd, cwd=str(workspace), env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"claude_cli_adapter: claude exited {result.returncode}: {result.stderr[-3000:]}", file=sys.stderr)
        raise SystemExit(3)
    out = json.loads(result.stdout)
    _record_spend(workspace, model, out)
    if out.get("is_error"):
        print(f"claude_cli_adapter: model error: {str(out.get('result'))[-2000:]}", file=sys.stderr)
        raise SystemExit(3)
    text = out.get("result") or ""
    submission = _extract_json(text)
    submission["directive_id"] = directive.get("directive_id")
    submission["role"] = directive.get("role")
    submission["mode"] = directive.get("mode")
    submission["actor"] = {"agent_id": "claude-cli-adapter", "model": model, "context_id": context_id}
    submission.setdefault("request_id", f"claude-cli:{str(directive.get('directive_id', 'D-none'))[-12:]}:{uuid.uuid4().hex[:8]}")
    print(json.dumps(submission))


if __name__ == "__main__":
    main()
