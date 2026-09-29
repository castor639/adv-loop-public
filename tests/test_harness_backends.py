from __future__ import annotations

import unittest
from pathlib import Path

from harness.backends import ModelSpec, SessionBudget, SessionResult, WorkspaceHandle


class ModelSpecTests(unittest.TestCase):
    def test_pricing_is_optional_and_costs_are_computed_per_million(self):
        unpriced = ModelSpec(backend="http_chat", model="gpt-astra", provider="azure")
        self.assertFalse(unpriced.priced)
        self.assertIsNone(unpriced.cost_usd(1000, 1000))
        priced = ModelSpec(backend="http_chat", model="gpt-astra", provider="azure",
                           price_in_usd=2.0, price_out_usd=8.0)
        self.assertEqual(priced.cost_usd(1_000_000, 500_000), 6.0)
        self.assertEqual(priced.cost_usd(0, 0, cache_read_tokens=1_000_000), 0.2)

    def test_invalid_billing_wire_or_provider_is_refused(self):
        for kwargs in ({"billing": "free"}, {"wire": "grpc"}, {"provider": "mystery"}):
            with self.assertRaises(ValueError):
                ModelSpec(backend="http_chat", model="m", **kwargs)
        with self.assertRaises(ValueError):
            ModelSpec(backend="", model="m")

    def test_subscription_spec_is_expressible(self):
        spec = ModelSpec(backend="claude_code", model="opus", provider="anthropic_subscription",
                         wire="anthropic", billing="subscription", effort="high")
        self.assertEqual(spec.billing, "subscription")
        self.assertFalse(spec.priced)


class SessionResultTests(unittest.TestCase):
    def test_ok_requires_success_output_and_no_rate_limit(self):
        self.assertTrue(SessionResult(session_id="s", model_output={"a": 1}).ok)
        self.assertFalse(SessionResult(session_id="s", model_output=None).ok)
        self.assertFalse(SessionResult(session_id="s", model_output={}, rate_limited=True).ok)
        self.assertFalse(SessionResult(session_id="s", ended="timeout", model_output={}).ok)

    def test_unknown_ending_is_refused(self):
        with self.assertRaises(ValueError):
            SessionResult(session_id="s", ended="exploded")

    def test_handle_session_dir_is_under_the_workspace(self):
        handle = WorkspaceHandle(ws_id="w", path=Path("/tmp/ws"), session_id="abc")
        self.assertEqual(handle.session_dir, Path("/tmp/ws/.harness/sessions/abc"))
        self.assertEqual(SessionBudget().max_tool_turns, 12)


if __name__ == "__main__":
    unittest.main()
