"""Role and mode to model resolution.

Resolution order: loop-config `harness.roles["<role>/<mode>"]`, then
`harness.roles["<role>"]`, then `harness.roles["default"]`, then the fleet
defaults in `harness/state/routing-defaults.json`. A value is either a model
alias from the `models` table or an inline ModelSpec object.

Two rules are mechanical: a critic or verifier never resolves to the model
that answers the researcher for the same workspace, and a role may bill to a
subscription only when the subscription token file exists.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional

from .backends.base import ModelSpec, SessionBudget

STATE_DIR = Path(__file__).resolve().parent / "state"
DEFAULTS_FILE = STATE_DIR / "routing-defaults.json"
PRICES_FILE = STATE_DIR / "prices.json"
ENDPOINTS_FILE = Path("/etc/adv-loop/env.d/harness")
ENDPOINT_VARIABLES = {"AZURE_OPENAI_ENDPOINT", "AZURE_AI_ENDPOINT"}
INDEPENDENT_OF_RESEARCHER = ("critic", "verifier")
SPEC_FIELDS = {f.name for f in fields(ModelSpec)}
BUDGET_FIELDS = {f.name for f in fields(SessionBudget)}


class RoutingError(ValueError):
    pass


def routing_environment() -> Dict[str, str]:
    """Load endpoint defaults without evaluating shell code or importing API keys.

    Desktop terminals need not run a login shell. Explicit nonempty environment
    values still win; an explicit Router(env=...) remains fully isolated.
    """
    env = dict(os.environ)
    if all(env.get(name) for name in ENDPOINT_VARIABLES):
        return env
    try:
        lines = ENDPOINTS_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return env
    except (OSError, UnicodeError) as exc:
        raise RoutingError(f"Cannot read endpoint defaults from {ENDPOINTS_FILE}") from exc
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        name, separator, value = stripped.partition("=")
        name = name.strip()
        if not separator or name not in ENDPOINT_VARIABLES or env.get(name):
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise RoutingError(f"Invalid endpoint assignment at {ENDPOINTS_FILE}:{number}") from None
        if len(parts) != 1 or not parts[0].startswith("https://") or any(c in parts[0] for c in "$` \t\r\n"):
            raise RoutingError(f"Expected a literal HTTPS endpoint at {ENDPOINTS_FILE}:{number}")
        env[name] = parts[0]
    return env


@dataclass(frozen=True)
class Assignment:
    role: str
    mode: Optional[str]
    alias: Optional[str]
    spec: ModelSpec
    budget: SessionBudget
    source: str

    def summary(self) -> Dict[str, Any]:
        """The fields an operator reads off `adv-harness route`."""

        return {
            "model": self.spec.model, "alias": self.alias, "backend": self.spec.backend,
            "billing": self.spec.billing, "source": self.source,
            "context_window": self.spec.context_window, "architecture_tier": self.spec.architecture_tier,
            "max_budget_usd": self.budget.max_budget_usd,
        }


def load_defaults(path: Path = DEFAULTS_FILE) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_prices(path: Path = PRICES_FILE) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k != "note"}


def _expand_env(value: Any, env: Dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return env.get(value[2:-1]) or None
    return value


def spec_from(data: Dict[str, Any], *, prices: Optional[Dict[str, Any]] = None,
              env: Optional[Dict[str, str]] = None) -> ModelSpec:
    env = dict(os.environ if env is None else env)
    body = {k: _expand_env(v, env) for k, v in data.items() if k in SPEC_FIELDS}
    price = (prices or {}).get(body.get("model"))
    if isinstance(price, dict):
        body.setdefault("price_in_usd", price.get("input"))
        body.setdefault("price_out_usd", price.get("output"))
    return ModelSpec(**body)


def budget_from(data: Optional[Dict[str, Any]]) -> SessionBudget:
    return SessionBudget(**{k: v for k, v in (data or {}).items() if k in BUDGET_FIELDS})


class Router:
    def __init__(self, config: Optional[Dict[str, Any]] = None, *, defaults: Optional[Dict[str, Any]] = None,
                 prices: Optional[Dict[str, Any]] = None, env: Optional[Dict[str, str]] = None,
                 token_exists=None) -> None:
        self.config = (config or {}).get("harness", {}) if config else {}
        self.defaults = defaults if defaults is not None else load_defaults()
        self.prices = prices if prices is not None else load_prices()
        self.env = routing_environment() if env is None else dict(env)
        self.token_exists = token_exists or (lambda p: Path(p).is_file())
        self.models: Dict[str, Dict[str, Any]] = {**self.defaults.get("models", {}), **self.config.get("models", {})}

    def _lookup(self, role: str, mode: Optional[str]):
        local = self.config.get("roles", {})
        fleet = self.defaults.get("roles", {})
        for table, source in ((local, "loop-config"), (fleet, "fleet-default")):
            for key in ([f"{role}/{mode}"] if mode else []) + [role, "default"]:
                if key in table:
                    return table[key], source, key
        raise RoutingError(f"no routing for {role}/{mode}")

    def _spec(self, value: Any) -> "tuple[Optional[str], ModelSpec]":
        if isinstance(value, str):
            if value not in self.models:
                raise RoutingError(f"unknown model alias {value!r}")
            return value, spec_from(self.models[value], prices=self.prices, env=self.env)
        if isinstance(value, dict):
            return value.get("alias"), spec_from(value, prices=self.prices, env=self.env)
        raise RoutingError(f"routing value must be an alias or an object, got {type(value).__name__}")

    def _budget(self, role: str, mode: Optional[str]) -> SessionBudget:
        table = {**self.defaults.get("budgets", {}), **self.config.get("budgets", {})}
        for key in ([f"{role}/{mode}"] if mode else []) + [role, "default"]:
            if key in table:
                return budget_from(table[key])
        return SessionBudget()

    def resolve(self, role: str, mode: Optional[str] = None, *, check: bool = True) -> Assignment:
        value, source, key = self._lookup(role, mode)
        alias, spec = self._spec(value)
        assignment = Assignment(role=role, mode=mode, alias=alias, spec=spec,
                                budget=self._budget(role, mode), source=f"{source}:{key}")
        if check:
            self.check(assignment)
        return assignment

    def check(self, assignment: Assignment) -> None:
        spec = assignment.spec
        if spec.billing == "api" and not spec.priced:
            raise RoutingError(f"{assignment.role}: model {spec.model!r} has no price in prices.json; refusing to bill blind")
        if spec.billing == "api" and spec.context_window is None:
            raise RoutingError(f"{assignment.role}: model {spec.model!r} has no context_window in its routing entry;"
                               " refusing to size prompts blind")
        if spec.billing == "subscription":
            if not spec.key_file or not self.token_exists(spec.key_file):
                raise RoutingError(f"{assignment.role}: subscription billing needs the token file {spec.key_file}")
        if spec.provider == "azure" and not spec.base_url:
            raise RoutingError(f"{assignment.role}: azure provider needs base_url; set AZURE_OPENAI_ENDPOINT / "
                               f"AZURE_AI_ENDPOINT in the environment or {ENDPOINTS_FILE}")
        if assignment.role in INDEPENDENT_OF_RESEARCHER:
            researcher = self.resolve("researcher", "experiment", check=False).spec
            if researcher.model == spec.model and researcher.provider == spec.provider:
                raise RoutingError(
                    f"{assignment.role} resolves to the researcher's model {spec.model!r}; independence must be mechanical")

    def validate_all(self) -> Dict[str, Assignment]:
        """Every role the fleet can issue; raises on the first violation."""

        table: Dict[str, Assignment] = {}
        for role in sorted(set(self.defaults.get("roles", {})) | set(self.config.get("roles", {}))):
            if role == "default" or "/" in role:
                continue
            table[role] = self.resolve(role)
        return table
