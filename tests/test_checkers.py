"""Phase C: shipped checkers speak the validate contract honestly.

The generic command checker is exercised for real (python3 is its tool); the
Lean and z3 checkers run only where their toolchains exist — a missing tool is
a skipped test here and a hook *failure* in production, never a fake verdict.
"""

import hashlib
import json
import shutil
import unittest
from pathlib import Path

from src.adv_loop.validators import validate_report

try:
    from test_engine import Harness
except ImportError:  # invoked as tests.test_checkers rather than via discovery
    from tests.test_engine import Harness

REPO_ROOT = Path(__file__).resolve().parent.parent


def tool_runs(executable: str) -> bool:
    """A shim on PATH is not a working toolchain; probe --version for real."""

    import subprocess
    if not shutil.which(executable):
        return False
    probe = subprocess.run([executable, "--version"], capture_output=True, check=False)
    return probe.returncode == 0


def register(workspace: Path, name: str, script: str, rank: str) -> None:
    (workspace / "loop-config.json").write_text(json.dumps({
        "validators": {name: {
            "command": ["python3", str(REPO_ROOT / "checkers" / script)],
            "rank": rank,
        }},
    }), encoding="utf-8")


class CommandCheckerTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(self)
        register(self.harness.engine.workspace, "suite", "command_checker.py", "executable_spec")

    def test_passing_command_yields_an_accepting_artifact_bound_verdict(self):
        workspace = self.harness.engine.workspace
        (workspace / "payload").mkdir()
        artifact = workspace / "payload" / "result.txt"
        artifact.write_text("the invariant held on 100 cases\n", encoding="utf-8")
        report = validate_report(workspace, "suite", input_value={
            "command": ["python3", "-c", "print('ok')"],
            "artifact": "payload/result.txt",
        })
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["result"]["accepted"])
        self.assertEqual(report["result"]["artifact_hash"],
                         hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual(report["evidence_hint"]["formalization_rank"], "executable_spec")

    def test_failing_command_is_a_rejection_verdict_not_a_hook_failure(self):
        report = validate_report(self.harness.engine.workspace, "suite", input_value={
            "command": ["python3", "-c", "import sys; sys.exit(1)"],
        })
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["result"]["accepted"])
        self.assertNotIn("formalization_rank", report["evidence_hint"])

    def test_escaping_paths_and_missing_artifacts_fail_the_hook(self):
        for bad_input in (
            {"command": ["python3", "-c", "pass"], "artifact": "../outside.txt"},
            {"command": ["python3", "-c", "pass"], "artifact": "payload/missing.txt"},
            {"command": ["python3", "-c", "pass"], "cwd": "../.."},
            {"command": "not-an-argv"},
        ):
            report = validate_report(self.harness.engine.workspace, "suite", input_value=bad_input)
            self.assertEqual(report["exit_code"], 1, bad_input)
            self.assertIn("failure", report)


@unittest.skipUnless(tool_runs("lean"), "no working Lean toolchain")
class LeanKernelIntegrationTests(unittest.TestCase):
    def test_a_trivially_true_statement_is_accepted(self):
        harness = Harness(self)
        workspace = harness.engine.workspace
        register(workspace, "lean", "lean_kernel.py", "kernel_proof")
        (workspace / "sandbox").mkdir()
        scratch = workspace / "payload" / "scratch"
        scratch.mkdir(parents=True)
        (scratch / "Trivial.lean").write_text(
            "theorem trivial_true : True := True.intro\n", encoding="utf-8"
        )
        report = validate_report(workspace, "lean", input_value={
            "file": "payload/scratch/Trivial.lean", "cwd": "sandbox",
        })
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["result"]["accepted"])


@unittest.skipUnless(tool_runs("z3"), "no working z3 solver")
class Z3IntegrationTests(unittest.TestCase):
    def test_an_unsat_goal_is_accepted_and_a_sat_goal_is_not(self):
        harness = Harness(self)
        workspace = harness.engine.workspace
        register(workspace, "smt", "z3_discharge.py", "smt_discharge")
        scratch = workspace / "payload" / "scratch"
        scratch.mkdir(parents=True)
        (scratch / "goal.smt2").write_text(
            "(declare-const x Int)\n(assert (and (> x 0) (< x 0)))\n(check-sat)\n",
            encoding="utf-8",
        )
        report = validate_report(workspace, "smt", input_value={"file": "payload/scratch/goal.smt2"})
        self.assertTrue(report["result"]["accepted"])
        (scratch / "sat.smt2").write_text(
            "(declare-const x Int)\n(assert (> x 0))\n(check-sat)\n", encoding="utf-8"
        )
        report = validate_report(workspace, "smt", input_value={"file": "payload/scratch/sat.smt2"})
        self.assertFalse(report["result"]["accepted"])


if __name__ == "__main__":
    unittest.main()
