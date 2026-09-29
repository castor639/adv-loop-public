"""HTTP chat backend: one class, two wire formats, any OpenAI-compatible or
Anthropic Messages endpoint, including Azure AI Foundry deployments.

The harness runs the tools itself, so every call is native to the ledger.
The final text is checked against the compiled schema and one retry carries
the exact problems back. Spend is recorded from usage and the role's prices.
Provider keys are read from a file on the host at request time and never
placed in any container environment.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adv_loop.storage import atomic_write_text

from .. import assembler, jsonschema_lite, live, tools
from .base import AssembledPrompt, ModelSpec, SessionBudget, SessionResult, WorkspaceHandle

REQUEST_TIMEOUT_SECONDS = 600
ANTHROPIC_VERSION = "2023-06-01"
Transport = Callable[[str, Dict[str, str], Dict[str, Any]], Dict[str, Any]]


class TransportError(RuntimeError):
    def __init__(self, status: Optional[int], body: str, retry_after: Optional[float] = None) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


RETRY_AFTER_MAX_SECONDS = 120.0
RETRY_AFTER_ATTEMPTS = 3
WAIT_HINT = re.compile(r"wait (\d+) seconds", re.IGNORECASE)


def retry_after_seconds(status: Optional[int], body: str, headers=None) -> Optional[float]:
    """A short server-stated wait (per-minute token limits) that is worth sleeping through in-session."""

    if status != 429:
        return None
    value = None
    if headers is not None:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        try:
            value = float(raw) if raw else None
        except ValueError:
            value = None
    if value is None:
        match = WAIT_HINT.search(body or "")
        value = float(match.group(1)) if match else None
    return value


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def urllib_transport(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise TransportError(exc.code, body, retry_after_seconds(exc.code, body, exc.headers)) from exc
    except urllib.error.URLError as exc:
        raise TransportError(None, str(exc.reason)) from exc


def read_key(spec: ModelSpec) -> Optional[str]:
    if not spec.key_file:
        return None
    text = Path(spec.key_file).read_text(encoding="utf-8").strip()
    # env.d style files may hold KEY=value lines; take the first value
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip('"')
    return text


def endpoint(spec: ModelSpec) -> str:
    base = (spec.base_url or "").rstrip("/")
    if spec.path:
        path = spec.path.replace("{model}", spec.model)
    elif spec.wire == "anthropic":
        path = "/v1/messages"
    elif spec.wire == "responses":
        path = "/v1/responses"
    else:
        path = "/chat/completions"
    url = base + path
    if spec.api_version:
        url += ("&" if "?" in url else "?") + "api-version=" + spec.api_version
    return url


def headers_for(spec: ModelSpec, key: Optional[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if key:
        if spec.auth_header == "api-key":
            headers["api-key"] = key
            if spec.wire == "anthropic":
                headers["x-api-key"] = key
        elif spec.auth_header == "x-api-key":
            headers["x-api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    if spec.wire == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    return headers


def extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    if start > 0:
        text = text[start:]
    end = text.rfind("}")
    if end >= 0:
        text = text[:end + 1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("output must be a JSON object")
    return value


class HttpChatBackend:
    name = "http_chat"

    def __init__(self, *, transport: Transport = urllib_transport, tool_runner_factory=None,
                 key_reader: Callable[[ModelSpec], Optional[str]] = read_key, agent_id: str = "adv-harness",
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.transport = transport
        self.tool_runner_factory = tool_runner_factory
        self.key_reader = key_reader
        self.agent_id = agent_id
        self.sleep = sleep

    def _runner(self, handle: WorkspaceHandle) -> tools.ToolRunner:
        ledger_path = handle.session_dir / "ledger.jsonl"
        if self.tool_runner_factory:
            return self.tool_runner_factory(handle, ledger_path)
        return tools.ToolRunner(handle.path, ledger_path)

    def run(self, directive: Dict[str, Any], handle: WorkspaceHandle, spec: ModelSpec,
            budget: SessionBudget, prompt: AssembledPrompt) -> SessionResult:
        started = time.monotonic()
        result = SessionResult(session_id=handle.session_id, started_at=utc_now(), resolved_model=spec.model,
                               session_dir=handle.session_dir)
        handle.session_dir.mkdir(parents=True, exist_ok=True)
        runner = self._runner(handle)
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0, "requests": 0}
        try:
            key = self.key_reader(spec)
        except OSError as exc:
            return self._finish(result, runner, usage_total, spec, "process_error", [f"key file unreadable: {exc}"])
        url = endpoint(spec)
        headers = headers_for(spec, key)
        defs = getattr(runner, "definitions", tools.TOOL_DEFINITIONS)
        wire = {"openai": OpenAIWire, "anthropic": AnthropicWire, "responses": ResponsesWire}[spec.wire](spec, defs)
        messages = wire.initial(prompt)
        # Pre-flight size guard: a prompt the window cannot hold is refused before any request is billed.
        estimate = (len(prompt.system_text) + len(prompt.user_text) + len(json.dumps(defs))) // 4
        usage_total["estimated_prompt_tokens"] = estimate
        if spec.context_window and estimate + spec.max_tokens > 0.9 * spec.context_window:
            return self._finish(result, runner, usage_total, spec, "error_prompt_too_large", [
                f"prompt estimate {estimate} tokens plus max_tokens {spec.max_tokens} exceeds 90% of the"
                f" {spec.context_window}-token context window of {spec.model}; no request was sent"])
        retried = False
        waited = 0
        errors: List[str] = []
        ended = "success"
        output: Optional[Dict[str, Any]] = None

        for _turn in range(budget.max_tool_turns + 2):
            if time.monotonic() - started > budget.wall_clock_seconds:
                result.timed_out = True
                ended = "timeout"
                break
            if usage_total["requests"] >= (budget.max_turns or 10 ** 6):
                ended = "error_max_turns"
                break
            tag = f"[{handle.session_id[:8]}]"
            live.note(handle.path, f"{tag} request {usage_total['requests'] + 1} -> {spec.model} "
                                   f"({len(messages)} messages, effort={spec.effort or 'default'}) sent; waiting for reply")
            sent_at = time.monotonic()
            try:
                response = self.transport(url, headers, wire.request(messages, prompt.schema))
            except TransportError as exc:
                live.note(handle.path, f"{tag} HTTP {exc.status} after {time.monotonic() - sent_at:.0f}s: {exc.body[:160]}")
                if exc.status == 400 and hasattr(wire, "flip_token_key") and wire.flip_token_key(exc.body):
                    errors.append(f"token-limit key rejected; retrying with {wire.completion_tokens_key}")
                    continue
                wait = exc.retry_after if exc.retry_after is None else exc.retry_after + 2.0
                if exc.status == 429 and wait is not None and wait <= RETRY_AFTER_MAX_SECONDS and waited < RETRY_AFTER_ATTEMPTS:
                    waited += 1
                    errors.append(f"per-minute limit; waited {wait:.0f}s (attempt {waited})")
                    self.sleep(wait)
                    continue
                result.api_error_status = exc.status
                errors.append(str(exc))
                if exc.status == 429:
                    result.rate_limited = True
                    result.rate_limit = {"status": 429, "body": exc.body[:500]}
                    ended = "rate_limited"
                else:
                    ended = "process_error"
                break
            usage_total["requests"] += 1
            wire.add_usage(response, usage_total)
            cost = spec.cost_usd(usage_total["input_tokens"], usage_total["output_tokens"],
                                 usage_total["cache_read_input_tokens"], usage_total["cache_creation_input_tokens"])
            if cost is not None and cost > budget.max_budget_usd:
                errors.append(f"session budget {budget.max_budget_usd} exceeded at {cost}")
                ended = "error_max_budget_usd"
                break
            calls, text, truncated = wire.parse(response, messages)
            live.note(handle.path, f"{tag} reply {usage_total['requests']} after {time.monotonic() - sent_at:.0f}s: "
                                   f"in={usage_total['input_tokens']} out={usage_total['output_tokens']} "
                                   + (f"cost=${cost:.3f} " if cost is not None else "")
                                   + (f"tool calls: {', '.join(c['name'] for c in calls)}" if calls
                                      else f"final text {len(text or '')} chars" + (" (truncated)" if truncated else "")))
            if calls:
                for call in calls:
                    outcome = runner.run(call["name"], call["arguments"], tool_use_id=call["id"])
                    wire.tool_result(messages, call, outcome)
                continue
            if truncated:
                errors.append("model output was truncated at max_tokens")
                ended = "error_during_execution"
                break
            try:
                candidate = extract_json(text or "")
                problems = jsonschema_lite.validate(candidate, prompt.schema)
            except ValueError as exc:
                candidate, problems = None, [f"output is not a JSON object: {exc}"]
            if not problems:
                output = candidate
                break
            if retried:
                output = candidate
                errors.append("schema problems after retry: " + "; ".join(problems[:8]))
                ended = "error_max_structured_output_retries"
                break
            retried = True
            wire.retry(messages, text or "", assembler.render_retry(problems[:12]))
        else:
            ended = "error_max_turns"
            errors.append("tool-turn limit reached without a final object")

        result.model_output = output
        result.messages = messages
        return self._finish(result, runner, usage_total, spec, ended, errors)

    def _finish(self, result: SessionResult, runner: tools.ToolRunner, usage: Dict[str, Any], spec: ModelSpec,
                ended: str, errors: List[str]) -> SessionResult:
        result.tool_ledger = list(runner.entries)
        result.usage = dict(usage)
        result.cost_usd = spec.cost_usd(usage["input_tokens"], usage["output_tokens"],
                                        usage["cache_read_input_tokens"], usage["cache_creation_input_tokens"])
        result.errors = errors
        result.is_error = ended != "success"
        result.ended = ended
        result.finished_at = utc_now()
        if result.messages is not None:
            atomic_write_text(result.session_dir / "messages.json", json.dumps(result.messages, indent=1, sort_keys=True))
        return result


class OpenAIWire:
    def __init__(self, spec: ModelSpec, definitions: Optional[List[Dict[str, Any]]] = None) -> None:
        self.spec = spec
        self.definitions = tools.TOOL_DEFINITIONS if definitions is None else definitions
        # newer OpenAI models reject max_tokens; other OpenAI-compatible servers reject max_completion_tokens
        self.completion_tokens_key = "max_completion_tokens" if spec.model.lower().startswith(("gpt-", "o1", "o3", "o4")) else "max_tokens"

    def initial(self, prompt: AssembledPrompt) -> List[Dict[str, Any]]:
        return [{"role": "system", "content": prompt.system_text}, {"role": "user", "content": prompt.user_text}]

    def flip_token_key(self, error_body: str) -> bool:
        """Swap the token-limit key once when the server names it as unsupported."""

        other = "max_tokens" if self.completion_tokens_key == "max_completion_tokens" else "max_completion_tokens"
        if self.completion_tokens_key in error_body and "unsupported" in error_body.lower():
            self.completion_tokens_key = other
            return True
        return False

    def request(self, messages: List[Dict[str, Any]], schema: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": self.spec.model, self.completion_tokens_key: self.spec.max_tokens,
                                "messages": messages, "tools": tools.openai_tools(self.definitions)}
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature
        if self.spec.effort:
            body["reasoning_effort"] = self.spec.effort
        return body

    @staticmethod
    def add_usage(response: Dict[str, Any], total: Dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        # some servers (xAI on Foundry) report reasoning tokens beside completion_tokens; they are billed as output
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        declared_total = int(usage.get("total_tokens") or 0)
        if reasoning and declared_total and prompt + completion + reasoning <= declared_total:
            completion += reasoning
        total["input_tokens"] += prompt
        total["output_tokens"] += completion
        details = usage.get("prompt_tokens_details") or {}
        total["cache_read_input_tokens"] += int(details.get("cached_tokens") or 0)

    @staticmethod
    def parse(response: Dict[str, Any], messages: List[Dict[str, Any]]):
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
            except (json.JSONDecodeError, ValueError) as exc:
                arguments = {"__error__": f"unparseable tool arguments: {exc}"}
            calls.append({"id": call.get("id"), "name": function.get("name", ""), "arguments": arguments})
        if calls:
            messages.append(message)
            return calls, None, False
        text = message.get("content") or message.get("reasoning_content") or ""
        messages.append({"role": "assistant", "content": text})
        return [], text, choice.get("finish_reason") == "length"

    @staticmethod
    def tool_result(messages: List[Dict[str, Any]], call: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(outcome, sort_keys=True)})

    @staticmethod
    def retry(messages: List[Dict[str, Any]], _text: str, feedback: str) -> None:
        messages.append({"role": "user", "content": feedback})


class AnthropicWire:
    def __init__(self, spec: ModelSpec, definitions: Optional[List[Dict[str, Any]]] = None) -> None:
        self.spec = spec
        self.definitions = tools.TOOL_DEFINITIONS if definitions is None else definitions
        self.system = ""

    def initial(self, prompt: AssembledPrompt) -> List[Dict[str, Any]]:
        self.system = prompt.system_text
        return [{"role": "user", "content": prompt.user_text}]

    def request(self, messages: List[Dict[str, Any]], schema: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.spec.model, "max_tokens": self.spec.max_tokens,
            "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages, "tools": tools.anthropic_tools(self.definitions),
        }
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature
        if self.spec.effort:
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": self.spec.effort}
        return body

    @staticmethod
    def add_usage(response: Dict[str, Any], total: Dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        total["input_tokens"] += int(usage.get("input_tokens") or 0)
        total["output_tokens"] += int(usage.get("output_tokens") or 0)
        total["cache_read_input_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
        total["cache_creation_input_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)

    @staticmethod
    def parse(response: Dict[str, Any], messages: List[Dict[str, Any]]):
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})
        calls = [{"id": b.get("id"), "name": b.get("name", ""), "arguments": b.get("input") or {}}
                 for b in content if b.get("type") == "tool_use"]
        if calls:
            return calls, None, False
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        return [], text, response.get("stop_reason") == "max_tokens"

    @staticmethod
    def tool_result(messages: List[Dict[str, Any]], call: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        block = {"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(outcome, sort_keys=True)}
        if messages and messages[-1].get("role") == "user" and isinstance(messages[-1].get("content"), list):
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": "user", "content": [block]})

    @staticmethod
    def retry(messages: List[Dict[str, Any]], _text: str, feedback: str) -> None:
        messages.append({"role": "user", "content": feedback})


class ResponsesWire:
    """OpenAI Responses API: reasoning models with function tools on one route.

    Stateless: every request carries the full input list (prior output items
    included) with `store: false`, so nothing is retained server-side.
    """

    def __init__(self, spec: ModelSpec, definitions: Optional[List[Dict[str, Any]]] = None) -> None:
        self.spec = spec
        self.definitions = tools.TOOL_DEFINITIONS if definitions is None else definitions
        self.instructions = ""

    def initial(self, prompt: AssembledPrompt) -> List[Dict[str, Any]]:
        self.instructions = prompt.system_text
        return [{"role": "user", "content": prompt.user_text}]

    def request(self, messages: List[Dict[str, Any]], schema: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.spec.model, "store": False, "instructions": self.instructions, "input": messages,
            "max_output_tokens": self.spec.max_tokens,
            "tools": [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["input_schema"]} for t in self.definitions],
        }
        if self.spec.effort:
            body["reasoning"] = {"effort": self.spec.effort}
            # stateless reasoning across tool turns needs the encrypted reasoning items replayed
            body["include"] = ["reasoning.encrypted_content"]
        if self.spec.temperature is not None:
            body["temperature"] = self.spec.temperature
        return body

    @staticmethod
    def add_usage(response: Dict[str, Any], total: Dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        total["input_tokens"] += int(usage.get("input_tokens") or 0)
        total["output_tokens"] += int(usage.get("output_tokens") or 0)
        total["cache_read_input_tokens"] += int(details.get("cached_tokens") or 0)
        total["cache_creation_input_tokens"] += int(details.get("cache_write_tokens") or 0)

    @staticmethod
    def parse(response: Dict[str, Any], messages: List[Dict[str, Any]]):
        items = response.get("output") or []
        calls = []
        texts = []
        for item in items:
            kind = item.get("type")
            if kind == "function_call":
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    arguments = {"__error__": f"unparseable tool arguments: {exc}"}
                calls.append({"id": item.get("call_id"), "name": item.get("name", ""), "arguments": arguments})
                messages.append({"type": "function_call", "call_id": item.get("call_id"), "name": item.get("name"),
                                 "arguments": item.get("arguments") or "{}"})
            elif kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        texts.append(part.get("text", ""))
                messages.append({"role": "assistant", "content": "\n".join(texts)})
            elif kind == "reasoning":
                # replayed verbatim so the next turn keeps its chain of thought (store is false)
                messages.append({k: v for k, v in item.items() if k in ("type", "id", "encrypted_content", "summary")})
        if calls:
            return calls, None, False
        truncated = (response.get("incomplete_details") or {}).get("reason") == "max_output_tokens" \
            or response.get("status") == "incomplete"
        return [], "\n".join(texts), truncated

    @staticmethod
    def tool_result(messages: List[Dict[str, Any]], call: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        messages.append({"type": "function_call_output", "call_id": call["id"], "output": json.dumps(outcome, sort_keys=True)})

    @staticmethod
    def retry(messages: List[Dict[str, Any]], _text: str, feedback: str) -> None:
        messages.append({"role": "user", "content": feedback})
