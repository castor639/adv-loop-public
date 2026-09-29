"""Compile the model-facing JSON schema for one (role, mode).

The kernel validates the full attempt with its own gates; this module produces
the strict subset a model must return: draft-07, no external ``$ref``, no
harness-owned fields, ``additionalProperties: false`` everywhere the engine
rejects unknown keys. The allowed-key sets mirror
``LoopEngine._validate_and_normalize_attempt`` and the special validators for
explorer, triage, surgeon, and overlay review.

Regenerate the cache with ``python -m harness.schema --regen`` after a kernel
change; ``tests/test_harness_schema.py`` fails if the cache is stale.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
CACHE = Path(__file__).resolve().parent / "schema_cache"
DRAFT7 = "http://json-schema.org/draft-07/schema#"

HARNESS_OWNED = frozenset({
    "id", "at", "request_id", "directive_id", "role", "mode", "actor",
    "strategy_fingerprint", "observation",
})

ROLES_BY_MODE = {
    "initial_plan": "planner",
    "decompose": "planner",
    "fresh_replan": "planner",
    "experiment": "researcher",
    "attempt_review": "critic",
    "contradiction_search": "critic",
    "blocker_audit": "critic",
    "triage": "critic",
    "overlay_review": "critic",
    "independent_verification": "verifier",
    "final_report": "synthesizer",
    "ideation": "explorer",
    "overlay_diagnosis": "surgeon",
}

STANDARD_MODEL_FIELDS = (
    "strategy", "hypothesis", "action", "observation_notes", "interpretation",
    "uncertainties", "next_step", "outcome", "evidence", "criterion_updates",
    "contradictions", "contradiction_resolutions", "decisions",
)
PROOF_INERT_FIELDS = ("evidence", "criterion_updates", "contradictions", "contradiction_resolutions")

STRING = {"type": "string", "minLength": 1}
STRING_LIST = {"type": "array", "items": STRING}
ID_LIST = {"type": "array", "items": {"type": "string", "minLength": 1}}


def _obj(required: List[str], properties: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}


def _load(name: str) -> Dict[str, Any]:
    with open(SCHEMAS / name, encoding="utf-8") as handle:
        return json.load(handle)


def _inline(node: Any, attempt: Dict[str, Any], evidence: Dict[str, Any], report: Dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline(item, attempt, evidence, report) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref.endswith("#/$defs/strategy"):
            return _inline(copy.deepcopy(attempt["$defs"]["strategy"]), attempt, evidence, report)
        if ref.endswith("#/$defs/seed"):
            return _inline(copy.deepcopy(attempt["$defs"]["seed"]), attempt, evidence, report)
        if ref == "report.schema.json":
            return _inline(_strip(copy.deepcopy(report)), attempt, evidence, report)
        if ref == "evidence.schema.json":
            return _model_evidence(evidence)
        if ref.endswith("#/properties/actor"):
            return copy.deepcopy(evidence["properties"]["actor"])
        raise ValueError(f"unhandled $ref {ref}")
    return {key: _inline(value, attempt, evidence, report) for key, value in node.items()}


def _strip(schema: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("$schema", "$id", "title"):
        schema.pop(key, None)
    return schema


def _model_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Evidence as the model submits it: names an artifact or a verdict, never a hash."""

    props = evidence["properties"]
    model_props = {
        "ref": STRING,
        "kind": copy.deepcopy(props["kind"]),
        "quality": copy.deepcopy(props["quality"]),
        "claim": copy.deepcopy(props["claim"]),
        "locator": copy.deepcopy(props["locator"]),
        "method": copy.deepcopy(props["method"]),
        "independence_key": copy.deepcopy(props["independence_key"]),
        "supports": copy.deepcopy(props["supports"]),
        "artifact_path": {"type": "string", "minLength": 1,
                          "description": "Workspace-relative path; the harness computes the fingerprint."},
        "verdict_id": {"type": "string", "pattern": "^V[0-9]{4}$",
                       "description": "A checker verdict id issued in this session by adv-validate."},
        "formalization_rank": copy.deepcopy(props["formalization_rank"]),
        "theory_base_hash": copy.deepcopy(props["theory_base_hash"]),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref", "kind", "quality", "claim", "method", "independence_key", "supports"],
        "properties": model_props,
        "oneOf": [{"required": ["artifact_path"]}, {"required": ["verdict_id"]}],
    }


