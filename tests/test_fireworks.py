"""The Fireworks adapter keeps the same harness guarantees as the Claude one.

All tests inject a fake OpenAI-shaped transport — no key, no network. Pinned:
mechanical identity, the tool-call round trip in the OpenAI wire format,
truncation refusal, and the env-priced spend ledger.
"""

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_engine import Harness
except ImportError:  # invoked as tests.test_fireworks rather than via discovery
    from tests.test_engine import Harness

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "fireworks_adapter", REPO_ROOT / "adapters" / "fireworks_adapter.py"
)
fireworks_adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fireworks_adapter)

SUBMISSION_TEXT = json.dumps({
    "request_id": "model-chosen-id",
    "hypothesis": "h", "action": "a", "observation": "o",
})


def text_choice(text, finish_reason="stop", usage=None):
    return {
        "choices": [{"finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": text}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50},
    }


def tool_choice(name, arguments, call_id="call-1"):
    return {
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}],
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


class FireworksAdapterTests(unittest.TestCase):
    def envelope(self, harness):
        directive = harness.engine.next_instruction()
        return {
            "workspace": str(harness.engine.workspace),
            "directive": directive,
            "rejections": [],
            "retry": 0,
        }, directive

    def test_identity_is_mechanical_and_the_wire_format_is_openai_shaped(self):
        harness = Harness(self)
        envelope, directive = self.envelope(harness)
        transport = FakeTransport([text_choice(SUBMISSION_TEXT)])
        submission = fireworks_adapter.build_submission(
            envelope, transport, model="accounts/fireworks/models/kimi-k2-instruct"
        )
        self.assertEqual(submission["directive_id"], directive["directive_id"])
        self.assertEqual(submission["actor"]["agent_id"], "fireworks-reference-adapter")
        self.assertTrue(submission["actor"]["context_id"].startswith(directive["mode"]))
        request = transport.requests[0]
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(request["tools"][0]["type"], "function")
        self.assertIn(request["tools"][0]["function"]["name"], {"run_in_sandbox", "hash_artifact"})

    def test_tool_calls_round_trip_as_role_tool_messages(self):
        harness = Harness(self)
        envelope, _ = self.envelope(harness)
        (harness.engine.workspace / "payload").mkdir()
        transport = FakeTransport([
            tool_choice("run_in_sandbox", {
                "command": ["python3", "-c",
                            "open('payload/marker.txt', 'w').write('fireworks tool ran')"],
            }),
            text_choice(SUBMISSION_TEXT),
        ])
        fireworks_adapter.build_submission(envelope, transport, model="m")
        self.assertEqual(
            (harness.engine.workspace / "payload" / "marker.txt").read_text(),
            "fireworks tool ran",
        )
        follow_up = transport.requests[1]["messages"]
        self.assertEqual(follow_up[-1]["role"], "tool")
        self.assertEqual(follow_up[-1]["tool_call_id"], "call-1")
        self.assertEqual(follow_up[-2]["role"], "assistant")

    def test_unparseable_tool_arguments_become_error_results_not_crashes(self):
        harness = Harness(self)
        envelope, _ = self.envelope(harness)
        broken = tool_choice("run_in_sandbox", {})
        broken["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
        transport = FakeTransport([broken, text_choice(SUBMISSION_TEXT)])
        fireworks_adapter.build_submission(envelope, transport, model="m")
        result = json.loads(transport.requests[1]["messages"][-1]["content"])
        self.assertIn("unparseable tool arguments", result["error"])

    def test_truncated_output_is_refused(self):
        harness = Harness(self)
        envelope, _ = self.envelope(harness)
        transport = FakeTransport([text_choice("{\"partial\":", finish_reason="length")])
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            fireworks_adapter.build_submission(envelope, transport, model="m")

    def test_spend_ledger_prices_from_env(self):
        harness = Harness(self)
        envelope, _ = self.envelope(harness)
        usage = {"prompt_tokens": 1000, "completion_tokens": 2000}
        with mock.patch.dict(os.environ, {"ADV_LOOP_PRICE_IN_USD": "1.0",
                                          "ADV_LOOP_PRICE_OUT_USD": "3.0"}):
            fireworks_adapter.build_submission(
                envelope, FakeTransport([text_choice(SUBMISSION_TEXT, usage=usage)]), model="m"
            )
        record = json.loads(
            (harness.engine.workspace / ".spend.jsonl").read_text().splitlines()[0]
        )
        self.assertEqual(record["input_tokens"], 1000)
        self.assertAlmostEqual(record["estimated_usd"], 0.007)

        envelope2, _ = self.envelope(harness)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADV_LOOP_PRICE_IN_USD", None)
            os.environ.pop("ADV_LOOP_PRICE_OUT_USD", None)
            fireworks_adapter.build_submission(
                envelope2, FakeTransport([text_choice(SUBMISSION_TEXT, usage=usage)]), model="m"
            )
        record = json.loads(
            (harness.engine.workspace / ".spend.jsonl").read_text().splitlines()[1]
        )
        self.assertIsNone(record["estimated_usd"])


if __name__ == "__main__":
    unittest.main()
