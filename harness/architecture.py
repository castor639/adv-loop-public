"""The system reference: generated blocks, drift check, export, drafts.

`harness/prompts/ARCHITECTURE.md` describes the system to every model. The
parts that restate code (tool arguments, schema fields, kernel constants,
limits, states) are generated from the code so the prose cannot drift from
what runs: `check` reports drift, `write` regenerates in place. `export`
copies the whole reference into a workspace as one read-only file per
chapter so a session can read chapters it was not injected with, and
`ensure` keeps that copy intact across restarts and prompt updates.
`assemble_drafts` splices chapter drafts written elsewhere into the file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adv_loop import policy

from . import assembler, budget, container, improvement, routing, schema, tools
from . import gpu
from .backends.base import SESSION_ENDINGS, SessionBudget

ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE_PATH = assembler.PROMPT_DIR / assembler.ARCHITECTURE_FILE
EXPORT_DIR = Path(".harness") / "architecture"
MANIFEST_NAME = "manifest.json"
EXPORT_MODE = 0o444
GENERATED_OPEN = re.compile(r"^<!-- generated: ([a-z][a-z0-9_.]*) -->$")
GENERATED_CLOSE = re.compile(r"^<!-- /generated: ([a-z][a-z0-9_.]*) -->$")
DIRECTIVE_SOURCES = (ROOT / "src" / "adv_loop" / "policy.py", ROOT / "src" / "adv_loop" / "engine.py")
THINKING_CONSTANTS = (
    "STRATEGY_DIMENSIONS", "ESCALATION_LADDER", "LADDER_CYCLE", "LADDER_SPAN", "RESEARCH_MOVES", "REVIEW_LENSES",
    "BARRIER_PROBE_PATTERNS", "FORMALIZATION_RANKS", "CHECKED_RANK_FLOOR", "BASIN_CLOSURE_FAILURES",
    "MULTI_CAUSE_STREAK", "SEED_MIN", "SEED_MAX", "TRIAGE_PROMOTION_CAP", "SURGEON_FAULT_THRESHOLD",
)
IMPROVEMENT_LISTS = (
    ("improvement.ANTI_PATTERNS", improvement, "ANTI_PATTERNS"),
    ("improvement.KILL_TEST_KINDS", improvement, "KILL_TEST_KINDS"),
    ("improvement.KILL_TEST_OUTCOMES", improvement, "KILL_TEST_OUTCOMES"),
    ("improvement.TARGET_KINDS", improvement, "TARGET_KINDS"),
    ("improvement.EDIT_ACTIONS", improvement, "EDIT_ACTIONS"),
    ("improvement.EDIT_KINDS", improvement, "EDIT_KINDS"),
    ("improvement.HORIZON_EVENT_KINDS", improvement, "HORIZON_EVENT_KINDS"),
    ("schema.OVERLAY_OPS", schema, "OVERLAY_OPS"),
)

# Directive keys as the policy issues them: key -> (who receives it, what it carries). Keys that
# are not string literals in the policy or the engine are dropped at render time.
DIRECTIVE_FIELDS: "OrderedDict[str, Tuple[str, str]]" = OrderedDict([
    ("action", ("every directive",
                "`attempt` opens a model session; `stop`, `finalize`, and `await_human` end or pause the loop without one")),
    ("directive_id", ("every attempt directive",
                      "hash of policy version, event head, revision, role, mode, and target; stale once another event lands")),
    ("role", ("every attempt directive", "the role this session plays; the harness copies it into the submission")),
    ("mode", ("every attempt directive", "the procedure the session runs under; one mode file each")),
    ("policy_version", ("every attempt directive", "the workspace's pinned policy; gates and fields initialize by it")),
    ("task", ("every attempt directive", "the task statement recorded in `task_created`")),
    ("open_criteria", ("every attempt directive", "criteria not yet satisfied, with text and rank where one is set")),
    ("instructions", ("every attempt directive",
                      "the mode's instruction lines plus adopted overlay lines; the only authoritative text in the directive")),
    ("encouragement", ("every attempt directive", "a fixed line chosen by mode and streak; it changes no rule")),
    ("failure_streak", ("planner, researcher, critic, verifier, synthesizer",
                        "consecutive attempts without validated progress")),
    ("active_plan_id", ("planner, researcher, critic, verifier, synthesizer", "id of the plan attempt in force")),
    ("active_plan", ("planner, researcher, critic, verifier, synthesizer", "the plan object of that attempt")),
    ("recent_attempts", ("planner, researcher, critic, verifier, synthesizer", "summaries of the last five attempts")),
    ("recent_failed_strategies", ("researcher", "the last three researcher strategies that did not validate")),
    ("forbidden_strategy_fingerprints", ("researcher", "every researcher fingerprint on record; a repeat is rejected")),
    ("required_strategy_dimension_changes", ("researcher",
                                             "how many of the six dimensions must differ from the recent failed strategies")),
    ("criterion_failure_streaks", ("attempt directives on policy 3.0 and later", "failure streak per criterion id")),
    ("escalation", ("any role when a ladder rung is due", "the rung's `mark`, `criterion_id`, and `cycle`")),
    ("required_move", ("researcher when a move rung is due", "the move this attempt must declare")),
    ("open_candidates", ("researcher, planner, critic/triage", "top open candidates from triage, by score")),
    ("review_context", ("critic/attempt_review", "the target attempt, the full criteria list, and the event head")),
    ("lessons", ("researcher, planner", "lessons the kernel recorded; a plan answers them in `lessons_addressed`")),
    ("assets", ("researcher", "registered reusable assets with locators")),
    ("barriers", ("researcher, planner", "named walls with statements; a barrier probe cites one or adds one")),
    ("wishes", ("researcher, planner", "open wishes with their recheck times")),
    ("basins", ("researcher, planner, explorer, critic/triage", "basin registry: name, status, failure count")),
    ("target_attempt_id", ("critic, verifier, synthesizer", "the attempt under review, verification, or triage")),
    ("lens", ("critic/attempt_review",
              "the review lens for this pass: `correctness`, `novelty`, `proves_too_much`, or `simplification`")),
    ("criteria_to_verify", ("verifier", "every criterion with its primary evidence records")),
    ("independence_rule", ("verifier", "the context id and independence key must differ from the primary evidence")),
])

FIELD_MEANINGS: Dict[str, str] = {
    "strategy": "six dimensions naming how you attack the problem; its fingerprint can never repeat in the workspace",
    "hypothesis": "what you expect and why, stated before running anything; a representation shift is recorded here",
    "action": "what you did, as a narrative the ledger is checked against",
    "observation_notes": "your reading of the recorded tool results; the harness writes the raw `observation` from the ledger",
    "interpretation": "what the observation means, kept apart from what was seen",
    "uncertainties": "what you could not settle and what would settle it",
    "next_step": "the next experiment and its strategy distance from this one",
    "outcome": "`progress`, `no_progress`, `failed`, or `inconclusive`; `progress` needs an evidence-bearing change in this attempt",
    "evidence": "entries naming `artifact_path` or `verdict_id`; the harness hashes files and copies signed verdicts",
    "criterion_updates": "status claims per criterion with `evidence_refs` into this submission; refused from planner, verifier, and synthesizer",
    "contradictions": "observations that conflict with a recorded claim, with severity",
    "contradiction_resolutions": "resolutions of recorded contradictions, each with its evidence",
    "decisions": "choices made in this session with rationale and rejected alternatives",
    "plan": "assumptions, subproblems, candidate experiments, falsification tests, rejected assumptions, and `lessons_addressed`",
    "proposed_criteria": "new criteria the planner proposes; accepted only in `decompose` and `fresh_replan`",
    "plan_id": "the plan attempt this experiment executes; must equal `active_plan_id`",
    "criterion_targets": "the criteria this experiment attacks",
    "basin": "the approach family this attempt works in; a closed basin needs `representation_shift`",
    "representation_shift": "how the problem is re-represented; required to re-enter a closed basin, enters the strategy signature",
    "move": "`test`, `survey`, `barrier_probe`, `combine`, or `replicate`; a demanded move is checked",
    "candidate_id": "an open candidate consumed by this attempt so its fate is recorded",
    "assets_registered": "reusable assets with locators; a survey move registers at least one",
    "combination": "`asset_ids` of exactly two registered assets never combined before; required by a combine move",
    "barrier_probe": "`pattern`, `approach`, and exactly one of `barrier_id` or `new_barrier`; required by a barrier_probe move",
    "replication_of": "the earlier researcher attempt a replicate move reproduces",
    "wishes_declared": "wishes with `statement`, `would_open`, `test`, and `recheck_after`; the fleet rechecks them",
    "assessment": "the review verdict, reasons, uncertainty, candidate causes, control check, and `harness_gap`",
    "contradiction_search": "assumptions checked, disconfirming queries run, and the conclusion",
    "blocker_audit": "the dependency, its evidence, at least three safe alternatives tried, and the human action needed",
    "lens": "the review lens this pass applied; matches the directive's `lens`",
    "verification_results": "one entry per criterion with verdict, method, observation, and evidence refs",
    "report": "the structured final report: summary, per-criterion results, facts, inferences, uncertainties, limitations, next actions",
    "context_scope": "the literal `minimal`, attesting that the seeds came from a history-free context",
    "ideation_kind": "`broad` or `evolve`, as the directive set it",
    "seeds": "between `SEED_MIN` and `SEED_MAX` seed objects with claim, basin, first unjustified step, and kill test",
    "diagnosis": "raw detail, classification, kill test, next experiment, and the proposed overlay delta",
    "triage": "the target attempt, one verdict per seed, and pairwise comparisons against open candidates",
    "overlay_review": "`adopt`, `reject`, or `narrow` with reasons, uncertainty, and a narrowed delta",
}

ENDING_MEANINGS: Dict[str, str] = {
    "success": "the session returned one JSON object that passed the schema",
    "error_max_turns": "the request count reached the budget's `max_turns`",
    "error_max_budget_usd": "the running cost passed the session's `max_budget_usd`",
    "error_max_structured_output_retries": "the model kept returning output that failed the schema",
    "error_during_execution": "the backend hit an execution error, including output truncated at `max_tokens`",
    "rate_limited": "the provider returned 429 past the short retry window; the workspace pauses",
    "timeout": "the session's wall-clock seconds ran out",
    "process_error": "the backend process or the transport failed",
    "no_output": "the session ended without a final object",
    "error_prompt_too_large": "the pre-flight estimate of prompt plus `max_tokens` exceeded 90% of the model's"
                              " `context_window`; zero requests were sent",
}

# Tool arguments carry no descriptions in their JSON schema; these lines are the model-facing ones.
ARGUMENT_NOTES: Dict[str, Dict[str, str]] = {
    "run_in_sandbox": {
        "command": "argv list; the first element is the executable, no shell unless you name one",
        "cwd": "workspace-relative working directory; defaults to the workspace root",
        "timeout_seconds": "seconds before the harness kills the command; defaults to `DEFAULT_TOOL_TIMEOUT`",
    },
    "write_file": {
        "path": "workspace-relative path; parent directories are created",
        "content": "UTF-8 text written verbatim",
    },
    "read_file": {"path": "workspace-relative path; the first `READ_LIMIT` bytes come back with a `truncated` flag"},
    "hash_artifact": {"path": "workspace-relative path of the file to hash"},
    "validate": {
        "hook": "a checker hook registered in the workspace's validators",
        "input": "hook-specific input object handed to the checker",
    },
    "gpu_run": {
        "command": "argv list run inside the GPU image",
        "cwd": "workspace-relative working directory; defaults to `payload/scratch`",
        "inputs": "workspace-relative files pushed to the box; nothing else exists there",
        "outputs": "workspace-relative paths pulled back, hashed, and ledgered",
        "timeout_seconds": "per-job cap in seconds; clamped to `max_job_seconds`",
        "env": "extra environment; keys match `ENV_KEY_PATTERN`, values at most 4 KB, forbidden prefixes refused",
        "network": "`bridge` by default, or `none`",
        "label": "free text kept in the job record",
    },
    "gpu_collect": {"job_id": "the job whose outputs are awaiting collection"},
    "gpu_status": {"job_id": "one job to report on; without it, the whole grant"},
}


@dataclass(frozen=True)
class Region:
    block_id: str
    chapter: Optional[str]
    start: int
    end: int
    text: str


def _cell(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
    return text.strip()


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def _code(value: Any) -> str:
    return f"`{value}`"


def _literal(value: Any) -> str:
    if isinstance(value, (tuple, list)):
        if value and all(isinstance(item, (tuple, list)) for item in value):
            return "; ".join("(" + ", ".join(_code(part) for part in item) + ")" for item in value)
        return ", ".join(_code(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{_code(key)}: {_literal(item)}" for key, item in value.items())
    return _code(value)


def _type_of(node: Dict[str, Any]) -> str:
    kind = node.get("type")
    if isinstance(kind, list):
        kind = " or ".join(kind)
    if kind == "array" and isinstance(node.get("items"), dict):
        return f"array of {_type_of(node['items'])}"
    if kind == "object" and isinstance(node.get("additionalProperties"), dict):
        return f"object of {_type_of(node['additionalProperties'])}"
    if kind is None and "enum" in node:
        return "enum"
    if kind is None and "const" in node:
        return "const"
    return kind or "any"


def _constraints(node: Dict[str, Any]) -> str:
    parts: List[str] = []
    if "enum" in node:
        parts.append("one of " + ", ".join(_code(item) for item in node["enum"]))
    if "const" in node:
        parts.append("exactly " + _code(json.dumps(node["const"])))
    for key in ("pattern", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
        if key in node:
            parts.append(f"{key} {_code(node[key])}")
    if node.get("additionalProperties") is False:
        parts.append("no other keys")
    if node.get("uniqueItems"):
        parts.append("unique items")
    if "oneOf" in node:
        alternatives = [", ".join(_code(name) for name in alt.get("required", [])) for alt in node["oneOf"]]
        parts.append("exactly one of: " + " / ".join(alternatives))
    return "; ".join(parts)


def _walk(node: Dict[str, Any], path: str, required: Optional[bool], rows: List[List[str]]) -> None:
    if path:
        flag = "" if required is None else ("yes" if required else "no")
        rows.append([_code(path), _type_of(node), flag, _constraints(node), node.get("description", "")])
    props = node.get("properties")
    if isinstance(props, dict):
        needed = set(node.get("required", []))
        for key, child in props.items():
            _walk(child, f"{path}.{key}" if path else key, key in needed, rows)
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):
        _walk(extra, f"{path}.*", None, rows)
    items = node.get("items")
    if isinstance(items, dict) and (items.get("properties") or items.get("additionalProperties")):
        _walk(items, f"{path}[]", None, rows)


def schema_rows(node: Dict[str, Any], keys: Optional[List[str]] = None) -> List[List[str]]:
    rows: List[List[str]] = []
    needed = set(node.get("required", []))
    for key, child in node.get("properties", {}).items():
        if keys is None or key in keys:
            _walk(child, key, key in needed, rows)
    return rows


SCHEMA_HEADERS = ["field", "type", "required", "constraints", "description"]


def _common_schema() -> Dict[str, Any]:
    compiled = schema.load_cached("researcher", "experiment", "5.0")
    return {key: compiled["properties"][key] for key in schema.STANDARD_MODEL_FIELDS}


def render_index() -> str:
    rows = []
    for chapter_id, title in assembler.CHAPTERS:
        if chapter_id in assembler.CORE_CHAPTERS:
            audience = "every session"
        elif chapter_id == assembler.GPU_CHAPTER:
            audience = ", ".join(assembler.GPU_ROLES) + " with a GPU grant"
        else:
            roles = [role for role, chapters in assembler.ROLE_CHAPTERS.items() if chapter_id in chapters]
            modes = [f"critic/{mode}" for mode, chapters in assembler.MODE_CHAPTERS.items() if chapter_id in chapters]
            audience = ", ".join(roles + modes) or "on disk only"
        rows.append([_code(chapter_id), title, audience, _code(f"{chapter_id}.md")])
    return _table(["chapter", "title", "injected for", "file under .harness/architecture/"], rows)


def _directive_literals() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in DIRECTIVE_SOURCES)


def render_directive_fields() -> str:
    source = _directive_literals()
    rows = [[_code(key), audience, meaning]
            for key, (audience, meaning) in DIRECTIVE_FIELDS.items() if f'"{key}"' in source]
    return _table(["field", "issued to", "carries"], rows)


def render_endings() -> str:
    missing = [ending for ending in SESSION_ENDINGS if ending not in ENDING_MEANINGS]
    if missing:
        raise ValueError(f"ENDING_MEANINGS lacks {missing}")
    return _table(["ending", "meaning"], [[_code(e), ENDING_MEANINGS[e]] for e in SESSION_ENDINGS])


def render_session_limits() -> str:
    from . import supervisor

    retries = supervisor.Runtime.__dataclass_fields__["adapter_retries"].default
    rows = [
        ["tool output tail", _code(tools.OUTPUT_TAIL), "bytes of stdout and stderr returned per tool call; the ledger keeps all of it"],
        ["read limit", _code(tools.READ_LIMIT), "bytes `read_file` returns before setting `truncated`"],
        ["tool timeout", _code(tools.DEFAULT_TOOL_TIMEOUT), "seconds a tool call runs before the harness kills it"],
        ["supplemental cap", _code(assembler.SUPPLEMENTAL_CAP), "bytes of the supplemental block; the rest is cut with a notice"],
        ["adapter retries", _code(retries), "extra sessions the supervisor runs for one directive after a rejection"],
    ]
    for item in fields(SessionBudget):
        rows.append([f"session budget `{item.name}`", _code(item.default), "default when no routing budget names the role"])
    return _table(["limit", "value", "meaning"], rows)


def render_budget_numbers() -> str:
    budgets = routing.load_defaults().get("budgets", {})
    role_rows = [[_code(key), row.get("max_budget_usd"), row.get("wall_clock_seconds"), row.get("max_turns"),
                  row.get("max_tool_turns")] for key, row in budgets.items()]
    line_rows = [
        [_code("budget.API_WARN_USD"), budget.API_WARN_USD, "the fleet degrades: one worker, no refinement"],
        [_code("budget.API_HARD_STOP_USD"), budget.API_HARD_STOP_USD, "no API-billed session starts"],
        [_code("budget.AWS_GPU_STOP_USD"), budget.AWS_GPU_STOP_USD, "no GPU job starts"],
        [_code("budget.AWS_HARD_STOP_USD"), budget.AWS_HARD_STOP_USD, "no session starts on the box"],
    ]
    limits = gpu.LIMITS
    gpu_rows = [[_code(f"gpu.LIMITS[{key!r}]"), limits[key]]
                for key in ("hourly_usd", "max_hours_default", "max_job_seconds") if key in limits]
    return "\n\n".join([
        _table(["routing budget", "max_budget_usd", "wall_clock_seconds", "max_turns", "max_tool_turns"], role_rows),
        _table(["line", "usd", "effect"], line_rows),
        _table(["GPU number", "value"], gpu_rows),
    ])


def render_sandbox_limits() -> str:
    rows = [[_code(f"container.DEFAULT_LIMITS[{key!r}]"), _code(value)] for key, value in container.DEFAULT_LIMITS.items()]
    rows.append([_code("container.REPO_MOUNT"), _code(container.REPO_MOUNT)])
    rows.append([_code("container.DEFAULT_IMAGE"), _code(container.DEFAULT_IMAGE)])
    return _table(["setting", "value"], rows)


def render_thinking_constants() -> str:
    rows = []
    for name in THINKING_CONSTANTS:
        value = getattr(policy, name, None)
        if isinstance(value, (tuple, list, dict, int, str)) and not isinstance(value, bool):
            rows.append([_code(f"policy.{name}"), _literal(value)])
    return _table(["constant", "value"], rows)


def _tool_definitions() -> List[Dict[str, Any]]:
    return list(tools.TOOL_DEFINITIONS) + list(gpu.TOOL_DEFINITIONS)


def _render_tool(definition: Dict[str, Any]) -> Callable[[], str]:
    def render() -> str:
        name = definition["name"]
        spec = definition["input_schema"]
        needed = set(spec.get("required", []))
        notes = ARGUMENT_NOTES.get(name, {})
        rows = []
        for argument, node in spec.get("properties", {}).items():
            note = node.get("description") or notes.get(argument)
            if not note:
                raise ValueError(f"ARGUMENT_NOTES lacks {name}.{argument}")
            constraints = _constraints(node)
            rows.append([_code(argument), _type_of(node), "yes" if argument in needed else "no",
                         note + (f" ({constraints})" if constraints else "")])
        text = definition["description"].strip()
        return f"{_code(name)}: {text}\n\n" + _table(["argument", "type", "required", "meaning"], rows)
    return render


def _render_role_fields(role: str) -> Callable[[], str]:
    def render() -> str:
        modes = [mode for mode, owner in schema.ROLES_BY_MODE.items() if owner == role]
        allowed: Dict[str, List[str]] = OrderedDict()
        required: Dict[str, List[str]] = {}
        for mode in modes:
            compiled = schema.load_cached(role, mode, "5.0")
            for key in schema.allowed_model_keys(role, mode, "5.0"):
                allowed.setdefault(key, []).append(mode)
            for key in compiled.get("required", []):
                required.setdefault(key, []).append(mode)
        missing = sorted(key for key in allowed if key not in FIELD_MEANINGS)
        if missing:
            raise ValueError(f"FIELD_MEANINGS lacks {missing} for {role}")
        rows = [[_code(key), ", ".join(allowed[key]), ", ".join(required.get(key, [])) or "never", FIELD_MEANINGS[key]]
                for key in sorted(allowed)]
        return _table(["field", "allowed in", "required in", "meaning"], rows)
    return render


def render_schema_common() -> str:
    common = {"type": "object", "required": schema.load_cached("researcher", "experiment", "5.0")["required"],
              "properties": _common_schema()}
    return _table(SCHEMA_HEADERS, schema_rows(common))


def render_schema_roles() -> str:
    common = _common_schema()
    sections = []
    for mode, role in schema.ROLES_BY_MODE.items():
        compiled = schema.load_cached(role, mode, "5.0")
        props = compiled["properties"]
        shared = [key for key in props if key in common and props[key] == common[key]]
        differing = [key for key in props if key in common and key not in shared]
        own = {key: value for key, value in props.items() if key not in shared}
        node = {"type": "object", "required": compiled.get("required", []), "properties": own}
        lead = f"{_code(f'{role}/{mode}')}: "
        if shared:
            lead += "common fields as in the common table"
            lead += (" except " + ", ".join(_code(key) for key in differing) + "; ") if differing else "; "
        else:
            lead += "no field matches the common table; "
        lead += "required " + ", ".join(_code(key) for key in compiled.get("required", []))
        rows = schema_rows(node)
        sections.append(lead + ("\n\n" + _table(SCHEMA_HEADERS, rows) if rows else ""))
    return "\n\n".join(sections)


def render_improvement_lists() -> str:
    rows = []
    for label, module, name in IMPROVEMENT_LISTS:
        value = getattr(module, name, None)
        if value is None:
            raise ValueError(f"{label} is not defined; the generator needs it")
        rows.append([_code(label), _literal(value)])
    return _table(["list", "values"], rows)


def _schema_file(name: str) -> Dict[str, Any]:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def render_event_types() -> str:
    types = _schema_file("event.schema.json")["properties"]["type"]["enum"]
    return "\n".join(f"- {_code(name)}" for name in types)


def render_state_keys() -> str:
    state = _schema_file("state.schema.json")
    needed = set(state.get("required", []))
    rows = [[_code(key), _type_of(node), "yes" if key in needed else "no"]
            for key, node in state["properties"].items()]
    return _table(["state key", "type", "required"], rows)


def render_gpu_tools() -> str:
    return _table(["tool", "what it does"], [[_code(t["name"]), t["description"].strip()] for t in gpu.TOOL_DEFINITIONS])


def render_gpu_states() -> str:
    rows = [[_code(state), "yes" if state in gpu.TERMINAL_STATES else "no"] for state in gpu.JOB_STATES]
    return (_table(["job state", "terminal"], rows) + "\n\nRefusal reasons: "
            + ", ".join(_code(reason) for reason in gpu.REFUSAL_REASONS))


def render_gpu_fields() -> str:
    return _table(["job.json field", "meaning"], [[_code(key), value] for key, value in gpu.JOB_FIELDS.items()])


def render_gpu_limits() -> str:
    rows = [[_code(f"gpu.LIMITS[{key!r}]"), _code(value)] for key, value in gpu.LIMITS.items()]
    rows.append([_code("gpu.NETWORKS"), _literal(gpu.NETWORKS)])
    rows.append([_code("gpu.ENV_KEY_PATTERN"), _code(gpu.ENV_KEY_PATTERN)])
    rows.append([_code("gpu.ENV_FORBIDDEN_PREFIXES"), _literal(gpu.ENV_FORBIDDEN_PREFIXES)])
    return _table(["limit", "value"], rows)


def _registry() -> "OrderedDict[str, Tuple[str, Callable[[], str]]]":
    blocks: "OrderedDict[str, Tuple[str, Callable[[], str]]]" = OrderedDict()
    blocks["index"] = ("index", render_index)
    blocks["session.directive_fields"] = ("session", render_directive_fields)
    blocks["session.endings"] = ("session", render_endings)
    blocks["session.limits"] = ("session", render_session_limits)
    blocks["budgets.numbers"] = ("budgets", render_budget_numbers)
    blocks["kernel.thinking.constants"] = ("kernel.thinking", render_thinking_constants)
    for definition in _tool_definitions():
        blocks[f"tools.{definition['name']}"] = ("tools", _render_tool(definition))
    blocks["sandbox.limits"] = ("sandbox", render_sandbox_limits)
    for role in sorted(set(schema.ROLES_BY_MODE.values())):
        blocks[f"roles.{role}.fields"] = (f"roles.{role}", _render_role_fields(role))
    blocks["gpu.tools"] = ("gpu", render_gpu_tools)
    blocks["gpu.states"] = ("gpu", render_gpu_states)
    blocks["gpu.fields"] = ("gpu", render_gpu_fields)
    blocks["gpu.limits"] = ("gpu", render_gpu_limits)
    blocks["improvement.lists"] = ("improvement", render_improvement_lists)
    blocks["schema.common"] = ("schema.common", render_schema_common)
    blocks["schema.roles"] = ("schema.roles", render_schema_roles)
    blocks["kernel.events.types"] = ("kernel.events", render_event_types)
    blocks["kernel.events.state_keys"] = ("kernel.events", render_state_keys)
    return blocks


_BLOCKS = _registry()
GENERATED: "OrderedDict[str, Callable[[], str]]" = OrderedDict((k, v[1]) for k, v in _BLOCKS.items())
BLOCK_CHAPTERS: Dict[str, str] = {k: v[0] for k, v in _BLOCKS.items()}


def blocks_for(chapter_id: str) -> List[str]:
    return [block_id for block_id, chapter in BLOCK_CHAPTERS.items() if chapter == chapter_id]


def render(block_id: str) -> str:
    if block_id not in GENERATED:
        raise ValueError(f"unknown generated block {block_id!r}")
    return GENERATED[block_id]().strip("\n")


def regions(text: str) -> List[Region]:
    """Every generated region with its enclosing chapter; malformed markers raise."""

    found: List[Region] = []
    chapter: Optional[str] = None
    open_id: Optional[str] = None
    start = 0
    lines: List[str] = []
    for index, line in enumerate(text.splitlines()):
        chapter_open = assembler.CHAPTER_OPEN.match(line)
        chapter_close = assembler.CHAPTER_CLOSE.match(line)
        opened = GENERATED_OPEN.match(line)
        closed = GENERATED_CLOSE.match(line)
        if chapter_open:
            chapter = chapter_open.group(1)
        elif chapter_close:
            if open_id is not None:
                raise ValueError(f"generated block {open_id!r} is not closed before chapter {chapter!r} ends")
            chapter = None
        elif opened:
            if open_id is not None:
                raise ValueError(f"line {index + 1}: generated block {opened.group(1)!r} opens inside {open_id!r}")
            if chapter is None:
                raise ValueError(f"line {index + 1}: generated block {opened.group(1)!r} outside any chapter")
            open_id, start, lines = opened.group(1), index, []
        elif closed:
            if open_id is None or closed.group(1) != open_id:
                raise ValueError(f"line {index + 1}: close marker {closed.group(1)!r} does not match {open_id!r}")
            found.append(Region(open_id, chapter, start, index, "\n".join(lines).strip("\n")))
            open_id = None
        elif open_id is not None:
            lines.append(line)
    if open_id is not None:
        raise ValueError(f"generated block {open_id!r} is never closed")
    return found


def check(path: Path = ARCHITECTURE_PATH) -> List[str]:
    """Drift between the file's generated regions and the code; empty when clean."""

    text = path.read_text(encoding="utf-8")
    problems: List[str] = []
    seen: List[str] = []
    for region in regions(text):
        if region.block_id not in GENERATED:
            problems.append(f"{region.block_id}: not a generated block")
            continue
        if region.block_id in seen:
            problems.append(f"{region.block_id}: appears twice")
        seen.append(region.block_id)
        expected = BLOCK_CHAPTERS[region.block_id]
        if region.chapter != expected:
            problems.append(f"{region.block_id}: in chapter {region.chapter!r}, belongs in {expected!r}")
        if region.text != render(region.block_id):
            problems.append(f"{region.block_id}: drift")
    for block_id in GENERATED:
        if block_id not in seen:
            problems.append(f"{block_id}: missing from {path.name}")
    return problems