def _shared_objects() -> Dict[str, Any]:
    refs = {"type": "array", "items": STRING,
            "description": "ref values of evidence entries in this same submission (never verdict ids or paths)"}
    ids = {"type": "array", "items": {"type": "string", "pattern": "^E[0-9]{6}$"},
           "description": "ids of evidence already recorded in the workspace (E000012 form); never a verdict id"}
    return {
        "criterion_updates": {"type": "array", "items": _obj(
            ["id", "status"],
            {"id": STRING, "status": {"enum": ["open", "partial", "satisfied", "failed_verification"]},
             "evidence_refs": refs, "evidence_ids": ids, "reason": STRING})},
        "contradictions": {"type": "array", "items": _obj(
            ["id", "claim", "severity"],
            {"id": STRING, "claim": STRING, "severity": {"enum": ["critical", "noncritical"]},
             "evidence_refs": refs, "evidence_ids": ids})},
        "contradiction_resolutions": {"type": "array", "items": _obj(
            ["id", "resolution"],
            {"id": STRING, "resolution": STRING, "evidence_refs": refs, "evidence_ids": ids})},
        "decisions": {"type": "array", "items": _obj(
            ["decision", "rationale"],
            {"decision": STRING, "rationale": STRING, "rejected_alternatives": STRING_LIST})},
        "observation_notes": {"type": "string", "minLength": 1,
                              "description": "Your reading of the recorded tool results. The raw observation is generated by the harness."},
    }


def _plan(mode: str, policy_version: str) -> Dict[str, Any]:
    nonempty = {"type": "array", "minItems": 1, "items": STRING}
    props = {
        "assumptions": nonempty,
        "subproblems": {"type": "array", "minItems": 2 if mode == "decompose" else 1, "items": STRING},
        "candidate_experiments": nonempty,
        "falsification_tests": nonempty,
        "rejected_assumptions": nonempty if mode == "fresh_replan" else STRING_LIST,
    }
    required = ["assumptions", "subproblems", "candidate_experiments", "falsification_tests"]
    if mode == "fresh_replan":
        required.append("rejected_assumptions")
    if policy_version != "2.0":
        props["lessons_addressed"] = {"type": "array", "items": _obj(
            ["lesson_id", "response"], {"lesson_id": {"type": "string", "pattern": "^L[0-9]{4}$"}, "response": STRING})}
    return _obj(required, props)


def _assessment(policy_version: str) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "target_attempt_id": {"type": "string", "pattern": "^A[0-9]{6}$"},
        "verdict": {"enum": ["validated_progress", "mixed", "no_progress", "invalid"]},
        "reasons": {"type": "array", "minItems": 1, "items": STRING},
        "uncertainty": STRING,
    }
    if policy_version != "2.0":
        props["candidate_causes"] = {"type": "array", "items": _obj(
            ["cause", "discriminating_test"], {"cause": STRING, "discriminating_test": STRING})}
        props["control_check"] = _obj(
            ["control_id", "outcome"],
            {"control_id": STRING, "outcome": {"enum": ["rejects_control", "endorses_control", "not_applicable"]},
             "note": STRING})
    if policy_version not in ("2.0", "3.0"):
        props["harness_gap"] = {"type": "boolean"}
    return _obj(["target_attempt_id", "verdict", "reasons", "uncertainty"], props)


CONTRADICTION_SEARCH = _obj(
    ["assumptions_checked", "disconfirming_queries", "conclusion"],
    {"assumptions_checked": STRING_LIST, "disconfirming_queries": STRING_LIST, "conclusion": STRING})

BLOCKER_AUDIT = _obj(
    ["dependency", "dependency_evidence_ids", "safe_alternative_attempt_ids", "unsafe_alternatives_rejected",
     "exhaustion_reason", "human_action", "safe_alternatives_exhausted"],
    {"dependency": STRING, "dependency_evidence_ids": ID_LIST,
     "safe_alternative_attempt_ids": {"type": "array", "minItems": 3, "items": {"type": "string", "pattern": "^A[0-9]{6}$"}},
     "unsafe_alternatives_rejected": STRING_LIST, "exhaustion_reason": STRING, "human_action": STRING,
     "safe_alternatives_exhausted": {"const": True}})

