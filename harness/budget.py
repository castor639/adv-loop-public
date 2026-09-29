"""Spend ledgers and the two hard stops.

The API budget is one number across every Azure model: the cumulative
`estimated_usd` over every workspace's `.spend.jsonl` plus the loops' own
ledger. `ApiSpendGuard` refuses API-billed sessions at the hard stop and
degrades the fleet at the warning line. Subscription sessions record
`estimated_usd: 0.0` so the kernel's billed-dollars ceilings keep their
meaning, with the notional value beside it. AWS spend has its own guard,
fed by Cost Explorer; neither budget borrows from the other.

GPU hours are AWS spend. They land in two ledgers of their own: the fleet
file `<state>/gpu-spend.jsonl` (one `box_run` row per start/stop cycle,
the number Cost Explorer will show a day later) and the workspace file
`.gpu-spend.jsonl` (one `job` row per job, what the charter's hour cap
counts). `fleet_api_spend` never reads either.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .backends.base import ModelSpec
from .gpu import GPU_SPEND_FILE, STATE_SPEND_FILE  # noqa: F401  re-exported

API_HARD_STOP_USD = 1900.0
API_WARN_USD = 1500.0
AWS_HARD_STOP_USD = 1850.0
AWS_GPU_STOP_USD = 1600.0
GPU_RATE_USD_PER_HOUR = 1.006
SPEND_FILE = ".spend.jsonl"
FORBIDDEN_FOR_SUBSCRIPTION = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


class BudgetError(RuntimeError):
    pass


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_utc(text: Any) -> Optional[float]:
    """Epoch seconds for an ISO timestamp (`Z` or offset form, optional fraction); None when unparseable."""

    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def record_spend(workspace: Path, spec: ModelSpec, usage: Dict[str, Any], *, role: str, mode: str,
                 directive_id: Optional[str], session_id: str, base_prompt_hash: Optional[str] = None,
                 notional_spec: Optional[ModelSpec] = None) -> Dict[str, Any]:
    """Append one row; returns it. Billed dollars only for API billing."""

    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    real = spec.cost_usd(input_tokens, output_tokens, cache_read, cache_write)
    if usage.get("total_cost_usd") is not None and real is None:
        real = float(usage["total_cost_usd"])
    if spec.billing == "api":
        estimated = real if real is not None else None
        notional = real
    else:
        estimated = 0.0
        pricer = notional_spec or spec
        notional = pricer.cost_usd(input_tokens, output_tokens, cache_read, cache_write)
        if notional is None and usage.get("total_cost_usd") is not None:
            notional = float(usage["total_cost_usd"])
    row = {
        "at": utc_now(), "provider": spec.provider, "model": spec.model, "billing": spec.billing,
        "estimated_usd": estimated, "notional_usd": notional,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
        "role": role, "mode": mode, "directive_id": directive_id, "session_id": session_id,
        "base_prompt_hash": base_prompt_hash,
    }
    with open(workspace / SPEND_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def ledger_total(path: Path, field: str = "estimated_usd") -> float:
    total = 0.0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line).get(field)
            except (ValueError, AttributeError):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value)
    except (OSError, UnicodeDecodeError):
        return 0.0
    return round(total, 6)


def record_gpu_spend(path: Path, row: Dict[str, Any]) -> Dict[str, Any]:
    """Append one GPU ledger row (fleet `box_run` or workspace `job`); returns it with `at` filled."""

    row = dict(row)
    row.setdefault("at", utc_now())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def gpu_spend_rows(path: Path, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (kind is None or row.get("kind") == kind):
            rows.append(row)
    return rows


def gpu_spend_total(state_dir: Path, *, running_since: Optional[str] = None, rate: float = GPU_RATE_USD_PER_HOUR,
                    now: Optional[float] = None) -> float:
    """Dollars of box time: recorded `box_run` rows plus the live estimate for a box still running."""

    total = 0.0
    for row in gpu_spend_rows(Path(state_dir) / STATE_SPEND_FILE, "box_run"):
        value = row.get("usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
    started = parse_utc(running_since) if running_since else None
    if started is not None:
        elapsed = max(0.0, (now if now is not None else time.time()) - started)
        total += elapsed / 3600.0 * rate
    return round(total, 6)


def fleet_api_spend(root: Path, extra_ledgers: Iterable[Path] = ()) -> float:
    total = 0.0
    if root.is_dir():
        for ws in sorted(root.iterdir()):
            if ws.is_dir():
                total += ledger_total(ws / SPEND_FILE)
    for path in extra_ledgers:
        total += ledger_total(path)
    return round(total, 6)


@dataclass
class ApiSpendGuard:
    state_file: Path
    hard_stop_usd: float = API_HARD_STOP_USD
    warn_usd: float = API_WARN_USD

    def refresh(self, root: Path, extra_ledgers: Iterable[Path] = ()) -> Dict[str, Any]:
        total = fleet_api_spend(root, extra_ledgers)
        state = {"api_spend_usd": total, "hard_stop_usd": self.hard_stop_usd, "warn_usd": self.warn_usd,
                 "degraded": total >= self.warn_usd, "stopped": total >= self.hard_stop_usd, "at": utc_now()}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return state

    def state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"api_spend_usd": 0.0, "degraded": False, "stopped": False}

    def allows(self, spec: ModelSpec, projected_usd: float = 0.0) -> bool:
        if spec.billing != "api":
            return True
        state = self.state()
        return float(state.get("api_spend_usd", 0.0)) + projected_usd < self.hard_stop_usd

    def max_parallel(self, configured: int) -> int:
        return 1 if self.state().get("degraded") else configured

    def refine_allowed(self) -> bool:
        state = self.state()
        return not (state.get("degraded") or state.get("stopped"))


@dataclass
class AwsSpendGuard:
    state_file: Path
    hard_stop_usd: float = AWS_HARD_STOP_USD
    gpu_stop_usd: float = AWS_GPU_STOP_USD

    def update(self, cumulative_usd: float, by_tag: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        state = {"aws_spend_usd": float(cumulative_usd), "hard_stop_usd": self.hard_stop_usd,
                 "gpu_stop_usd": self.gpu_stop_usd, "stopped": cumulative_usd >= self.hard_stop_usd,
                 "gpu_disabled": cumulative_usd >= self.gpu_stop_usd, "at": utc_now()}
        if by_tag is not None:
            state["by_tag"] = {str(k): round(float(v), 2) for k, v in by_tag.items()}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return state

    def state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"aws_spend_usd": 0.0, "stopped": False, "gpu_disabled": False}

    def allows_sessions(self) -> bool:
        return not self.state().get("stopped")

    def allows_gpu(self, projected_usd: float = 0.0, unreported_usd: float = 0.0) -> bool:
        """Whether a box start fits under the GPU line, counting what Cost Explorer has not seen yet."""

        state = self.state()
        if state.get("stopped"):
            return False
        return float(state.get("aws_spend_usd", 0.0)) + float(unreported_usd) + float(projected_usd) < self.gpu_stop_usd

    def stale(self, max_age_s: float) -> bool:
        """True when the state has never been refreshed or its `at` is older than `max_age_s`."""

        at = parse_utc(self.state().get("at"))
        if at is None:
            return True
        return time.time() - at > max_age_s


def assert_no_api_billing(env: Dict[str, str]) -> None:
    """A subscription session must not be able to bill an API key by any route."""

    present: List[str] = [k for k in FORBIDDEN_FOR_SUBSCRIPTION if env.get(k)]
    present += [k for k in env if k.startswith("CLAUDE_CODE_USE_")]
    if present:
        raise BudgetError(f"subscription role would see API billing variables: {sorted(present)}")


def subscription_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if k not in FORBIDDEN_FOR_SUBSCRIPTION and not k.startswith("CLAUDE_CODE_USE_") and k != "CLAUDECODE"}
    assert_no_api_billing(env)
    return env
