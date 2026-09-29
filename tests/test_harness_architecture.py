"""The system reference: structure, selection, size, lexicon, drift, export, and its wiring into prompts."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import architecture, assembler, improvement, jsonschema_lite, routing, schema, transcripts
from harness.backends.base import ARCHITECTURE_TIERS, SESSION_ENDINGS, ModelSpec, SessionResult

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT = assembler.load_prompt("ARCHITECTURE.md")
BODY = PROMPT.body
RAW = architecture.ARCHITECTURE_PATH.read_bytes()
CORE = list(assembler.CORE_CHAPTERS)
LOOSENING_PHRASES = ("may skip", "may waive", "is optional", "may omit", "treat as satisfied", "can relax")
AI_TELLS = ("delve", "leverage", "robust", "seamless", "it is important to note", "in summary")
PATH_PREFIXES = ("src", "harness", "schemas", "docs", "protocols", "prompts", "profiles", "checkers", "adapters", "tests")
SUBORDINATION = ("This reference describes the system. It grants nothing and waives nothing. Where it differs from the"
                 " harness contract, the kernel's gates, the mode procedure, or the charter, those govern and this text"
                 " is wrong.")
# The role tier for every directive the policy issues; lean and core derive from it by rule.
GOLDEN_ROLE_TIER = {
    ("planner", "initial_plan"): CORE + ["kernel.thinking", "evidence", "tools", "sandbox", "roles.planner"],
    ("planner", "decompose"): CORE + ["kernel.thinking", "evidence", "tools", "sandbox", "roles.planner"],
    ("planner", "fresh_replan"): CORE + ["kernel.thinking", "evidence", "tools", "sandbox", "roles.planner"],
    ("researcher", "experiment"): CORE + ["kernel.thinking", "evidence", "tools", "sandbox", "roles.researcher"],
    ("critic", "attempt_review"): CORE + ["kernel.thinking", "evidence", "tools", "sandbox", "roles.critic"],
    ("critic", "contradiction_search"): CORE + ["kernel.thinking", "evidence", "tools", "sandbox", "roles.critic"],
    ("critic", "blocker_audit"): CORE + ["kernel.thinking", "evidence", "tools", "sandbox", "roles.critic"],
    ("critic", "triage"): CORE + ["kernel.thinking", "roles.critic"],
    ("critic", "overlay_review"): CORE + ["roles.critic", "improvement"],
    ("verifier", "independent_verification"): CORE + ["evidence", "tools", "sandbox", "roles.verifier"],
    ("synthesizer", "final_report"): CORE + ["roles.synthesizer"],
    ("explorer", "ideation"): CORE + ["roles.explorer"],
    ("surgeon", "overlay_diagnosis"): CORE + ["evidence", "roles.surgeon", "improvement"],
}


def directive(role: str, mode: str, **extra):
    base = {"action": "attempt", "directive_id": "d-" + mode, "role": role, "mode": mode,
            "task": "t", "open_criteria": [], "instructions": [], "policy_version": "5.0"}
    if role == "researcher":
        base["active_plan_id"] = "P0001"
    base.update(extra)
    return base


def in_document_order(ids):
    return [chapter_id for chapter_id in assembler.CHAPTER_IDS if chapter_id in set(ids)]


class StructureTests(unittest.TestCase):
    def test_frontmatter_marks_the_file_immutable(self):
        self.assertEqual(PROMPT.meta.get("prompt_id"), "architecture")
        self.assertEqual(PROMPT.meta.get("immutable"), "true")
        self.assertEqual(PROMPT.meta.get("applies_to"), "all")
        self.assertIn("ARCHITECTURE.md", assembler.IMMUTABLE_PROMPTS)

    def test_chapters_are_well_formed_unique_ordered_and_cover_the_file(self):
        chapters = assembler.parse_chapters(BODY)
        self.assertEqual(list(chapters), list(assembler.CHAPTER_IDS))
        for chapter_id, title in assembler.CHAPTERS:
            self.assertTrue(chapters[chapter_id].startswith(f"## {title}\n"), chapter_id)
        opens = re.findall(r"^<!-- chapter: ([a-z][a-z0-9_.]*) -->$", BODY, re.M)
        closes = re.findall(r"^<!-- /chapter: ([a-z][a-z0-9_.]*) -->$", BODY, re.M)
        self.assertEqual(opens, closes)
        self.assertEqual(len(opens), len(set(opens)))

    def test_no_h1_and_no_rules_in_the_body(self):
        for line in BODY.splitlines():
            self.assertFalse(line.startswith("# "), line)
            self.assertNotEqual(line.strip(), "---", "a --- line would split the prompt sections")

    def test_registries_name_only_known_chapters(self):
        known = set(assembler.CHAPTER_IDS)
        self.assertTrue(set(assembler.CORE_CHAPTERS) <= known)
        for role, chapters in assembler.ROLE_CHAPTERS.items():
            self.assertTrue(set(chapters) <= known, role)
            self.assertIn(f"roles.{role}", chapters, role)
        for mode, chapters in assembler.MODE_CHAPTERS.items():
            self.assertTrue(set(chapters) <= known, mode)
        self.assertTrue(set(architecture.BLOCK_CHAPTERS.values()) <= known)
        self.assertEqual(set(assembler.ROLE_CHAPTERS), set(schema.ROLES_BY_MODE.values()) | {"refine"})

    def test_parser_rejects_malformed_marker_layouts(self):
        good = "<!-- chapter: a -->\ntext\n<!-- /chapter: a -->\n"
        self.assertEqual(assembler.parse_chapters(good), {"a": "text"})
        bad = {
            "nesting": "<!-- chapter: a -->\n<!-- chapter: b -->\n<!-- /chapter: b -->\n<!-- /chapter: a -->\n",
            "duplicate": good + good,
            "unclosed": "<!-- chapter: a -->\ntext\n",
            "mismatch": "<!-- chapter: a -->\ntext\n<!-- /chapter: b -->\n",
            "outside": "stray\n" + good,
        }
        for name, text in bad.items():
            with self.assertRaises(ValueError, msg=name):
                assembler.parse_chapters(text)

    def test_orientation_carries_the_subordination_sentence(self):
        self.assertIn(SUBORDINATION, assembler.parse_chapters(BODY)["orientation"])

    def test_no_author_todo_left(self):
        self.assertNotIn("TODO(author)", BODY)


class SelectionTests(unittest.TestCase):
    def test_golden_table_for_every_directive_tier_and_grant(self):
        for mode, role in schema.ROLES_BY_MODE.items():
            full = GOLDEN_ROLE_TIER[(role, mode)]
            lean = in_document_order(CORE + [c for c in full if c in ("evidence", f"roles.{role}")])
            for gpu in (False, True):
                grant = gpu and role in assembler.GPU_ROLES
                self.assertEqual(assembler.select_chapters(role, mode, tier="core", gpu=gpu), CORE, (role, mode))
                self.assertEqual(assembler.select_chapters(role, mode, tier="lean", gpu=gpu),
                                 in_document_order(lean + (["gpu"] if grant else [])), (role, mode, gpu))
                self.assertEqual(assembler.select_chapters(role, mode, tier="role", gpu=gpu),
                                 in_document_order(full + (["gpu"] if grant else [])), (role, mode, gpu))
            self.assertNotIn("evidence", assembler.select_chapters("explorer", "ideation", tier="role", gpu=True))

    def test_core_tier_stays_core_and_no_shipped_alias_uses_it(self):
        core_aliases = [alias for alias, spec in routing.load_defaults()["models"].items()
                        if spec.get("architecture_tier") == "core"]
        # Every shipped alias can be handed the sandbox, so none of them ships at core.
        self.assertEqual(core_aliases, [])
        for mode, role in schema.ROLES_BY_MODE.items():
            for gpu in (False, True):
                self.assertEqual(assembler.select_chapters(role, mode, tier="core", gpu=gpu), CORE)

    def test_refine_gets_core_improvement_and_its_role_chapter(self):
        for kind in ("proposer", "narrowness_critic", "adoption_critic"):
            self.assertEqual(assembler.select_chapters("refine", kind, tier="role"), CORE + ["improvement", "roles.refine"])
            self.assertEqual(assembler.select_chapters("refine", kind, tier="lean"), CORE + ["roles.refine"])
            self.assertEqual(assembler.select_chapters("refine", kind, tier="role", gpu=True), CORE + ["improvement", "roles.refine"])

    def test_unknown_inputs_raise(self):
        with self.assertRaises(ValueError):
            assembler.select_chapters("researcher", "experiment", tier="huge")
        with self.assertRaises(ValueError):
            assembler.select_chapters("steward", None)
        with patch.dict(assembler.ROLE_CHAPTERS, {"researcher": ("kernel.thinking", "no.such.chapter")}):
            with self.assertRaises(ValueError):
                assembler.select_chapters("researcher", "experiment", tier="role")

    def test_system_reference_keeps_chapter_markers_and_drops_generated_ones(self):
        text, chapters = assembler.system_reference("researcher", "experiment", tier="role", gpu=True)
        self.assertTrue(text.startswith(assembler.SYSTEM_REFERENCE_HEADING + "\n\n<!-- chapter: orientation -->\n"))
        self.assertEqual(chapters, GOLDEN_ROLE_TIER[("researcher", "experiment")] + ["gpu"])
        for chapter_id in chapters:
            self.assertIn(f"<!-- chapter: {chapter_id} -->", text)
            self.assertIn(f"<!-- /chapter: {chapter_id} -->", text)
        self.assertNotIn("<!-- generated:", text)
        self.assertNotIn("<!-- /generated:", text)
        self.assertNotIn("<!-- chapter: glossary -->", text)


class SizeTests(unittest.TestCase):
    def test_file_caps(self):
        self.assertLessEqual(len(RAW), 450 * 1024)
        self.assertLessEqual(RAW.count(b"\n"), 5000)

    def test_injected_caps_per_tier(self):
        caps = {"core": 36 * 1024, "lean": 96 * 1024, "role": 128 * 1024}
        for mode, role in schema.ROLES_BY_MODE.items():
            for tier, cap in caps.items():
                for gpu in (False, True):
                    text, _ = assembler.system_reference(role, mode, tier=tier, gpu=gpu)
                    self.assertLessEqual(len(text.encode("utf-8")), cap, (role, mode, tier, gpu))
        for kind in ("proposer", "narrowness_critic", "adoption_critic"):
            text, _ = assembler.system_reference("refine", kind, tier="role")
            self.assertLessEqual(len(text.encode("utf-8")), caps["role"], kind)

    def test_exported_chapter_files_stay_under_forty_kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = architecture.export(Path(tmp))
            for name in files:
                if name != "ARCHITECTURE.md":
                    self.assertLessEqual((Path(tmp) / name).stat().st_size, 40 * 1024, name)


class LintTests(unittest.TestCase):
    def test_loosening_lexicon_is_absent(self):
        low = BODY.lower()
        for phrase in LOOSENING_PHRASES:
            self.assertNotIn(phrase, low, phrase)
        self.assertEqual(improvement.loosening_hits(BODY), [])

    def test_ai_tells_are_absent(self):
        low = BODY.lower()
        for tell in AI_TELLS:
            self.assertNotIn(tell, low, tell)

    def test_gpu_guide_and_mode_bullets_pass_the_same_lexicon(self):
        texts = {name: assembler.load_prompt(name).body
                 for name in ("gpu.md", "mode.experiment.md", "mode.independent_verification.md")}
        self.assertLessEqual(architecture.ARCHITECTURE_PATH.with_name("gpu.md").read_text(encoding="utf-8").count("\n"), 60)
        for name, text in texts.items():
            low = text.lower()
            for phrase in LOOSENING_PHRASES:
                self.assertNotIn(phrase, low, (name, phrase))
            self.assertEqual(improvement.loosening_hits(text), [], name)
        self.assertTrue(texts["gpu.md"].startswith("# GPU compute\n"))
        for needle in ("gpu_run", "gpu_collect", "gpu_status", "gpu-replay", "gpu-replicate", "adv-gpu-run",
                       "never poll or sleep", "`timeout`"):
            self.assertIn(needle, texts["gpu.md"], needle)
        self.assertIn("gpu_run", texts["mode.experiment.md"])
        self.assertIn("gpu-replicate", texts["mode.independent_verification.md"])

    def test_every_backticked_repo_path_exists(self):
        pattern = re.compile(r"`((?:%s)/[^`\s]+)`" % "|".join(PATH_PREFIXES))
        for match in pattern.finditer(BODY):
            path = match.group(1).split(":", 1)[0].rstrip("/")
            if "*" in path:
                self.assertTrue(list(REPO_ROOT.glob(path)), path)
            else:
                self.assertTrue((REPO_ROOT / path).exists(), path)

    def test_fenced_json_examples_validate_against_the_model_schema(self):
        fence = re.compile(r"```json ([a-z_]+)/([a-z_]+)\n(.*?)\n```", re.S)
        for role, mode, text in fence.findall(BODY):
            self.assertEqual(schema.ROLES_BY_MODE.get(mode), role, (role, mode))
            problems = jsonschema_lite.validate(json.loads(text), schema.load_cached(role, mode, "5.0"))
            self.assertEqual(problems, [], (role, mode))


class GeneratedBlockTests(unittest.TestCase):
    def test_shipped_file_has_no_drift(self):
        self.assertEqual(architecture.check(), [])
        self.assertEqual(architecture.main(["--check"]), 0)

    def test_every_block_renders_into_its_chapter(self):
        chapters = assembler.parse_chapters(BODY)
        for block_id, chapter_id in architecture.BLOCK_CHAPTERS.items():
            text = architecture.render(block_id)
            self.assertTrue(text.strip(), block_id)
            self.assertIn(f"<!-- generated: {block_id} -->", chapters[chapter_id], block_id)
        with self.assertRaises(ValueError):
            architecture.render("no.such.block")
        self.assertEqual(set(architecture.blocks_for("gpu")), {"gpu.tools", "gpu.states", "gpu.fields", "gpu.limits"})
        self.assertTrue({"tools.gpu_run", "tools.gpu_collect", "tools.gpu_status", "tools.run_in_sandbox"}
                        <= set(architecture.blocks_for("tools")))

    def test_directive_fields_are_literals_in_the_policy_or_engine(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in architecture.DIRECTIVE_SOURCES)
        rendered = architecture.render("session.directive_fields")
        for key in architecture.DIRECTIVE_FIELDS:
            self.assertIn(f'"{key}"', source, key)
            self.assertIn(f"| `{key}` |", rendered, key)
        for key in ("directive_id", "instructions", "forbidden_strategy_fingerprints", "review_context", "basins"):
            self.assertIn(f"| `{key}` |", rendered)

    def test_role_field_tables_cover_every_allowed_key(self):
        for role in set(schema.ROLES_BY_MODE.values()):
            rendered = architecture.render(f"roles.{role}.fields")
            for mode, owner in schema.ROLES_BY_MODE.items():
                if owner == role:
                    for key in schema.allowed_model_keys(role, mode, "5.0"):
                        self.assertIn(f"| `{key}` |", rendered, (role, key))
        with patch.dict(architecture.FIELD_MEANINGS, {}, clear=True):
            with self.assertRaises(ValueError):
                architecture.render("roles.researcher.fields")

    def test_endings_and_constants_follow_the_code(self):
        endings = architecture.render("session.endings")
        for ending in SESSION_ENDINGS:
            self.assertIn(f"| `{ending}` |", endings)
        self.assertIn("error_prompt_too_large", SESSION_ENDINGS)
        SessionResult(session_id="s", ended="error_prompt_too_large")
        constants = architecture.render("kernel.thinking.constants")
        for name in ("STRATEGY_DIMENSIONS", "LADDER_CYCLE", "FORMALIZATION_RANKS", "SURGEON_FAULT_THRESHOLD"):
            self.assertIn(f"`policy.{name}`", constants)
        lists = architecture.render("improvement.lists")
        for name in improvement.ANTI_PATTERNS + improvement.KILL_TEST_KINDS + schema.OVERLAY_OPS:
            self.assertIn(f"`{name}`", lists)
        self.assertIn("`task_created`", architecture.render("kernel.events.types"))
        self.assertIn("| `integrity` |", architecture.render("kernel.events.state_keys"))

    def test_check_reports_drift_and_write_repairs_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "ARCHITECTURE.md"
            shutil.copy(architecture.ARCHITECTURE_PATH, copy)
            text = copy.read_text(encoding="utf-8")
            marker = "<!-- generated: session.endings -->\n"
            copy.write_text(text.replace(marker, marker + "| stray | row |\n", 1), encoding="utf-8")
            self.assertEqual(architecture.check(copy), ["session.endings: drift"])
            self.assertEqual(architecture.write(copy), ["session.endings"])
            self.assertEqual(architecture.check(copy), [])
            self.assertEqual(copy.read_bytes(), RAW)
            copy.write_text(text.replace("<!-- generated: gpu.limits -->\n", "", 1)
                            .replace("<!-- /generated: gpu.limits -->\n", "", 1), encoding="utf-8")
            self.assertIn("gpu.limits: missing from ARCHITECTURE.md", architecture.check(copy))
            with self.assertRaises(ValueError):
                architecture.write(copy)

    def test_assemble_drafts_replaces_prose_and_keeps_generated_regions(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "ARCHITECTURE.md"
            shutil.copy(architecture.ARCHITECTURE_PATH, copy)
            drafts = Path(tmp) / "drafts"
            drafts.mkdir()
            (drafts / "glossary.md").write_text("## Glossary\n\n- `basin`: an approach family.\n", encoding="utf-8")
            (drafts / "tools.md").write_text("The tools, in prose.\n", encoding="utf-8")
            self.assertEqual(architecture.assemble_drafts(drafts, copy), ["tools", "glossary"])
            chapters = assembler.parse_chapters(assembler.load_prompt("ARCHITECTURE.md", root=Path(tmp)).body)
            self.assertEqual(chapters["glossary"], "## Glossary\n\n- `basin`: an approach family.")
            self.assertTrue(chapters["tools"].startswith("## Tools\n\nThe tools, in prose."))
            for block_id in architecture.blocks_for("tools"):
                self.assertIn(f"<!-- generated: {block_id} -->\n" + architecture.render(block_id), chapters["tools"])
            self.assertEqual(architecture.check(copy), [])
            self.assertEqual(list(chapters), list(assembler.CHAPTER_IDS))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / ".harness" / "architecture"

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o644)
        self.tmp.cleanup()

    def test_export_writes_every_chapter_the_file_and_a_manifest_read_only(self):
        files = architecture.export(self.target)
        names = sorted(p.name for p in self.target.iterdir())
        self.assertEqual(names, sorted([f"{c}.md" for c in assembler.CHAPTER_IDS] + ["ARCHITECTURE.md", "manifest.json"]))
        self.assertEqual(set(files), set(names) - {"manifest.json"})
        manifest = json.loads((self.target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest, {"architecture_hash": assembler.prompt_hash("ARCHITECTURE.md"), "files": files})
        self.assertEqual((self.target / "ARCHITECTURE.md").read_bytes(), RAW)
        self.assertNotIn("<!-- generated:", (self.target / "tools.md").read_text(encoding="utf-8"))
        for name in names:
            self.assertEqual(stat.S_IMODE((self.target / name).stat().st_mode), 0o444, name)

    def test_ensure_is_a_noop_when_intact_and_repairs_tampering(self):
        self.assertTrue(architecture.ensure(self.root))
        self.assertFalse(architecture.ensure(self.root))
        victim = self.target / "session.md"
        os.chmod(victim, 0o644)
        victim.write_text("tampered\n", encoding="utf-8")
        self.assertTrue(architecture.ensure(self.root))
        self.assertNotEqual(victim.read_text(encoding="utf-8"), "tampered\n")
        self.assertFalse(architecture.ensure(self.root))

    def test_ensure_rewrites_everything_when_the_shipped_hash_moves(self):
        architecture.export(self.target)
        (self.target / "glossary.md").unlink()
        manifest_path = self.target / "manifest.json"
        os.chmod(manifest_path, 0o644)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["architecture_hash"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = {p.name: p.stat().st_ino for p in self.target.iterdir()}
        self.assertTrue(architecture.ensure(self.root))
        after = {p.name: p.stat().st_ino for p in self.target.iterdir()}
        self.assertIn("glossary.md", after)
        for name, inode in before.items():
            self.assertNotEqual(after[name], inode, name)
        self.assertTrue(architecture.intact(self.target))


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "payload").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def sections(self, prompt):
        return prompt.system_text.split(assembler.SEPARATOR)

    def test_reference_sits_in_slot_two_with_manifest_fields(self):
        prompt = assembler.assemble(directive("researcher", "experiment"), workspace=self.ws)
        sections = self.sections(prompt)
        self.assertTrue(sections[0].startswith("# The harness contract"))
        self.assertTrue(sections[1].startswith(assembler.SYSTEM_REFERENCE_HEADING + "\n"))
        self.assertTrue(sections[2].startswith("# Researcher: experiment"))
        expected, chapters = assembler.system_reference("researcher", "experiment", tier="role", gpu=False)
        self.assertEqual(sections[1], expected)
        manifest = prompt.manifest
        self.assertEqual(manifest["architecture_hash"], assembler.prompt_hash("ARCHITECTURE.md"))
        self.assertEqual(manifest["architecture_tier"], "role")
        self.assertEqual(manifest["architecture_chapters"], chapters)
        self.assertEqual(manifest["architecture_bytes"], len(expected.encode("utf-8")))
        self.assertFalse(manifest["gpu"])
        self.assertIn("ARCHITECTURE.md", manifest["files"])
        self.assertNotIn("gpu.md", manifest["files"])
        self.assertEqual(assembler.actor_provenance(manifest)["architecture_hash"], manifest["architecture_hash"])

    def test_tier_argument_sizes_the_reference(self):
        core = assembler.assemble(directive("researcher", "experiment"), architecture_tier="core")
        self.assertEqual(core.manifest["architecture_chapters"], CORE)
        lean = assembler.assemble(directive("researcher", "experiment"), architecture_tier="lean")
        self.assertEqual(lean.manifest["architecture_chapters"], CORE + ["evidence", "roles.researcher"])
        self.assertLess(core.manifest["architecture_bytes"], lean.manifest["architecture_bytes"])
        with self.assertRaises(ValueError):
            assembler.assemble(directive("researcher", "experiment"), architecture_tier="huge")
        self.assertEqual(ARCHITECTURE_TIERS, assembler.ARCHITECTURE_TIERS)

    def test_gpu_guide_rides_only_with_a_workspace_grant(self):
        (self.ws / "loop-config.json").write_text(json.dumps({"harness": {"gpu": {"enabled": True}}}), encoding="utf-8")
        self.assertTrue(assembler.workspace_gpu_enabled(self.ws))
        granted = assembler.assemble(directive("researcher", "experiment"), workspace=self.ws)
        self.assertIn("gpu.md", granted.manifest["files"])
        self.assertTrue(granted.manifest["gpu"])
        self.assertIn("gpu", granted.manifest["architecture_chapters"])
        sections = self.sections(granted)
        self.assertTrue(any(s.startswith("# GPU compute") for s in sections))
        self.assertLess([i for i, s in enumerate(sections) if s.startswith("# GPU compute")][0],
                        [i for i, s in enumerate(sections) if s.startswith("# Shape template")][0])
        (self.ws / "loop-config.json").write_text(json.dumps({"harness": {"gpu": {"enabled": False}}}), encoding="utf-8")
        plain = assembler.assemble(directive("researcher", "experiment"), workspace=self.ws)
        self.assertNotIn("gpu.md", plain.manifest["files"])
        self.assertNotIn("gpu", plain.manifest["architecture_chapters"])
        self.assertNotIn("# GPU compute", plain.system_text)
        forced = assembler.assemble(directive("explorer", "ideation"), workspace=self.ws, gpu=True)
        self.assertIn("gpu.md", forced.manifest["files"])
        self.assertNotIn("gpu", forced.manifest["architecture_chapters"], "the explorer runs no jobs")

    def test_refine_prompt_carries_the_refine_reference(self):
        prompt = assembler.assemble_refine("proposer", {"refinement_id": "R1"}, {"type": "object"})
        sections = self.sections(prompt)
        self.assertTrue(sections[0].startswith("# The harness contract"))
        self.assertTrue(sections[1].startswith(assembler.SYSTEM_REFERENCE_HEADING + "\n"))
        self.assertEqual(sections[2], assembler.load_prompt("IMPROVEMENT.md").body)
        self.assertEqual(sections[3].rstrip("\n"), assembler.load_prompt("proposer.md").body)
        self.assertEqual(prompt.manifest["architecture_chapters"], CORE + ["improvement", "roles.refine"])
        self.assertEqual(prompt.manifest["architecture_tier"], "role")
        self.assertEqual(assembler.assemble_refine("proposer", {}, {}, architecture_tier="core").manifest["architecture_chapters"], CORE)

    def test_model_spec_validates_the_tier_and_defaults_ship_four_aliases(self):
        with self.assertRaises(ValueError):
            ModelSpec(backend="http_chat", model="m", architecture_tier="huge")
        self.assertEqual(ModelSpec(backend="http_chat", model="m").architecture_tier, "role")
        models = routing.load_defaults()["models"]
        self.assertEqual(sorted(models), ["astra", "deepseek", "deepseek-flash", "grok"])
        self.assertEqual(models["grok"]["architecture_tier"], "role")
        self.assertEqual(models["deepseek"]["architecture_tier"], "role")
        self.assertEqual(models["deepseek-flash"]["architecture_tier"], "lean")
        self.assertEqual(models["astra"]["architecture_tier"], "role")
        for alias in ("astra", "grok", "deepseek", "deepseek-flash"):
            self.assertIsInstance(models[alias]["context_window"], int, alias)
            self.assertIn("context_window_note", models[alias], alias)

    def test_immutable_prefixes_cover_the_reference_and_gpu_surfaces(self):
        for prefix in ("harness/gpu", "harness/prompts/gpu.md", "harness/prompts/ARCHITECTURE.md", "harness/architecture.py"):
            self.assertIn(prefix, improvement.IMMUTABLE_PREFIXES)

    def test_digest_ignores_reads_of_the_exported_reference(self):
        def session(name, entries):
            target = self.ws / "transcripts" / "sessions" / name
            target.mkdir(parents=True)
            (target / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
            return target
        oriented = session("a", [
            {"tool": "read_file", "input": {"path": ".harness/architecture/tools.md"}},
            {"tool": "Read", "input": {"file_path": str(self.ws / ".harness" / "architecture" / "gpu.md")}},
            {"tool": "run_in_sandbox", "input": {"command": ["true"]}},
            {"tool": "read_file", "input": {"path": "payload/notes.md"}},
        ])
        self.assertFalse(transcripts.digest(oriented)["retrieval_before_attempt"])
        retrieved = session("b", [
            {"tool": "read_file", "input": {"path": "payload/notes.md"}},
            {"tool": "gpu_run", "input": {"command": ["python3", "x.py"], "outputs": ["payload/scratch/out"]}},
        ])
        self.assertTrue(transcripts.digest(retrieved)["retrieval_before_attempt"])
        self.assertIn("gpu_run", transcripts.EXECUTION_TOOLS)
        self.assertIn("gpu_collect", transcripts.EXECUTION_TOOLS)


if __name__ == "__main__":
    unittest.main()