VERIFICATION_RESULTS = {"type": "array", "minItems": 1, "items": _obj(
    ["criterion_id", "verdict", "evidence_refs", "method", "observation"],
    {"criterion_id": STRING, "verdict": {"enum": ["pass", "fail"]}, "evidence_refs": {"type": "array", "minItems": 1, "items": STRING},
     "method": STRING, "observation": STRING})}

OVERLAY_OPS = (
    "register_evidence_kind", "register_validator_hook", "register_sandbox", "add_stall_class",
    "add_role_instructions", "add_criterion", "require_min_rank", "raise_criterion_rank", "pin_toolchain_hash",
)

DIAGNOSIS = _obj(
    ["raw_detail", "classification", "kill_test", "next_experiment"],
    {"raw_detail": STRING,
     "classification": {"enum": ["harness_gap", "research_failure", "human_dependency"]},
     "kill_test": STRING, "next_experiment": STRING,
     "fault_signature": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
     "proposed_delta": _obj(["schema_version", "ops"], {
         "schema_version": {"const": 1},
         "ops": {"type": "array", "minItems": 1, "items": {
             "type": "object", "required": ["op"],
             "properties": {"op": {"enum": list(OVERLAY_OPS)}}}}}),
     "delta_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"}})

OVERLAY_REVIEW = _obj(
    ["target_attempt_id", "verdict", "reasons", "uncertainty"],
    {"target_attempt_id": {"type": "string", "pattern": "^A[0-9]{6}$"},
     "verdict": {"enum": ["adopt", "reject", "narrow"]},
     "reasons": {"type": "array", "minItems": 1, "items": STRING},
     "uncertainty": STRING,
     "narrowed_delta": {"type": "object"}})


def allowed_model_keys(role: str, mode: str, policy_version: str) -> Set[str]:
    """The keys the engine accepts for this role and mode, minus harness-owned ones, plus observation_notes."""

    special = {
        "explorer": {"context_scope", "ideation_kind", "seeds", "decisions", *PROOF_INERT_FIELDS},
        "surgeon": {"diagnosis", "decisions", *PROOF_INERT_FIELDS},
    }
    if role in special:
        return set(special[role])
    if mode == "triage":
        return {"triage", "decisions", *PROOF_INERT_FIELDS}
    if mode == "overlay_review":
        return {"overlay_review", "decisions", *PROOF_INERT_FIELDS}
    allowed = set(STANDARD_MODEL_FIELDS)
    if role == "planner":
        allowed.add("plan")
        if policy_version != "2.0":
            allowed.add("proposed_criteria")
    elif role == "researcher":
        allowed |= {"plan_id", "criterion_targets"}
        if policy_version != "2.0":
            allowed |= {"basin", "representation_shift", "move", "candidate_id", "assets_registered",
                        "combination", "barrier_probe", "replication_of", "wishes_declared"}
    elif role == "critic":
        allowed.add({"attempt_review": "assessment", "contradiction_search": "contradiction_search",
                     "blocker_audit": "blocker_audit"}[mode])
        if policy_version != "2.0":
            allowed |= {"lens", "wishes_declared"}
    elif role == "verifier":
        allowed.add("verification_results")
    elif role == "synthesizer":
        allowed.add("report")
    return allowed


def _required(role: str, mode: str, policy_version: str) -> List[str]:
    if role == "explorer":
        return ["context_scope", "ideation_kind", "seeds", "decisions"]
    if role == "surgeon":
        return ["diagnosis", "decisions"]
    if mode == "triage":
        return ["triage", "decisions"]
    if mode == "overlay_review":
        return ["overlay_review", "decisions"]
    required = list(STANDARD_MODEL_FIELDS)
    if role == "planner":
        required.append("plan")
    elif role == "researcher":
        required.extend(["plan_id", "criterion_targets"])
        if policy_version != "2.0":
            required += ["basin", "move"]
    elif role == "critic" and mode == "attempt_review":
        required.append("assessment")
    elif mode == "contradiction_search":
        required.append("contradiction_search")
    elif mode == "blocker_audit":
        required.append("blocker_audit")
    elif role == "verifier":
        required.append("verification_results")
    elif role == "synthesizer":
        required.append("report")
    return required


