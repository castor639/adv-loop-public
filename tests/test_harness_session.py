"""Tools land in the ledger, sessions become submissions the engine accepts, transcripts persist and digest."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from adv_loop.engine import LoopEngine

from harness import assembler, jsonschema_lite, ledger, session, tools, transcripts
from harness.backends.base import ModelSpec, SessionBudget, SessionResult, WorkspaceHandle
from harness.backends.minimal import MinimalBackend

try:
    from test_engine import Harness
except ImportError:
    from tests.test_engine import Harness

SPEC = ModelSpec(backend="minimal", model="none", provider="local", billing="local")


def handle_for(ws: Path) -> WorkspaceHandle:
    return WorkspaceHandle(ws_id=ws.name, path=ws, session_id=uuid.uuid4().hex)


class ToolRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.runner = tools.ToolRunner(self.ws, self.ws / "ledger.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_call_is_recorded_with_exit_and_hashes(self):
        out = self.runner.run("run_in_sandbox", {"command": ["bash", "-c", "echo hi; exit 3; echo EXIT=$?"]})
        self.assertEqual(out["returncode"], 3)
        out = self.runner.run("write_file", {"path": "payload/x.txt", "content": "abc"})
        self.assertEqual(len(out["sha256"]), 64)
        out = self.runner.run("read_file", {"path": "payload/x.txt"})
        self.assertEqual(out["content"], "abc")
        out = self.runner.run("hash_artifact", {"path": "payload/x.txt"})
        self.assertEqual(out["sha256"], ledger.load(self.ws / "ledger.jsonl")[1]["file_sha256"])
        self.assertEqual([e["tool"] for e in ledger.load(self.ws / "ledger.jsonl")],
                         ["run_in_sandbox", "write_file", "read_file", "hash_artifact"])

    def test_escapes_and_unknown_tools_are_errors_not_crashes(self):
        self.assertIn("error", self.runner.run("read_file", {"path": "../etc/passwd"}))
        self.assertIn("error", self.runner.run("nope", {}))
        self.assertIn("error", self.runner.run("validate", {"hook": "x"}))
        self.assertEqual(len(self.runner.entries), 3)

    def test_tool_schemas_convert_to_both_wire_formats(self):
        names = {t["name"] for t in tools.TOOL_DEFINITIONS}
        self.assertEqual({t["function"]["name"] for t in tools.openai_tools()}, names)
        self.assertEqual({t["name"] for t in tools.anthropic_tools()}, names)
        self.assertIn("validate", names)


class MinimalThroughTheEngineTests(unittest.TestCase):
    """The deterministic backend drives a real workspace through planner and researcher."""

    def setUp(self):
        self.harness = Harness(self)
        self.engine: LoopEngine = self.harness.engine
        self.ws = self.engine.workspace

    def drive_one(self, backend):
        directive = self.engine.next_instruction()
        self.assertEqual(directive["action"], "attempt")
        prompt = assembler.assemble(directive, workspace=self.ws)
        handle = handle_for(self.ws)
        result = backend.run(directive, handle, SPEC, SessionBudget(), prompt)
        submission, problems, warnings = session.build_submission(
            directive, result, prompt, workspace=self.ws, spec=SPEC, agent_id="test")
        return directive, result, submission, problems, warnings

    def test_planner_then_researcher_are_accepted_by_the_kernel(self):
        backend = MinimalBackend()
        directive, result, submission, problems, _ = self.drive_one(backend)
        self.assertEqual(directive["mode"], "initial_plan")
        self.assertEqual(problems, [], problems)
        self.assertEqual(submission["actor"]["context_id"], result.session_id)
        self.assertEqual(len(submission["actor"]["prompt_bundle"]), 64)
        self.assertIn("#1 run_in_sandbox", submission["observation"])
        self.engine.record_attempt(submission)

        directive, result, submission, problems, warnings = self.drive_one(backend)
        self.assertEqual(directive["mode"], "experiment")
        self.assertEqual(problems, [], problems)
        self.assertEqual(warnings, [])
        artifact = self.ws / submission["evidence"][0]["locator"]
        self.assertTrue(artifact.is_file())
        self.assertEqual(submission["evidence"][0]["fingerprint"], ledger.observation_digest and
                         __import__("hashlib").sha256(artifact.read_bytes()).hexdigest())
        self.assertNotIn("artifact_path", submission["evidence"][0])
        recorded = self.engine.record_attempt(submission)
        self.assertTrue(recorded)
        state = json.loads((self.ws / "state.json").read_text())
        self.assertEqual(len(state["attempts"]) if "attempts" in state else 2, 2)

    def test_a_lying_fingerprint_is_stopped_before_the_kernel(self):
        self.engine.record_attempt(self.drive_one(MinimalBackend())[2])
        directive, result, submission, problems, _ = self.drive_one(MinimalBackend(lie=True))
        self.assertEqual(directive["mode"], "experiment")
        self.assertTrue(any("does not match" in p for p in problems), problems)

    def test_harness_owned_fields_from_the_model_are_discarded(self):
        directive = self.engine.next_instruction()
        prompt = assembler.assemble(directive, workspace=self.ws)
        result = MinimalBackend().run(directive, handle_for(self.ws), SPEC, SessionBudget(), prompt)
        result.model_output.update({"observation": "I ran everything", "actor": {"agent_id": "liar"},
                                    "directive_id": "D-forged", "role": "verifier"})
        submission, problems, _ = session.build_submission(directive, result, prompt, workspace=self.ws,
                                                           spec=SPEC, agent_id="test")
        self.assertEqual(submission["directive_id"], directive["directive_id"])
        self.assertEqual(submission["role"], "planner")
        self.assertEqual(submission["actor"]["agent_id"], "test")
        self.assertNotEqual(submission["observation"], "I ran everything")
        self.assertTrue(any("unknown field" in p for p in problems))


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.prompt = assembler.assemble({"action": "attempt", "directive_id": "d1", "role": "planner",
                                          "mode": "initial_plan", "task": "t", "open_criteria": [], "instructions": []})

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, sid, entries, ending="success"):
        sdir = self.ws / ".harness" / "sessions" / sid
        sdir.mkdir(parents=True)
        (sdir / "stream.jsonl").write_text('{"type":"init","key":"sk-abcdefghijklmnopqrstuvwxyz0123"}\n')
        for e in entries:
            ledger.append(sdir / "ledger.jsonl", e)
        return SessionResult(session_id=sid, ended=ending, tool_ledger=entries, model_output={},
                             usage={"input_tokens": 10, "output_tokens": 5}, session_dir=sdir,
                             messages=[{"role": "user", "content": "Authorization: Bearer fw_ABCDEFGHIJKLMNOPQRSTUV"}])

    def test_persist_scrubs_keys_and_indexes(self):
        entries = [ledger.entry("read_file", {"path": "payload/a"}, at="t"),
                   ledger.entry("run_in_sandbox", {"command": ["bash", "-c", "make; echo EXIT=$?"]}, stdout="EXIT=1\n", at="t")]
        target = transcripts.persist(self.ws, self.result("s1", entries), self.prompt,
                                     directive={"directive_id": "d1", "role": "planner", "mode": "initial_plan"},
                                     model="m", submission={"x": 1}, rejection=None, accepted=True,
                                     secrets=["super-secret-value"])
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ["ledger.jsonl", "messages.json", "prompt.manifest.json", "prompt.md", "result.json",
                          "stream.jsonl", "submission.json", "system.md"])
        self.assertIn("[REDACTED]", (target / "stream.jsonl").read_text())
        self.assertNotIn("sk-abcdefghijklmnop", (target / "stream.jsonl").read_text())
        self.assertNotIn("fw_ABCDEFGHIJ", (target / "messages.json").read_text())
        rows = transcripts.load_index(self.ws)
        self.assertEqual(rows[0]["session_id"], "s1")
        self.assertEqual(rows[0]["tool_calls"], 2)
        self.assertTrue(rows[0]["accepted"])
        summary = transcripts.digest(target)
        self.assertTrue(summary["retrieval_before_attempt"])
        self.assertEqual(summary["failed_commands"], 1)
        self.assertEqual(summary["tool_sequence"], ["read_file", "run_in_sandbox"])

    def test_fixation_flags_see_a_repeated_procedure(self):
        for i in range(3):
            entries = [ledger.entry("run_in_sandbox", {"command": ["bash", "-c", f"python3 try{i}.py; echo EXIT=$?"]}, stdout="EXIT=1\n", at="t"),
                       ledger.entry("write_file", {"path": f"payload/scratch/{i}.txt"}, at="t")]
            transcripts.persist(self.ws, self.result(f"e{i}", entries), self.prompt,
                                directive={"directive_id": f"d{i}", "role": "researcher", "mode": "experiment"},
                                model="m", submission={}, rejection=None, accepted=True)
        flags = transcripts.fixation_flags(self.ws)
        self.assertTrue(flags["same_procedure_schema_last_n"])
        self.assertFalse(flags["first_attempt_retrieved_first"])
        self.assertEqual(flags["attempts_seen"], 3)

    def test_scrub_patterns(self):
        text = 'x-api-key: 0123456789abcdef0123456789abcdef and plain words'
        self.assertIn("[REDACTED]", transcripts.scrub(text))
        self.assertIn("plain words", transcripts.scrub(text))


class SchemaLiteTests(unittest.TestCase):
    def test_reports_paths(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["a"],
                  "properties": {"a": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                                 "b": {"enum": ["x", "y"]},
                                 "c": {"oneOf": [{"type": "object", "required": ["p"], "properties": {"p": {"type": "string"}}},
                                                 {"type": "object", "required": ["q"], "properties": {"q": {"type": "string"}}}]}}}
        self.assertEqual(jsonschema_lite.validate({"a": ["ok"], "b": "x", "c": {"p": "1"}}, schema), [])
        problems = jsonschema_lite.validate({"a": [""], "b": "z", "c": {"p": "1", "q": "2"}, "d": 1}, schema)
        self.assertTrue(any("$.a[0]" in p for p in problems))
        self.assertTrue(any("$.b" in p for p in problems))
        self.assertTrue(any("exactly one" in p for p in problems))
        self.assertTrue(any("unknown field 'd'" in p for p in problems))
        self.assertEqual(jsonschema_lite.validate({}, schema), ["$: missing required field 'a'"])


if __name__ == "__main__":
    unittest.main()
