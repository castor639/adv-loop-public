"""The model-facing schemas must stay a strict subset of what the engine accepts."""

from __future__ import annotations

import json
import unittest

from harness import schema

# Literal copies of the engine's allowed-key sets (engine.py, _validate_and_normalize_attempt
# and the explorer/triage/surgeon/overlay validators). If the kernel changes these, this
# test and the schema module both need a deliberate update.
ENGINE_STANDARD = {
    "request_id", "directive_id", "role", "mode", "actor", "strategy", "hypothesis",
    "action", "observation", "interpretation", "uncertainties", "next_step", "outcome",
    "evidence", "criterion_updates", "contradictions", "contradiction_resolutions", "decisions",
    "plan", "plan_id", "criterion_targets", "assessment", "contradiction_search",
    "blocker_audit", "verification_results", "report",
}
ENGINE_V3_EXTRA = {
    "researcher": {"basin", "representation_shift", "move", "candidate_id", "assets_registered",
                   "combination", "barrier_probe", "replication_of", "wishes_declared"},
    "critic": {"lens", "wishes_declared"},
    "planner": {"proposed_criteria"},
}
ENGINE_SPECIAL = {
    ("explorer", "ideation"): {"request_id", "directive_id", "role", "mode", "actor", "context_scope",
                               "ideation_kind", "seeds", "decisions", "evidence", "criterion_updates",
                               "contradictions", "contradiction_resolutions"},
    ("critic", "triage"): {"request_id", "directive_id", "role", "mode", "actor", "triage", "decisions",
                           "evidence", "criterion_updates", "contradictions", "contradiction_resolutions"},
    ("surgeon", "overlay_diagnosis"): {"request_id", "directive_id", "role", "mode", "actor", "diagnosis",
                                       "decisions", "evidence", "criterion_updates", "contradictions",
                                       "contradiction_resolutions"},
    ("critic", "overlay_review"): {"request_id", "directive_id", "role", "mode", "actor", "overlay_review",
                                   "decisions", "evidence", "criterion_updates", "contradictions",
                                   "contradiction_resolutions"},
}
# observation_notes is a harness field that becomes part of `observation`.
HARNESS_ONLY = {"observation_notes"}


def engine_allowed(role: str, mode: str) -> set:
    if (role, mode) in ENGINE_SPECIAL:
        return ENGINE_SPECIAL[(role, mode)]
    return ENGINE_STANDARD | ENGINE_V3_EXTRA.get(role, set())


class SchemaSubsetTests(unittest.TestCase):
    def test_every_mode_compiles_to_a_subset_of_the_engine_keys(self):
        for role, mode, version in schema.all_targets():
            compiled = schema.compile_model_schema(role, mode, version)
            keys = set(compiled["properties"]) - HARNESS_ONLY
            extra = keys - engine_allowed(role, mode)
            self.assertFalse(extra, f"{role}/{mode} exposes keys the engine rejects: {sorted(extra)}")
            owned = keys & schema.HARNESS_OWNED
            self.assertFalse(owned, f"{role}/{mode} asks the model for harness-owned {sorted(owned)}")
            self.assertTrue(set(compiled["required"]).issubset(set(compiled["properties"])))

    def test_compiled_schemas_are_draft7_without_refs(self):
        for role, mode, version in schema.all_targets():
            compiled = schema.compile_model_schema(role, mode, version)
            self.assertEqual(compiled["$schema"], schema.DRAFT7)
            text = json.dumps(compiled)
            for token in ('"$ref"', '"$id"', '"$defs"'):
                self.assertNotIn(token, text, f"{role}/{mode} still carries {token}")

    def test_model_evidence_names_an_artifact_or_a_verdict_never_a_hash(self):
        compiled = schema.compile_model_schema("researcher", "experiment")
        item = compiled["properties"]["evidence"]["items"]
        self.assertNotIn("fingerprint", item["properties"])
        self.assertIn("artifact_path", item["properties"])
        self.assertIn("verdict_id", item["properties"])
        self.assertEqual(item["oneOf"], [{"required": ["artifact_path"]}, {"required": ["verdict_id"]}])

    def test_seeds_omit_the_engine_computed_fingerprint(self):
        compiled = schema.compile_model_schema("explorer", "ideation")
        seed = compiled["properties"]["seeds"]["items"]
        self.assertNotIn("fingerprint", seed["properties"])
        self.assertNotIn("fingerprint", seed["required"])
        self.assertEqual(compiled["properties"]["evidence"], {"type": "array", "maxItems": 0})

    def test_roles_do_not_see_each_others_fields(self):
        researcher = set(schema.compile_model_schema("researcher", "experiment")["properties"])
        planner = set(schema.compile_model_schema("planner", "initial_plan")["properties"])
        self.assertNotIn("plan", researcher)
        self.assertNotIn("assessment", researcher)
        self.assertNotIn("criterion_targets", planner)
        self.assertIn("lessons_addressed", schema.compile_model_schema("planner", "fresh_replan")["properties"]["plan"]["properties"])
        self.assertIn("rejected_assumptions", schema.compile_model_schema("planner", "fresh_replan")["properties"]["plan"]["required"])

    def test_cache_matches_the_compiler(self):
        for role, mode, version in schema.all_targets():
            path = schema.CACHE / schema.cache_name(role, mode, version)
            self.assertTrue(path.exists(), f"missing cache {path.name}; run python -m harness.schema --regen")
            cached = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(cached, schema.compile_model_schema(role, mode, version), f"stale cache {path.name}")

    def test_unknown_role_mode_pair_is_refused(self):
        with self.assertRaises(ValueError):
            schema.compile_model_schema("planner", "experiment")


if __name__ == "__main__":
    unittest.main()


class ProposedCriteriaOnlyWherePermitted(unittest.TestCase):
    """The kernel refuses `proposed_criteria` outside decompose/fresh_replan, so the model must not be offered it."""

    def test_initial_plan_omits_field(self):
        props = schema.compile_model_schema("planner", "initial_plan", "5.0")["properties"]
        self.assertNotIn("proposed_criteria", props)

    def test_decompose_and_fresh_replan_keep_field(self):
        for mode in ("decompose", "fresh_replan"):
            props = schema.compile_model_schema("planner", mode, "5.0")["properties"]
            self.assertIn("proposed_criteria", props, mode)