def _property_map(role: str, mode: str, policy_version: str) -> Dict[str, Any]:
    attempt, evidence, report = _load("attempt.schema.json"), _load("evidence.schema.json"), _load("report.schema.json")
    base = _inline(copy.deepcopy(attempt["properties"]), attempt, evidence, report)
    shared = _shared_objects()
    props: Dict[str, Any] = {}
    for key in sorted(allowed_model_keys(role, mode, policy_version)):
        if key in shared:
            props[key] = copy.deepcopy(shared[key])
        elif key == "plan":
            props[key] = _plan(mode, policy_version)
        elif key == "assessment":
            props[key] = _assessment(policy_version)
        elif key == "contradiction_search":
            props[key] = copy.deepcopy(CONTRADICTION_SEARCH)
        elif key == "blocker_audit":
            props[key] = copy.deepcopy(BLOCKER_AUDIT)
        elif key == "verification_results":
            props[key] = copy.deepcopy(VERIFICATION_RESULTS)
        elif key == "diagnosis":
            props[key] = copy.deepcopy(DIAGNOSIS)
        elif key == "overlay_review":
            props[key] = copy.deepcopy(OVERLAY_REVIEW)
        elif key == "seeds":
            seeds = copy.deepcopy(base["seeds"])
            seed = seeds["items"]
            seed["properties"].pop("fingerprint", None)
            seed["required"] = [name for name in seed["required"] if name != "fingerprint"]
            props[key] = seeds
        elif key in base:
            props[key] = copy.deepcopy(base[key])
        else:
            raise ValueError(f"no schema for {key}")
    if role == "explorer" or role == "surgeon" or mode in ("triage", "overlay_review"):
        for name in PROOF_INERT_FIELDS:
            props[name] = {"type": "array", "maxItems": 0}
    if mode not in ("decompose", "fresh_replan") and "proposed_criteria" in props:
        # the engine lets only decompose and fresh_replan propose criteria; do not offer the field elsewhere
        del props["proposed_criteria"]
    if role in ("planner", "verifier", "synthesizer") and "criterion_updates" in props:
        # the engine refuses status claims from these roles; make it mechanical before the kernel
        props["criterion_updates"] = {"type": "array", "maxItems": 0}
    return props


def compile_model_schema(role: str, mode: str, policy_version: str = "5.0") -> Dict[str, Any]:
    if ROLES_BY_MODE.get(mode) != role:
        raise ValueError(f"{role}/{mode} is not a directive the policy issues")
    schema = {
        "$schema": DRAFT7,
        "title": f"ADV Loop model output for {role}/{mode} (policy {policy_version})",
        "type": "object",
        "additionalProperties": False,
        "required": _required(role, mode, policy_version),
        "properties": _property_map(role, mode, policy_version),
    }
    _assert_draft7_clean(schema)
    return schema


def _assert_draft7_clean(node: Any) -> None:
    if isinstance(node, dict):
        if "$ref" in node or "$id" in node or "$defs" in node:
            raise ValueError("compiled schema must not carry $ref, $id, or $defs")
        for value in node.values():
            _assert_draft7_clean(value)
    elif isinstance(node, list):
        for item in node:
            _assert_draft7_clean(item)


def cache_name(role: str, mode: str, policy_version: str) -> str:
    return f"{role}.{mode}.{policy_version}.json"


def all_targets(policy_versions: Tuple[str, ...] = ("5.0",)) -> List[Tuple[str, str, str]]:
    return [(role, mode, version) for mode, role in ROLES_BY_MODE.items() for version in policy_versions]


def load_cached(role: str, mode: str, policy_version: str = "5.0") -> Dict[str, Any]:
    path = CACHE / cache_name(role, mode, policy_version)
    if path.exists():
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return compile_model_schema(role, mode, policy_version)


def regenerate() -> List[Path]:
    CACHE.mkdir(exist_ok=True)
    written = []
    for role, mode, version in all_targets():
        path = CACHE / cache_name(role, mode, version)
        path.write_text(json.dumps(compile_model_schema(role, mode, version), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile model-facing schemas")
    parser.add_argument("--regen", action="store_true")
    parser.add_argument("--print", nargs=2, metavar=("ROLE", "MODE"))
    args = parser.parse_args()
    if args.regen:
        for path in regenerate():
            print(path)
    if args.print:
        print(json.dumps(compile_model_schema(args.print[0], args.print[1]), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
