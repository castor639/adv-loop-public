"""The http_chat backend against stub transports in both wire formats. No key, no network."""

from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from harness import assembler, jsonschema_lite, schema
from harness.backends import http_chat
from harness.backends.base import ModelSpec, SessionBudget, WorkspaceHandle

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fake_claude", REPO_ROOT / "harness" / "container" / "fake_claude.py")
fake_claude = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_claude)

AZURE_OPENAI = ModelSpec(backend="http_chat", model="gpt-astra", provider="azure", wire="openai",
                         base_url="https://foundry.example", path="/openai/deployments/{model}/chat/completions",
                         api_version="2025-04-01-preview", auth_header="api-key", price_in_usd=2.0, price_out_usd=8.0,
                         effort="high")
AZURE_ANTHROPIC = ModelSpec(backend="http_chat", model="claude-opus-5", provider="azure", wire="anthropic",
                            base_url="https://foundry.example", path="/anthropic/v1/messages",
                            api_version="2025-04-01-preview", auth_header="api-key", price_in_usd=5.0, price_out_usd=25.0,
                            effort="high")


def directive(role="planner", mode="initial_plan"):
    return {"action": "attempt", "directive_id": f"d-{mode}", "role": role, "mode": mode, "task": "t",
            "open_criteria": [{"id": "C1", "text": "c"}], "instructions": [], "active_plan_id": "P0001"}


def valid_output(role, mode):
    return fake_claude.fill(schema.load_cached(role, mode, "5.0"))


def openai_text(text, finish="stop", usage=None):
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": text}}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50}}


def openai_tool(name, arguments, call_id="call-1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


AZURE_RESPONSES = ModelSpec(backend="http_chat", model="gpt-6-astra", provider="azure", wire="responses",
                            base_url="https://res.openai.azure.com", path="/openai/v1/responses", auth_header="bearer",
                            price_in_usd=10.0, price_out_usd=50.0, effort="high")


def responses_call(name, arguments, call_id="call_1"):
    return {"status": "completed", "output": [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []},
        {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments), "status": "completed"}],
        "usage": {"input_tokens": 100, "output_tokens": 40, "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 5},
                  "output_tokens_details": {"reasoning_tokens": 30}}}


def responses_text(text, status="completed"):
    return {"status": status, "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}},
            "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None}


def anthropic_text(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop,
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10}}


