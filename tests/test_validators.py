"""Capability C: validator hooks run driver-style and return attested verdicts.

Every checker here is a python3 -c stub; a rejection verdict is a successful
observation, and only contract violations fail the run.
"""

import hashlib
import json
import unittest

from src.adv_loop.errors import ValidationError
from src.adv_loop.validators import validate_report

try:
    from test_engine import Harness
except ImportError:  # invoked as tests.test_validators rather than via discovery
    from tests.test_engine import Harness


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


VERDICT_SCRIPT = (
    "import hashlib, json, sys\n"
    "env = json.load(sys.stdin)\n"
    "assert env['protocol'] == 'adv-loop-validate/1'\n"
    "assert env['payload_root'].endswith('payload')\n"
    "accepted = (env.get('input') or {}).get('accepted', True)\n"
    "print(json.dumps({\n"
    "  'accepted': accepted,\n"
    "  'checker_id': 'stub-checker',\n"
    "  'checker_version': '1.0',\n"
    "  'artifact_hash': hashlib.sha256(b'artifact').hexdigest(),\n"
    "  'log_hash': hashlib.sha256(b'log').hexdigest(),\n"
    "  'details': {'cases': 3},\n"
    "}))\n"
)


def write_config(harness, config):
    (harness.engine.workspace / "loop-config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )


class ValidateTests(unittest.TestCase):
    def test_registered_hook_runs_and_returns_an_evidence_hint(self):
        harness = Harness(self)
        write_config(harness, {"validators": {
            "spec-check": {"command": ["python3", "-c", VERDICT_SCRIPT], "rank": "executable_spec"},
        }})
        report = validate_report(harness.engine.workspace, "spec-check")
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["ok"])
        self.assertTrue(report["result"]["accepted"])
        hint = report["evidence_hint"]
        self.assertEqual(hint["fingerprint"], digest("artifact"))
        self.assertEqual(hint["fingerprint"], report["result"]["artifact_hash"])
        self.assertEqual(hint["quality"], "direct")
        self.assertEqual(hint["formalization_rank"], "executable_spec")
        self.assertEqual(hint["checker"]["checker_id"], "stub-checker")
        self.assertNotIn("details", hint["checker"])

    def test_a_rejection_verdict_is_still_a_successful_run(self):
        harness = Harness(self)
        write_config(harness, {"validators": {
            "spec-check": {"command": ["python3", "-c", VERDICT_SCRIPT], "rank": "executable_spec"},
        }})
        report = validate_report(harness.engine.workspace, "spec-check",
                                 input_value={"accepted": False})
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["result"]["accepted"])
        # A rejected artifact earns no rank hint: it cannot satisfy a rank gate.
        self.assertNotIn("formalization_rank", report["evidence_hint"])

    def test_malformed_verdicts_and_dead_hooks_are_structured_failures(self):
        harness = Harness(self)
        write_config(harness, {"validators": {
            "empty": {"command": ["python3", "-c", "print('{}')"]},
            "bad-hex": {"command": ["python3", "-c",
                        "import json; print(json.dumps({'accepted': True, 'checker_id': 'c',"
                        " 'checker_version': '1', 'artifact_hash': 'zz', 'log_hash': 'zz'}))"]},
            "not-json": {"command": ["python3", "-c", "print('nope')"]},
            "dies": {"command": ["python3", "-c", "import sys; sys.exit(3)"]},
        }})
        for name in ("empty", "bad-hex", "not-json", "dies"):
            report = validate_report(harness.engine.workspace, name)
            self.assertEqual(report["exit_code"], 1, name)
            self.assertFalse(report["ok"], name)
            self.assertIn("failure", report, name)

    def test_an_unregistered_hook_is_a_validation_error(self):
        harness = Harness(self)
        with self.assertRaises(ValidationError):
            validate_report(harness.engine.workspace, "nonexistent")

    def test_in_sandbox_prepends_the_declared_sandbox_command(self):
        harness = Harness(self)
        write_config(harness, {
            "sandbox": {"kind": "uv", "command": ["python3"]},
            "validators": {"wrapped": {"command": ["-c", VERDICT_SCRIPT], "in_sandbox": True}},
        })
        report = validate_report(harness.engine.workspace, "wrapped")
        self.assertEqual(report["exit_code"], 0)
        write_config(harness, {
            "validators": {"wrapped": {"command": ["-c", VERDICT_SCRIPT], "in_sandbox": True}},
        })
        with self.assertRaisesRegex(ValidationError, "in_sandbox"):
            validate_report(harness.engine.workspace, "wrapped")

    def test_hook_timeouts_are_bounded(self):
        harness = Harness(self)
        write_config(harness, {"validators": {
            "sleepy": {"command": ["python3", "-c", "import time; time.sleep(5)"],
                       "timeout_seconds": 1},
        }})
        report = validate_report(harness.engine.workspace, "sleepy", timeout=0.5)
        self.assertEqual(report["exit_code"], 1)
        self.assertIn("failure", report)


if __name__ == "__main__":
    unittest.main()
