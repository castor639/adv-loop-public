"""The claude_code backend against the fake binary: argv shape, hook ledger, structured output, rate limits, no API key."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from harness import assembler, jsonschema_lite
from harness.backends import claude_code
from harness.backends.base import ModelSpec, SessionBudget, WorkspaceHandle

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE = REPO_ROOT / "harness" / "container" / "fake_claude.py"
HOOK = REPO_ROOT / "harness" / "container" / "ledger_hook.py"
SUB = ModelSpec(backend="claude_code", model="claude-opus-5", provider="anthropic_subscription", wire="anthropic",
                billing="subscription", effort="high")


def directive():
    return {"action": "attempt", "directive_id": "d-plan", "role": "planner", "mode": "initial_plan", "task": "t",
            "open_criteria": [], "instructions": []}


class RecordingRunner:
    def __init__(self):
        self.env = None
        self.argv = None

    def __call__(self, argv, **kwargs):
        self.argv, self.env = argv, kwargs.get("env")
        return subprocess.run(argv, **kwargs)


class ClaudeCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.prompt = assembler.assemble(directive())

    def tearDown(self):
        self.tmp.cleanup()

    def backend(self, scenario="ok", extra_env=None):
        env = {"PATH": os.environ.get("PATH", ""), "FAKE_CLAUDE_SCENARIO": scenario, "HOME": self.tmp.name,
               "ANTHROPIC_API_KEY": "sk-should-never-leak", "CLAUDE_CODE_USE_BEDROCK": "1", **(extra_env or {})}
        runner = RecordingRunner()
        backend = claude_code.ClaudeCodeBackend(claude_binary=[sys.executable, str(FAKE)], repo_mount=None,
                                                hook_path=str(HOOK), token_reader=lambda s: "oauth-token-value",
                                                runner=runner, env=env)
        return backend, runner

    def run_session(self, scenario="ok"):
        backend, runner = self.backend(scenario)
        handle = WorkspaceHandle(ws_id="w", path=self.ws, session_id=uuid.uuid4().hex)
        return backend.run(directive(), handle, SUB, SessionBudget(max_budget_usd=3), self.prompt), runner, handle

    def test_success_path_yields_schema_valid_output_and_a_hook_written_ledger(self):
        result, runner, handle = self.run_session()
        self.assertTrue(result.ok, (result.ended, result.errors, result.stderr_tail))
        self.assertEqual(jsonschema_lite.validate(result.model_output, self.prompt.schema), [])
        self.assertEqual(len(result.tool_ledger), 1)
        self.assertEqual(result.tool_ledger[0]["tool"], "Bash")
        self.assertEqual(result.tool_ledger[0]["exit_code"], 0)
        self.assertEqual(result.cost_usd, 0.0, "subscription sessions bill nothing")
        self.assertEqual(result.usage["total_cost_usd"], 0.0123)
        self.assertTrue((handle.session_dir / "stream.jsonl").is_file())
        self.assertTrue((handle.session_dir / "settings.json").is_file())
        argv = runner.argv
        self.assertEqual(argv[argv.index("--allowedTools") + 1], claude_code.ALLOWED_TOOLS)
        self.assertEqual(argv[argv.index("--session-id") + 1], handle.session_id)
        self.assertIn("--json-schema", argv)
        self.assertIn("--include-hook-events", argv)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3")
        self.assertNotIn("--bare", argv)

    def test_subscription_env_carries_the_token_and_never_an_api_key(self):
        _, runner, _ = self.run_session()
        self.assertEqual(runner.env["CLAUDE_CODE_OAUTH_TOKEN"], "oauth-token-value")
        self.assertEqual(runner.env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", runner.env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", runner.env)
        self.assertNotIn("CLAUDECODE", runner.env)

    def test_rate_limit_is_detected_not_retried_and_not_billed(self):
        result, _, _ = self.run_session("rate_limit")
        self.assertTrue(result.rate_limited)
        self.assertEqual(result.ended, "rate_limited")
        self.assertEqual(result.rate_limit["source"], "rate_limit_event")
        self.assertRegex(result.rate_limit["resets_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(result.cost_usd, 0.0)
        self.assertIsNone(result.model_output)

    def test_error_result_is_an_error_not_a_rate_limit(self):
        result, _, _ = self.run_session("error")
        self.assertFalse(result.rate_limited)
        self.assertTrue(result.is_error)
        self.assertEqual(result.ended, "error_during_execution")
        self.assertFalse(result.ok)

    def test_classify_rate_limit_variants(self):
        self.assertIsNone(claude_code.classify_rate_limit([{"type": "result", "is_error": False}], ""))
        self.assertEqual(claude_code.classify_rate_limit(
            [{"type": "api_retry", "error_status": 429}, {"type": "api_retry", "error_status": 429}], "")["source"], "api_retry")
        self.assertEqual(claude_code.classify_rate_limit([], "Error: 429 Too Many Requests")["source"], "stderr")
        info = claude_code.classify_rate_limit([{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resets_at": "2026-09-09T13:00:00Z"}}], "")
        self.assertEqual(info["resets_at"], "2026-09-09T13:00:00Z")
        self.assertIsNone(claude_code.classify_rate_limit([{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}], ""))

    def test_container_argv_wraps_claude_in_docker_exec_with_timeout(self):
        class FakeDocker:
            def kill_processes(self, *a):
                pass
        captured = {}

        def runner(argv, **kwargs):
            captured["argv"], captured["env"] = argv, kwargs["env"]
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        backend = claude_code.ClaudeCodeBackend(docker=FakeDocker(), container="adv-ws-demo", token_reader=lambda s: "tok",
                                                runner=runner, env={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-leak"})
        handle = WorkspaceHandle(ws_id="w", path=self.ws, session_id="sid1", container="adv-ws-demo")
        result = backend.run(directive(), handle, SUB, SessionBudget(wall_clock_seconds=120), self.prompt)
        argv = captured["argv"]
        self.assertEqual(argv[:2], ["docker", "exec"])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=tok", argv)
        self.assertIn("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1", argv)
        self.assertEqual(argv[argv.index("timeout") + 3], "120")
        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])
        self.assertEqual(result.ended, "no_output")


if __name__ == "__main__":
    unittest.main()
