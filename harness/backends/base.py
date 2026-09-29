"""The backend contract.

A backend runs exactly one model session for one directive and returns what
happened: the session identity, every tool call the harness observed, the
model's structured output, usage, and how the session ended. It never touches
the event chain. The supervisor turns a ``SessionResult`` into an attempt
submission (``harness.assembler``) and records it through ``LoopEngine``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

BILLING_KINDS = ("subscription", "api", "local")
WIRE_FORMATS = ("openai", "anthropic", "responses")
PROVIDERS = ("azure", "openai_compat", "anthropic_subscription", "local")
# How much of the system reference a model receives: the core chapters, the core plus its role
# chapter and evidence, or every chapter its role and mode call for.
ARCHITECTURE_TIERS = ("core", "lean", "role")


@dataclass(frozen=True)
class ModelSpec:
    """Which model answers a role, how it is reached, and how it is billed."""

    backend: str
    model: str
    provider: str = "openai_compat"
    wire: str = "openai"
    billing: str = "api"
    effort: Optional[str] = None
    base_url: Optional[str] = None
    path: Optional[str] = None
    api_version: Optional[str] = None
    key_file: Optional[str] = None
    auth_header: str = "bearer"
    price_in_usd: Optional[float] = None
    price_out_usd: Optional[float] = None
    max_tokens: int = 16000
    temperature: Optional[float] = None
    context_window: Optional[int] = None
    architecture_tier: str = "role"

    def __post_init__(self) -> None:
        if self.billing not in BILLING_KINDS:
            raise ValueError(f"billing must be one of {BILLING_KINDS}")
        if self.wire not in WIRE_FORMATS:
            raise ValueError(f"wire must be one of {WIRE_FORMATS}")
        if self.provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {PROVIDERS}")
        if not self.backend or not self.model:
            raise ValueError("backend and model are required")
        if self.architecture_tier not in ARCHITECTURE_TIERS:
            raise ValueError(f"architecture_tier must be one of {ARCHITECTURE_TIERS}")
        if self.context_window is not None and (isinstance(self.context_window, bool) or self.context_window <= 0):
            raise ValueError("context_window must be a positive token count")

    @property
    def priced(self) -> bool:
        return self.price_in_usd is not None and self.price_out_usd is not None

    def cost_usd(self, input_tokens: int, output_tokens: int,
                 cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> Optional[float]:
        if not self.priced:
            return None
        assert self.price_in_usd is not None and self.price_out_usd is not None
        total = (
            input_tokens * self.price_in_usd
            + cache_write_tokens * self.price_in_usd * 1.25
            + cache_read_tokens * self.price_in_usd * 0.1
            + output_tokens * self.price_out_usd
        )
        return round(total / 1_000_000, 6)


@dataclass(frozen=True)
class SessionBudget:
    max_budget_usd: float = 15.0
    wall_clock_seconds: float = 3000.0
    max_turns: Optional[int] = 80
    max_tool_turns: int = 12


@dataclass(frozen=True)
class WorkspaceHandle:
    ws_id: str
    path: Path
    session_id: str
    container: Optional[str] = None

    @property
    def session_dir(self) -> Path:
        return self.path / ".harness" / "sessions" / self.session_id


@dataclass(frozen=True)
class AssembledPrompt:
    system_text: str
    user_text: str
    schema: Dict[str, Any]
    manifest: Dict[str, Any]


SESSION_ENDINGS = (
    "success",
    "error_max_turns",
    "error_max_budget_usd",
    "error_max_structured_output_retries",
    "error_during_execution",
    "rate_limited",
    "timeout",
    "process_error",
    "no_output",
    "error_prompt_too_large",
)


@dataclass
class SessionResult:
    session_id: str
    ended: str = "success"
    tool_ledger: List[Dict[str, Any]] = field(default_factory=list)
    model_output: Optional[Dict[str, Any]] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    rate_limited: bool = False
    rate_limit: Optional[Dict[str, Any]] = None
    is_error: bool = False
    api_error_status: Optional[int] = None
    errors: List[str] = field(default_factory=list)
    stderr_tail: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    started_at: str = ""
    finished_at: str = ""
    resolved_model: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    session_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.ended not in SESSION_ENDINGS:
            raise ValueError(f"ended must be one of {SESSION_ENDINGS}")

    @property
    def ok(self) -> bool:
        return self.ended == "success" and self.model_output is not None and not self.rate_limited


class SessionBackend(Protocol):
    name: str

    def run(
        self,
        directive: Dict[str, Any],
        handle: WorkspaceHandle,
        model_spec: ModelSpec,
        budget: SessionBudget,
        prompt: AssembledPrompt,
    ) -> SessionResult: ...
