"""Claude Code backend: the unmodified `claude` binary on the owner's subscription.

One `claude -p` per directive, fresh session id, stream-json out, the ledger
written by the PostToolUse hook, the final object taken from the structured
output. Subscription roles never see an API key: the environment is scrubbed
and asserted before exec. A rate limit is not retried and not billed; it
returns `rate_limited` with the reset time so the supervisor pauses every
subscription role until then.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from adv_loop.storage import atomic_write_text

from .. import budget as budgeting
from .. import ledger
from .base import AssembledPrompt, ModelSpec, SessionBudget, SessionResult, WorkspaceHandle

ALLOWED_TOOLS = "Read,Write,Edit,Bash,Grep,Glob,WebFetch,WebSearch"
GPU_DIR = ".harness/gpu"
# adv-gpu-run blocks for the whole job; the Bash tool must outlive it plus the collection grace.
GPU_BASH_GRACE_SECONDS = 900
DISALLOWED_TOOLS = "Agent,NotebookEdit,mcp__*"
OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
LEDGER_HOOK = "/usr/local/lib/adv-loop/ledger_hook.py"
RATE_LIMIT_TEXT = re.compile(r"(rate limit|usage limit|limit reached|too many requests|429)", re.IGNORECASE)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def settings_for(hook_path: str, ledger_path: str, validate_dir: str, session_id: str) -> Dict[str, Any]:
    env = f"ADV_LOOP_LEDGER={ledger_path} ADV_LOOP_SESSION_ID={session_id} ADV_LOOP_VALIDATE_DIR={validate_dir}"
    command = f"{env} python3 {hook_path}"
    hook = {"matcher": "Bash|Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": command, "timeout": 20}]}
    return {
        "permissions": {"defaultMode": "bypassPermissions"},
        "sandbox": {"enabled": False},
        "hooks": {"PostToolUse": [hook], "PostToolUseFailure": [hook]},
    }


def claude_argv(spec: ModelSpec, budget: SessionBudget, session_id: str, session_dir: Path,
                schema_path: Path, system_path: Path, repo_mount: Optional[str]) -> List[str]:
    argv = [
        "claude", "-p", "--output-format", "stream-json", "--verbose", "--include-hook-events",
        "--session-id", session_id, "--no-session-persistence",
        "--model", spec.model,
        "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions", "--permission-prompts", "none",
        "--allowedTools", ALLOWED_TOOLS, "--disallowedTools", DISALLOWED_TOOLS, "--strict-mcp-config",
        "--settings", str(session_dir / "settings.json"),
        "--max-budget-usd", str(budget.max_budget_usd),
        "--json-schema", schema_path.read_text(encoding="utf-8"),
        "--append-system-prompt-file", str(system_path),
    ]
    if spec.effort:
        argv += ["--effort", spec.effort]
    if budget.max_turns:
        argv += ["--max-turns", str(budget.max_turns)]
    if repo_mount:
        argv += ["--add-dir", repo_mount]
    return argv


def classify_rate_limit(events: Sequence[Dict[str, Any]], stderr: str) -> Optional[Dict[str, Any]]:
    """Rate-limit evidence in a stream: the event, repeated 429 retries, an error result, or stderr text."""

    retries_429 = 0
    for event in events:
        kind = event.get("type")
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info") or event.get("rate_limit") or event
            if str(info.get("status", "")).lower() in ("rejected", "limited", "exceeded"):
                resets = info.get("resetsAt") or info.get("resets_at") or info.get("reset_at")
                return {"source": "rate_limit_event", "resets_at": _normalize_reset(resets), "raw": info}
        if kind == "api_retry" or event.get("subtype") == "api_retry":
            status = event.get("error_status") or (event.get("error") or {}).get("status")
            if status == 429:
                retries_429 += 1
                if retries_429 >= 2:
                    return {"source": "api_retry", "resets_at": None, "raw": event}
        if kind == "result" and event.get("is_error"):
            text = str(event.get("result") or event.get("error") or "")
            if "429" in text or RATE_LIMIT_TEXT.search(text):
                return {"source": "result", "resets_at": None, "raw": text[:500]}
    if stderr and RATE_LIMIT_TEXT.search(stderr) and ("429" in stderr or "limit" in stderr.lower()):
        return {"source": "stderr", "resets_at": None, "raw": stderr[-500:]}
    return None


def _normalize_reset(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10 ** 11:
            stamp /= 1000.0
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
    return str(value)


def parse_stream(text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


class ClaudeCodeBackend:
    name = "claude_code"

    def __init__(self, *, docker=None, container: Optional[str] = None, claude_binary: Sequence[str] = ("claude",),
                 repo_mount: Optional[str] = "/srv/adv-loop/repo", hook_path: str = LEDGER_HOOK,
                 token_reader=None, runner=subprocess.run, env: Optional[Dict[str, str]] = None,
                 gpu_max_job_seconds: Optional[int] = None) -> None:
        self.docker = docker
        self.container = container
        self.claude_binary = list(claude_binary)
        self.repo_mount = repo_mount
        self.hook_path = hook_path
        self.token_reader = token_reader
        self.runner = runner
        self.env = env
        self.gpu_max_job_seconds = gpu_max_job_seconds

    def session_env(self, handle: WorkspaceHandle, ledger_path: Path, validate_dir: Path) -> Dict[str, str]:
        env = {"ADV_LOOP_LEDGER": str(ledger_path), "ADV_LOOP_SESSION_ID": handle.session_id,
               "ADV_LOOP_VALIDATE_DIR": str(validate_dir), "ADV_LOOP_GPU_DIR": str(handle.path / GPU_DIR),
               "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}
        if self.gpu_max_job_seconds:
            wait_ms = str((int(self.gpu_max_job_seconds) + GPU_BASH_GRACE_SECONDS) * 1000)
            env.update({"ADV_LOOP_GPU_MAX_JOB_SECONDS": str(int(self.gpu_max_job_seconds)),
                        "BASH_DEFAULT_TIMEOUT_MS": wait_ms, "BASH_MAX_TIMEOUT_MS": wait_ms})
        return env

    def _token(self, spec: ModelSpec) -> Optional[str]:
        if self.token_reader:
            return self.token_reader(spec)
        if spec.billing == "subscription" and spec.key_file:
            return Path(spec.key_file).read_text(encoding="utf-8").strip()
        return None

    def run(self, directive: Dict[str, Any], handle: WorkspaceHandle, spec: ModelSpec,
            budget: SessionBudget, prompt: AssembledPrompt) -> SessionResult:
        session_dir = handle.session_dir
        session_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = session_dir / "ledger.jsonl"
        validate_dir = handle.path / ".harness" / "validate"
        schema_path = session_dir / "schema.json"
        system_path = session_dir / "system.md"
        atomic_write_text(schema_path, json.dumps(prompt.schema, sort_keys=True))
        atomic_write_text(system_path, prompt.system_text)
        atomic_write_text(session_dir / "settings.json", json.dumps(
            settings_for(self.hook_path, str(ledger_path), str(validate_dir), handle.session_id), indent=2))

        result = SessionResult(session_id=handle.session_id, started_at=utc_now(), resolved_model=spec.model,
                               session_dir=session_dir)
        argv = claude_argv(spec, budget, handle.session_id, session_dir, schema_path, system_path, self.repo_mount)
        argv = self.claude_binary + argv[1:]

        session_env = self.session_env(handle, ledger_path, validate_dir)
        token = self._token(spec)
        if token:
            session_env[OAUTH_ENV] = token
        if spec.billing == "subscription":
            base_env = budgeting.subscription_env(self.env)
        else:
            base_env = dict(self.env if self.env is not None else os.environ)

        if self.docker is not None and self.container:
            from .. import container as ct
            wall = int(budget.wall_clock_seconds)
            full = ct.exec_prefix(self.container, handle.path, env=session_env) + \
                ["timeout", "--signal=TERM", f"--kill-after=30", str(wall)] + argv
            popen_env = base_env
        else:
            full = argv
            popen_env = {**base_env, **session_env}
        if spec.billing == "subscription":
            budgeting.assert_no_api_billing({k: v for k, v in popen_env.items() if k != OAUTH_ENV})

        try:
            done = self.runner(full, input=prompt.user_text, capture_output=True, text=True,
                               timeout=budget.wall_clock_seconds + 60, env=popen_env, cwd=str(handle.path))
        except subprocess.TimeoutExpired as exc:
            result.timed_out = True
            result.ended = "timeout"
            result.is_error = True
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
            if self.docker is not None and self.container:
                self.docker.kill_processes(self.container, handle.session_id)
            return self._finish(result, stdout, stderr, ledger_path, spec)
        return self._finish(result, done.stdout, done.stderr, ledger_path, spec, exit_code=done.returncode)

    def _finish(self, result: SessionResult, stdout: str, stderr: str, ledger_path: Path, spec: ModelSpec,
                exit_code: Optional[int] = None) -> SessionResult:
        atomic_write_text(result.session_dir / "stream.jsonl", stdout)
        atomic_write_text(result.session_dir / "stderr.txt", stderr[-20000:])
        events = parse_stream(stdout)
        result.exit_code = exit_code
        result.stderr_tail = stderr[-3000:]
        result.tool_ledger = ledger.load(ledger_path) if ledger_path.is_file() else []
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if final:
            result.usage = final.get("usage") or {}
            if final.get("total_cost_usd") is not None:
                result.usage["total_cost_usd"] = final["total_cost_usd"]
            result.cost_usd = 0.0 if spec.billing == "subscription" else final.get("total_cost_usd")
            result.is_error = bool(final.get("is_error"))
            subtype = final.get("subtype") or "success"
            result.ended = subtype if subtype in ("success", "error_max_turns", "error_max_budget_usd",
                                                  "error_max_structured_output_retries", "error_during_execution") \
                else ("error_during_execution" if result.is_error else "success")
            output = final.get("structured_output")
            if output is None and isinstance(final.get("result"), str):
                try:
                    from .http_chat import extract_json
                    output = extract_json(final["result"])
                except ValueError:
                    output = None
            result.model_output = output if isinstance(output, dict) else None
            init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
            if init and init.get("model"):
                result.resolved_model = init["model"]
        elif not result.timed_out:
            result.ended = "process_error" if exit_code else "no_output"
            result.is_error = True
        limit = classify_rate_limit(events, stderr)
        if limit:
            result.rate_limited = True
            result.rate_limit = limit
            result.ended = "rate_limited"
            result.is_error = True
            result.cost_usd = 0.0
        if exit_code and not result.errors:
            result.errors.append(f"claude exited {exit_code}")
        result.finished_at = utc_now()
        return result