def anthropic_tool(name, arguments, call_id="tu-1"):
    return {"content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}], "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 20}}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, copy.deepcopy(body)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class HttpChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "payload").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_backend(self, spec, responses, d=None, budget=None):
        d = d or directive()
        prompt = assembler.assemble(d)
        transport = FakeTransport(responses)
        backend = http_chat.HttpChatBackend(transport=transport, key_reader=lambda s: "KEY-123")
        handle = WorkspaceHandle(ws_id="w", path=self.ws, session_id=uuid.uuid4().hex)
        result = backend.run(d, handle, spec, budget or SessionBudget(), prompt)
        return result, transport, prompt

    def test_openai_wire_on_azure_runs_tools_and_returns_the_object(self):
        output = valid_output("planner", "initial_plan")
        result, transport, prompt = self.run_backend(AZURE_OPENAI, [
            openai_tool("run_in_sandbox", {"command": ["bash", "-c", "echo hi; echo EXIT=$?"]}),
            openai_tool("write_file", {"path": "payload/scratch/a.txt", "content": "x"}, "call-2"),
            openai_text(json.dumps(output)),
        ])
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.model_output, output)
        self.assertEqual(jsonschema_lite.validate(result.model_output, prompt.schema), [])
        url, headers, body = transport.calls[0]
        self.assertEqual(url, "https://foundry.example/openai/deployments/gpt-astra/chat/completions?api-version=2025-04-01-preview")
        self.assertEqual(headers["api-key"], "KEY-123")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("# The harness contract", body["messages"][0]["content"])
        self.assertEqual([e["tool"] for e in result.tool_ledger], ["run_in_sandbox", "write_file"])
        self.assertEqual(result.tool_ledger[0]["exit_code"], 0)
        self.assertEqual(result.tool_ledger[0]["tool_use_id"], "call-1")
        tool_msgs = [m for m in transport.calls[2][2]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertEqual(result.usage["input_tokens"], 300)
        self.assertAlmostEqual(result.cost_usd, (300 * 2.0 + 90 * 8.0) / 1e6)
        self.assertTrue((self.ws / "payload" / "scratch" / "a.txt").is_file())
        self.assertTrue((result.session_dir / "messages.json").is_file())

    def test_anthropic_wire_on_azure(self):
        output = valid_output("critic", "attempt_review")
        d = directive("critic", "attempt_review")
        result, transport, _ = self.run_backend(AZURE_ANTHROPIC, [
            anthropic_tool("read_file", {"path": "payload/none.txt"}),
            anthropic_text(json.dumps(output)),
        ], d=d)
        self.assertTrue(result.ok, result.errors)
        url, headers, body = transport.calls[0]
        self.assertEqual(url, "https://foundry.example/anthropic/v1/messages?api-version=2025-04-01-preview")
        self.assertEqual(headers["x-api-key"], "KEY-123")
        self.assertEqual(headers["anthropic-version"], http_chat.ANTHROPIC_VERSION)
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")
        second = transport.calls[1][2]["messages"]
        self.assertEqual(second[-1]["role"], "user")
        self.assertEqual(second[-1]["content"][0]["type"], "tool_result")
        self.assertIn("error", second[-1]["content"][0]["content"])
        self.assertEqual(result.usage["cache_read_input_tokens"], 10)

    def test_responses_wire_replays_reasoning_and_tool_results(self):
        output = valid_output("planner", "initial_plan")
        result, transport, _ = self.run_backend(AZURE_RESPONSES, [
            responses_call("run_in_sandbox", {"command": ["bash", "-c", "echo hi; echo EXIT=$?"]}),
            responses_text(json.dumps(output)),
        ])
        self.assertTrue(result.ok, result.errors)
        url, headers, body = transport.calls[0]
        self.assertEqual(url, "https://res.openai.azure.com/openai/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer KEY-123")
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertIn("# The harness contract", body["instructions"])
        self.assertEqual(body["max_output_tokens"], 16000)
        self.assertEqual(body["tools"][0]["type"], "function")
        second = transport.calls[1][2]["input"]
        kinds = [item.get("type") or item.get("role") for item in second]
        self.assertEqual(kinds, ["user", "reasoning", "function_call", "function_call_output"])
        self.assertEqual(second[1]["encrypted_content"], "opaque")
        self.assertEqual(second[3]["call_id"], "call_1")
        self.assertEqual(result.tool_ledger[0]["tool_use_id"], "call_1")
        self.assertEqual(result.usage["cache_creation_input_tokens"], 5)
        self.assertEqual(result.usage["input_tokens"], 200)
        result, _, _ = self.run_backend(AZURE_RESPONSES, [responses_text("{", status="incomplete")])
        self.assertEqual(result.ended, "error_during_execution")

    def test_openai_wire_flips_the_token_limit_key_once(self):
        output = valid_output("planner", "initial_plan")
        rejected = http_chat.TransportError(400, '{"error": {"message": "Unsupported parameter: max_completion_tokens is not supported"}}')
        gpt = ModelSpec(**{**AZURE_OPENAI.__dict__, "model": "gpt-6-astra"})
        result, transport, _ = self.run_backend(gpt, [rejected, openai_text(json.dumps(output))])
        self.assertTrue(result.ok, result.errors)
        self.assertIn("max_completion_tokens", transport.calls[0][2])
        self.assertIn("max_tokens", transport.calls[1][2])
        self.assertNotIn("max_completion_tokens", transport.calls[1][2])
        self.assertIn("max_tokens", transport.calls[0][2].keys() | {"max_tokens"})

    def test_grok_reasoning_tokens_count_as_output(self):
        output = valid_output("planner", "initial_plan")
        usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 180,
                 "completion_tokens_details": {"reasoning_tokens": 170}}
        result, _, _ = self.run_backend(AZURE_OPENAI, [openai_text(json.dumps(output), usage=usage)])
        self.assertEqual(result.usage["output_tokens"], 173)

    def test_schema_problems_get_exactly_one_retry(self):
        output = valid_output("planner", "initial_plan")
        broken = {**output, "outcome": "victory"}
        result, transport, _ = self.run_backend(AZURE_OPENAI, [openai_text(json.dumps(broken)), openai_text(json.dumps(output))])
        self.assertTrue(result.ok)
        retry = transport.calls[1][2]["messages"][-1]
        self.assertEqual(retry["role"], "user")
        self.assertIn("rejected by the contract", retry["content"])
        self.assertIn("$.outcome", retry["content"])
        result, _, _ = self.run_backend(AZURE_OPENAI, [openai_text(json.dumps(broken)), openai_text(json.dumps(broken))])
        self.assertEqual(result.ended, "error_max_structured_output_retries")
        self.assertIsNotNone(result.model_output)

    def test_prose_around_the_object_is_tolerated(self):
        output = valid_output("planner", "initial_plan")
        result, _, _ = self.run_backend(AZURE_OPENAI, [openai_text("Here you go:\n```json\n" + json.dumps(output) + "\n```\nDone.")])
        self.assertTrue(result.ok, result.errors)

    def test_rate_limit_and_truncation_and_budget(self):
        result, _, _ = self.run_backend(AZURE_OPENAI, [http_chat.TransportError(429, "slow down")])
        self.assertTrue(result.rate_limited)
        self.assertEqual(result.ended, "rate_limited")
        self.assertEqual(result.api_error_status, 429)
        result, _, _ = self.run_backend(AZURE_OPENAI, [openai_text("{", finish="length")])
        self.assertEqual(result.ended, "error_during_execution")
        pricey = ModelSpec(**{**AZURE_OPENAI.__dict__, "price_out_usd": 1e9})
        result, _, _ = self.run_backend(pricey, [openai_text("{}")], budget=SessionBudget(max_budget_usd=1.0))
        self.assertEqual(result.ended, "error_max_budget_usd")
        result, _, _ = self.run_backend(AZURE_OPENAI, [http_chat.TransportError(500, "boom")])
        self.assertEqual(result.ended, "process_error")
        self.assertEqual(result.api_error_status, 500)

    def test_tool_turn_limit_ends_the_session(self):
        responses = [openai_tool("hash_artifact", {"path": "payload/none"}, f"c{i}") for i in range(6)]
        result, _, _ = self.run_backend(AZURE_OPENAI, responses, budget=SessionBudget(max_tool_turns=2))
        self.assertEqual(result.ended, "error_max_turns")
        self.assertLessEqual(len(result.tool_ledger), 4)

    def test_key_reader_handles_env_style_files(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# azure\nAZURE_API_KEY=\"abc123\"\n")
        spec = ModelSpec(**{**AZURE_OPENAI.__dict__, "key_file": fh.name})
        self.assertEqual(http_chat.read_key(spec), "abc123")
        self.assertEqual(http_chat.headers_for(ModelSpec(backend="b", model="m"), "k"), {"Authorization": "Bearer k"})


if __name__ == "__main__":
    unittest.main()
