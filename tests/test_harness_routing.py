"""Role routing, the two budgets, and pauses."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import budget, pause, routing
from harness.backends.base import ModelSpec

PRICES = {"gpt-6-astra": {"input": 2.0, "output": 8.0}, "grok-4-6": {"input": 2.0, "output": 6.0},
          "deepseek-v4-pro": {"input": 1.74, "output": 3.48}, "deepseek-v4-flash": {"input": 0.19, "output": 0.51},
          "claude-opus-5": {"input": 5.0, "output": 25.0}}
CLAUDE_MAX = {"backend": "claude_code", "provider": "anthropic_subscription", "wire": "anthropic", "model": "claude-opus-5",
              "billing": "subscription", "key_file": "/etc/adv-loop/claude-oauth.token", "effort": "high"}
ENV = {"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com", "AZURE_AI_ENDPOINT": "https://res.services.ai.azure.com"}


def router(config=None, **kw):
    return routing.Router(config, prices=PRICES, env=ENV, token_exists=kw.pop("token_exists", lambda p: True), **kw)


class RoutingTests(unittest.TestCase):
    def test_non_login_process_loads_endpoint_file_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            endpoint_file = Path(tmp) / "harness"
            endpoint_file.write_text('export AZURE_OPENAI_ENDPOINT="https://res.openai.azure.com"\n'
                                     "AZURE_AI_ENDPOINT='https://res.services.ai.azure.com' # comment\n"
                                     'AZURE_API_KEY=must-not-enter-environment\n')
            with patch.dict(os.environ, {}, clear=True), patch.object(routing, "ENDPOINTS_FILE", endpoint_file):
                r = routing.Router(prices=PRICES)
                self.assertEqual(r.resolve("planner").spec.base_url, ENV["AZURE_OPENAI_ENDPOINT"])
                self.assertEqual(r.resolve("critic").spec.base_url, ENV["AZURE_AI_ENDPOINT"])
                self.assertEqual(r.resolve("explorer").spec.base_url, ENV["AZURE_AI_ENDPOINT"])
                self.assertNotIn("AZURE_API_KEY", r.env)
                self.assertNotIn("AZURE_OPENAI_ENDPOINT", os.environ)
                with self.assertRaises(routing.RoutingError):
                    routing.Router(prices=PRICES, env={}).resolve("planner")

    def test_environment_overrides_file_and_missing_file_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            endpoint_file = Path(tmp) / "harness"
            with patch.object(routing, "ENDPOINTS_FILE", endpoint_file), patch.dict(os.environ, ENV, clear=True):
                self.assertEqual(routing.routing_environment(), ENV)
                endpoint_file.write_text('AZURE_OPENAI_ENDPOINT=https://different.openai.azure.com\n')
                self.assertEqual(routing.routing_environment(), ENV)

    def test_endpoint_file_does_not_execute_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            endpoint_file = Path(tmp) / "harness"
            marker = Path(tmp) / "should-not-exist"
            endpoint_file.write_text(f'AZURE_OPENAI_ENDPOINT=$(touch {marker})\n')
            with patch.object(routing, "ENDPOINTS_FILE", endpoint_file), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(routing.RoutingError):
                    routing.routing_environment()
            self.assertFalse(marker.exists())

    def test_fleet_defaults_route_the_owner_table(self):
        r = router()
        table = r.validate_all()
        # Astra only plans, Grok writes the code, the two DeepSeek lines carry every other role.
        self.assertEqual(table["planner"].spec.model, "gpt-6-astra")
        self.assertEqual(table["researcher"].spec.model, "grok-4-6")
        for role in ("surgeon", "critic", "verifier", "synthesizer", "refine_proposer"):
            self.assertEqual(table[role].spec.model, "deepseek-v4-pro", role)
        for role in ("budget_planner", "explorer", "steward", "probe", "frontier", "narrowness_critic", "adoption_critic"):
            self.assertEqual(table[role].spec.model, "deepseek-v4-flash", role)
        self.assertEqual({a.spec.model for a in table.values()},
                         {"gpt-6-astra", "grok-4-6", "deepseek-v4-pro", "deepseek-v4-flash"})
        self.assertEqual(table["critic"].spec.wire, "openai")
        self.assertEqual(table["planner"].spec.wire, "responses")
        self.assertEqual(table["planner"].spec.base_url, "https://res.openai.azure.com")
        self.assertEqual(table["critic"].spec.base_url, "https://res.services.ai.azure.com")
        self.assertEqual(table["critic"].spec.api_version, "2024-05-01-preview")
        # The researcher and its reviewers sit on different publishers, so independence is mechanical.
        self.assertNotEqual(table["researcher"].spec.model, table["critic"].spec.model)
        self.assertNotEqual(table["researcher"].spec.model, table["verifier"].spec.model)
        self.assertEqual(table["planner"].spec.price_in_usd, 2.0)
        self.assertEqual(table["explorer"].budget.max_budget_usd, 4.0)
        self.assertEqual(table["planner"].budget.max_budget_usd, 17.5)
        self.assertEqual(table["critic"].budget.max_budget_usd, 11.0)
        self.assertEqual(table["refine_proposer"].budget.max_budget_usd, 2.5)
        self.assertEqual(table["planner"].spec.context_window, 200000)
        self.assertEqual(table["researcher"].spec.context_window, 256000)
        self.assertEqual(table["planner"].summary()["context_window"], 200000)
        self.assertEqual(table["planner"].summary()["architecture_tier"], "role")

    def test_workspace_overrides_by_mode_then_role_then_default(self):
        r = router({"harness": {"roles": {"critic/triage": "deepseek-flash", "explorer": "astra", "researcher": "deepseek",
                                          "default": "grok"}}})
        self.assertEqual(r.resolve("critic", "triage").spec.model, "deepseek-v4-flash")
        # a workspace default outranks the fleet's per-role table
        self.assertEqual(r.resolve("critic", "attempt_review").spec.model, "grok-4-6")
        self.assertEqual(r.resolve("explorer", "ideation").spec.model, "gpt-6-astra")
        self.assertEqual(r.resolve("steward").source, "loop-config:default")
        self.assertEqual(router().resolve("steward").source, "fleet-default:steward")

    def test_inline_spec_and_local_model_table(self):
        r = router({"harness": {"models": {"local": {"backend": "http_chat", "provider": "local", "wire": "openai",
                                                       "model": "qwen", "base_url": "http://gpu-box:8000/v1",
                                                       "billing": "local"}},
                                "roles": {"explorer": "local", "synthesizer": {"backend": "http_chat", "model": "grok-4-6",
                                                                                "provider": "azure", "base_url": "https://f",
                                                                                "context_window": 256000}}}})
        self.assertEqual(r.resolve("explorer").spec.billing, "local")
        self.assertEqual(r.resolve("synthesizer").spec.price_out_usd, 6.0)

    def test_critic_and_verifier_must_differ_from_the_researcher(self):
        for role in ("critic", "verifier"):
            with self.assertRaises(routing.RoutingError):
                router({"harness": {"roles": {role: "grok"}}}).resolve(role)
        # moving the researcher onto the reviewers' model is caught the same way
        with self.assertRaises(routing.RoutingError):
            router({"harness": {"roles": {"researcher": "deepseek"}}}).resolve("critic")
        with self.assertRaises(routing.RoutingError):
            router({"harness": {"roles": {"researcher": "deepseek"}}}).resolve("verifier")

    def test_unknown_price_refuses_to_start(self):
        r = routing.Router(prices={}, env=ENV, token_exists=lambda p: True)
        with self.assertRaises(routing.RoutingError):
            r.resolve("planner")
        self.assertEqual(r.resolve("planner", check=False).spec.model, "gpt-6-astra")

    def test_subscription_needs_the_token_file(self):
        with self.assertRaises(routing.RoutingError):
            router({"harness": {"roles": {"planner": CLAUDE_MAX}}}, token_exists=lambda p: False).resolve("planner")
        spec = router({"harness": {"roles": {"planner": CLAUDE_MAX}}}).resolve("planner").spec
        self.assertEqual(spec.billing, "subscription")
        self.assertEqual(spec.backend, "claude_code")

    def test_azure_without_endpoint_is_refused(self):
        r = routing.Router(prices=PRICES, env={}, token_exists=lambda p: True)
        with self.assertRaises(routing.RoutingError):
            r.resolve("planner")

    def test_api_model_without_a_context_window_is_refused(self):
        blind = {"backend": "http_chat", "model": "gpt-6-astra", "provider": "azure", "base_url": "https://f"}
        r = router({"harness": {"roles": {"planner": blind}}})
        with self.assertRaisesRegex(routing.RoutingError, "refusing to size prompts blind"):
            r.resolve("planner")
        self.assertIsNone(r.resolve("planner", check=False).spec.context_window)
        sized = router({"harness": {"roles": {"planner": {**blind, "context_window": 128000}}}})
        self.assertEqual(sized.resolve("planner").spec.context_window, 128000)
        # local and subscription billing size nothing against a price, so no window is demanded
        local = {"backend": "http_chat", "model": "qwen", "provider": "local", "base_url": "http://h:8000/v1", "billing": "local"}
        self.assertIsNone(router({"harness": {"roles": {"explorer": local}}}).resolve("explorer").spec.context_window)
        self.assertIsNone(router({"harness": {"roles": {"planner": CLAUDE_MAX}}}).resolve("planner").spec.context_window)

    def test_architecture_tier_comes_from_the_alias_and_is_validated(self):
        r = router()
        self.assertEqual(r.resolve("explorer").spec.architecture_tier, "lean")
        self.assertEqual(r.resolve("synthesizer").spec.architecture_tier, "role")
        self.assertEqual(r.resolve("planner").spec.architecture_tier, "role")
        self.assertEqual(r.resolve("critic").spec.architecture_tier, "role")
        self.assertEqual(router({"harness": {"roles": {"planner": CLAUDE_MAX}}}).resolve("planner").spec.architecture_tier, "role")
        self.assertEqual(ModelSpec(backend="http_chat", model="m").architecture_tier, "role")
        with self.assertRaises(ValueError):
            ModelSpec(backend="http_chat", model="m", architecture_tier="huge")
        with self.assertRaises(ValueError):
            router({"harness": {"roles": {"planner": {"backend": "http_chat", "model": "gpt-6-astra", "provider": "azure",
                                                     "base_url": "https://f", "context_window": 1, "architecture_tier": "all"}}}}).resolve("planner")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = ModelSpec(backend="http_chat", model="gpt-6-astra", provider="azure", price_in_usd=2.0, price_out_usd=8.0)
        self.sub = ModelSpec(backend="claude_code", model="claude-opus-5", provider="anthropic_subscription",
                             wire="anthropic", billing="subscription")

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_spend_is_billed_and_subscription_spend_is_notional(self):
        ws = self.root / "ws"
        ws.mkdir()
        row = budget.record_spend(ws, self.api, {"prompt_tokens": 1_000_000, "completion_tokens": 100_000},
                                  role="researcher", mode="experiment", directive_id="d", session_id="s")
        self.assertAlmostEqual(row["estimated_usd"], 2.8)
        row = budget.record_spend(ws, self.sub, {"input_tokens": 10, "output_tokens": 10, "total_cost_usd": 0.5},
                                  role="critic", mode="attempt_review", directive_id="d", session_id="s2")
        self.assertEqual(row["estimated_usd"], 0.0)
        self.assertEqual(row["notional_usd"], 0.5)
        self.assertAlmostEqual(budget.ledger_total(ws / ".spend.jsonl"), 2.8)

    def test_guard_refuses_at_hard_stop_and_degrades_at_warning(self):
        for name, spend in (("a", 900.0), ("b", 650.0)):
            ws = self.root / name
            ws.mkdir()
            (ws / ".spend.jsonl").write_text(json.dumps({"estimated_usd": spend}) + "\n")
        guard = budget.ApiSpendGuard(self.root / "state" / "api-spend.json")
        state = guard.refresh(self.root)
        self.assertAlmostEqual(state["api_spend_usd"], 1550.0)
        self.assertTrue(state["degraded"])
        self.assertFalse(state["stopped"])
        self.assertTrue(guard.allows(self.api))
        self.assertEqual(guard.max_parallel(4), 1)
        self.assertFalse(guard.refine_allowed())
        (self.root / "c").mkdir()
        (self.root / "c" / ".spend.jsonl").write_text(json.dumps({"estimated_usd": 400.0}) + "\n")
        state = guard.refresh(self.root, extra_ledgers=[self.root / "state" / ".spend.jsonl"])
        self.assertTrue(state["stopped"])
        self.assertFalse(guard.allows(self.api))
        self.assertTrue(guard.allows(self.sub), "subscription sessions do not draw on the API budget")

    def test_aws_guard_has_its_own_stops(self):
        guard = budget.AwsSpendGuard(self.root / "aws.json")
        guard.update(1650.0)
        self.assertTrue(guard.allows_sessions())
        self.assertFalse(guard.allows_gpu())
        guard.update(1900.0)
        self.assertFalse(guard.allows_sessions())

    def test_subscription_env_never_carries_an_api_key(self):
        env = budget.subscription_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-x", "CLAUDE_CODE_USE_BEDROCK": "1",
                                       "CLAUDECODE": "1", "HOME": "/h"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h"})
        with self.assertRaises(budget.BudgetError):
            budget.assert_no_api_billing({"ANTHROPIC_AUTH_TOKEN": "t"})
        with self.assertRaises(budget.BudgetError):
            budget.assert_no_api_billing({"CLAUDE_CODE_USE_VERTEX": "1"})


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_after_prefers_the_reset_time_then_backs_off(self):
        reset = pause.iso(self.now + timedelta(minutes=10))
        self.assertEqual(pause.resume_after(reset, 0, now=self.now), self.now + timedelta(minutes=10, seconds=90))
        self.assertEqual(pause.resume_after(None, 0, now=self.now), self.now + timedelta(minutes=30))
        self.assertEqual(pause.resume_after(None, 1, now=self.now), self.now + timedelta(hours=1))
        self.assertEqual(pause.resume_after(None, 9, now=self.now), self.now + timedelta(hours=5))
        stale = pause.iso(self.now - timedelta(minutes=1))
        self.assertEqual(pause.resume_after(stale, 0, now=self.now), self.now + timedelta(minutes=30))

    def test_subscription_pause_is_account_wide_and_counts(self):
        first = pause.pause_subscription(self.root, None)
        second = pause.pause_subscription(self.root, None)
        self.assertEqual((first["count"], second["count"]), (1, 2))
        self.assertTrue(pause.subscription_paused(self.root))
        self.assertEqual(first["reason"], "subscription_rate_limit")

    def test_workspace_pause_expires_and_human_hold_does_not(self):
        ws = self.root / "ws"
        pause.pause_workspace(ws, "budget", pause.utc_now() - timedelta(seconds=1))
        self.assertFalse(pause.workspace_paused(ws))
        pause.human_hold(ws, "owner review")
        self.assertTrue(pause.workspace_paused(ws))
        pause.clear(ws / pause.PAUSE_FILE)
        self.assertFalse(pause.workspace_paused(ws))


if __name__ == "__main__":
    unittest.main()