def _splice(text: str, found: List[Region], bodies: Dict[str, str]) -> str:
    lines = text.splitlines()
    for region in sorted(found, key=lambda r: r.start, reverse=True):
        if region.block_id in bodies:
            lines[region.start + 1:region.end] = bodies[region.block_id].splitlines()
    return "\n".join(lines) + "\n"


def write(path: Path = ARCHITECTURE_PATH) -> List[str]:
    """Regenerate every generated region in place; returns the ids whose text changed."""

    text = path.read_text(encoding="utf-8")
    found = regions(text)
    present = {region.block_id for region in found}
    missing = [block_id for block_id in GENERATED if block_id not in present]
    if missing:
        raise ValueError(f"{path.name} lacks generated regions {missing}; add the markers first")
    bodies = {region.block_id: render(region.block_id) for region in found if region.block_id in GENERATED}
    changed = [region.block_id for region in found if bodies.get(region.block_id, region.text) != region.text]
    path.write_text(_splice(text, found, bodies), encoding="utf-8")
    return changed


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(path: Path, data: bytes) -> str:
    if path.exists():
        os.chmod(path, 0o644)
        path.unlink()
    path.write_bytes(data)
    os.chmod(path, EXPORT_MODE)
    return _sha256(data)


def export(target: Path) -> Dict[str, str]:
    """Write the reference into `target`: one file per chapter, the whole file, and a manifest."""

    target.mkdir(parents=True, exist_ok=True)
    raw = ARCHITECTURE_PATH.read_bytes()
    prompt = assembler.load_prompt(assembler.ARCHITECTURE_FILE)
    files: Dict[str, str] = {}
    for chapter_id, body in assembler.parse_chapters(prompt.body).items():
        name = f"{chapter_id}.md"
        files[name] = _put(target / name, (assembler.strip_generated_markers(body) + "\n").encode("utf-8"))
    files[assembler.ARCHITECTURE_FILE] = _put(target / assembler.ARCHITECTURE_FILE, raw)
    manifest = {"architecture_hash": prompt.sha256, "files": files}
    _put(target / MANIFEST_NAME, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return files


def intact(target: Path) -> bool:
    """Whether the export under `target` matches the shipped reference byte for byte."""

    try:
        manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("architecture_hash") != assembler.prompt_hash(assembler.ARCHITECTURE_FILE):
        return False
    files = manifest.get("files")
    if not isinstance(files, dict) or assembler.ARCHITECTURE_FILE not in files:
        return False
    for name, digest in files.items():
        path = target / name
        if not path.is_file() or _sha256(path.read_bytes()) != digest:
            return False
    return True


def ensure(root: Path) -> bool:
    """Export into `root/.harness/architecture` when the copy is missing, tampered, or stale; True when written."""

    target = root / EXPORT_DIR
    if intact(target):
        return False
    export(target)
    return True


def _split_frontmatter(text: str) -> Tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end < 0:
        return "", text
    return text[:end + 5], text[end + 5:]


def _draft_body(chapter_id: str, raw: str) -> str:
    _, body = assembler.parse_frontmatter(raw)
    lines = body.strip("\n").splitlines()
    if lines and assembler.CHAPTER_OPEN.match(lines[0]):
        lines = lines[1:]
    if lines and assembler.CHAPTER_CLOSE.match(lines[-1]):
        lines = lines[:-1]
    body = "\n".join(lines).strip("\n")
    title = f"## {assembler.CHAPTER_TITLES[chapter_id]}"
    first = next((line for line in lines if line.strip()), "")
    if first.strip() != title:
        body = title + "\n\n" + body
    wrapped = f"<!-- chapter: {chapter_id} -->\n{body}\n<!-- /chapter: {chapter_id} -->"
    present = {region.block_id for region in regions(wrapped)}
    for block_id in blocks_for(chapter_id):
        if block_id not in present:
            body += f"\n\n<!-- generated: {block_id} -->\n<!-- /generated: {block_id} -->"
    return body


def assemble_drafts(drafts_dir: Path, path: Path = ARCHITECTURE_PATH) -> List[str]:
    """Replace each chapter that has `<drafts_dir>/<id>.md` with the draft, then regenerate the blocks."""

    text = path.read_text(encoding="utf-8")
    head, body = _split_frontmatter(text)
    chapters = assembler.parse_chapters(body)
    unknown = [chapter_id for chapter_id in chapters if chapter_id not in assembler.CHAPTER_TITLES]
    if unknown:
        raise ValueError(f"{path.name} carries unknown chapters {unknown}")
    applied: List[str] = []
    for chapter_id in assembler.CHAPTER_IDS:
        draft = drafts_dir / f"{chapter_id}.md"
        if draft.is_file():
            chapters[chapter_id] = _draft_body(chapter_id, draft.read_text(encoding="utf-8"))
            applied.append(chapter_id)
    parts = [f"<!-- chapter: {chapter_id} -->\n{chapters[chapter_id]}\n<!-- /chapter: {chapter_id} -->"
             for chapter_id in assembler.CHAPTER_IDS if chapter_id in chapters]
    path.write_text(head + "\n".join(parts) + "\n", encoding="utf-8")
    write(path)
    return applied


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generated blocks of the system reference")
    parser.add_argument("--check", action="store_true", help="report drift; exit 1 when any")
    parser.add_argument("--write", action="store_true", help="regenerate the blocks in place")
    parser.add_argument("--export", metavar="DIR", help="write the chapter files and manifest into DIR")
    parser.add_argument("--assemble", metavar="DIR", help="splice DIR/<chapter>.md drafts into the file")
    parser.add_argument("--render", metavar="ID", help="print one generated block")
    parser.add_argument("--path", default=str(ARCHITECTURE_PATH))
    args = parser.parse_args(argv)
    path = Path(args.path)
    if args.render:
        print(render(args.render))
    if args.assemble:
        for chapter_id in assemble_drafts(Path(args.assemble), path):
            print(f"assembled {chapter_id}")
    if args.write:
        for block_id in write(path):
            print(f"rewrote {block_id}")
    if args.export:
        for name, digest in export(Path(args.export)).items():
            print(f"{digest}  {name}")
    if args.check:
        problems = check(path)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
