"""Phase F: an imagined checker verdict cannot enter the chain, and the log
survives its machine.

Attestations are workspace-keyed HMACs: the validate runner signs what a
checker actually printed, and the engine refuses unsigned or forged records
when the workspace requires them. Mirrors are append-only copies whose
divergence is refused loudly — never repaired silently.
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.adv_loop.cli import build_parser, dispatch
from src.adv_loop.engine import CHECKER_KEY_FILE, LoopEngine, checker_attestation_mac
from src.adv_loop.errors import LoopError, ValidationError
from src.adv_loop.mirror import mirror_report
from src.adv_loop.runner import fleet_report
from src.adv_loop.validators import validate_report

try:
    from test_engine import Harness, digest
except ImportError:  # invoked as tests.test_trust rather than via discovery
    from tests.test_engine import Harness, digest

VERDICT_SCRIPT = (
    "import hashlib, json, sys\n"
    "json.load(sys.stdin)\n"
    "print(json.dumps({'accepted': True, 'checker_id': 'trusted-checker',"
    " 'checker_version': '1.0',"
    " 'artifact_hash': hashlib.sha256(b'artifact').hexdigest(),"
    " 'log_hash': hashlib.sha256(b'log').hexdigest()}))"
)


def keygen(workspace: Path) -> str:
    parser = build_parser()
    result = dispatch(parser.parse_args(["keygen", str(workspace)]))
    assert result["created"]
    return (workspace / CHECKER_KEY_FILE).read_text(encoding="utf-8").strip()


def signed_checker(key_hex, accepted=True, label="artifact"):
    core = {
        "accepted": accepted,
        "artifact_hash": digest(label),
        "checker_id": "trusted-checker",
        "checker_version": "1.0",
        "log_hash": digest(f"{label}-log"),
    }
    return {**core, "attestation": checker_attestation_mac(key_hex, core)}


class KeygenAndSigningTests(unittest.TestCase):
    def test_keygen_creates_a_private_key_once(self):
        harness = Harness(self)
        key_hex = keygen(harness.engine.workspace)
        self.assertEqual(len(key_hex), 64)
        mode = (harness.engine.workspace / CHECKER_KEY_FILE).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        with self.assertRaises(LoopError):
            keygen(harness.engine.workspace)

    def test_validate_signs_verdicts_with_the_workspace_key(self):
        harness = Harness(self)
        key_hex = keygen(harness.engine.workspace)
        (harness.engine.workspace / "loop-config.json").write_text(json.dumps({
            "validators": {"trusted": {"command": ["python3", "-c", VERDICT_SCRIPT]}},
        }), encoding="utf-8")
        report = validate_report(harness.engine.workspace, "trusted")
        verdict = report["result"]
        self.assertEqual(
            verdict["attestation"],
            checker_attestation_mac(key_hex, {k: v for k, v in verdict.items()
                                              if k != "attestation"}),
        )
        self.assertIn("attestation", report["evidence_hint"]["checker"])


class AttestationEnforcementTests(unittest.TestCase):
    def enforcing_harness(self):
        harness = Harness(self)
        key_hex = keygen(harness.engine.workspace)
        (harness.engine.workspace / "loop-config.json").write_text(json.dumps({
            "checker_attestation": {"require": True},
        }), encoding="utf-8")
        harness.plan()
        return harness, key_hex

    def test_unsigned_and_forged_checker_records_are_refused(self):
        harness, key_hex = self.enforcing_harness()
        unsigned = signed_checker(key_hex)
        unsigned.pop("attestation")
        with self.assertRaisesRegex(ValidationError, "attestation is missing or invalid"):
            harness.research("unsigned", extra_evidence=[
                harness.evidence("e1", ["C1"], checker=unsigned),
            ])
        forged = signed_checker(key_hex)
        forged["attestation"] = digest("forged")
        with self.assertRaisesRegex(ValidationError, "attestation is missing or invalid"):
            harness.research("forged", extra_evidence=[
                harness.evidence("e2", ["C1"], checker=forged),
            ])
        state = harness.research("signed", extra_evidence=[
            harness.evidence("e3", ["C1"], rank="kernel_proof",
                             checker=signed_checker(key_hex)),
        ])
        recorded = state["evidence"]["E000001"]["checker"]
        self.assertIn("attestation", recorded)
        review = harness.engine.next_instruction()["review_context"]
        self.assertEqual(review["target_attempt"]["evidence"][0]["checker"], recorded)
        self.assertTrue(harness.engine.audit()["ok"])

    def test_requirement_without_a_key_is_a_hard_stop(self):
        harness = Harness(self)
        (harness.engine.workspace / "loop-config.json").write_text(json.dumps({
            "checker_attestation": {"require": True},
        }), encoding="utf-8")
        harness.plan()
        with self.assertRaisesRegex(ValidationError, "no attestation key"):
            harness.research("keyless", extra_evidence=[
                harness.evidence("e1", ["C1"], checker={
                    "checker_id": "chk", "checker_version": "1.0", "accepted": True,
                    "artifact_hash": digest("a"),
                }),
            ])

    def test_without_the_requirement_unsigned_records_still_pass(self):
        harness = Harness(self)
        harness.plan()
        state = harness.research("plain", extra_evidence=[
            harness.evidence("e1", ["C1"], checker={
                "checker_id": "chk", "checker_version": "1.0", "accepted": True,
                "artifact_hash": digest("a"),
            }),
        ])
        self.assertNotIn("attestation", state["evidence"]["E000001"]["checker"])

    def test_attestation_field_is_v5_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = LoopEngine.create(Path(tmp), "A 4.0 task", ["Done"], {},
                                       task_id="t4", policy_version="4.0")
            harness = Harness(self)
            harness.engine = engine
            harness.plan()
            with self.assertRaisesRegex(ValidationError, "unknown fields"):
                harness.research("v4-signed", extra_evidence=[
                    harness.evidence("e1", ["C1"], checker={
                        "checker_id": "chk", "checker_version": "1.0", "accepted": True,
                        "artifact_hash": digest("a"), "attestation": digest("mac"),
                    }),
                ])


class MirrorTests(unittest.TestCase):
    def test_mirror_appends_tails_and_refuses_divergence(self):
        harness = Harness(self)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "mirrors"
            first = mirror_report(harness.engine.workspace, destination)
            self.assertTrue(first["ok"])
            self.assertGreater(first["appended_bytes"], 0)
            mirror_file = Path(first["mirror"])
            self.assertEqual(mirror_file.read_bytes(),
                             (harness.engine.workspace / "events.jsonl").read_bytes())

            harness.plan()
            second = mirror_report(harness.engine.workspace, destination)
            self.assertTrue(second["ok"])
            self.assertGreater(second["appended_bytes"], 0)
            self.assertEqual(mirror_file.read_bytes(),
                             (harness.engine.workspace / "events.jsonl").read_bytes())

            third = mirror_report(harness.engine.workspace, destination)
            self.assertEqual(third["appended_bytes"], 0)

            with open(mirror_file, "ab") as handle:
                handle.write(b"tampered\n")
            diverged = mirror_report(harness.engine.workspace, destination)
            self.assertFalse(diverged["ok"])
            self.assertTrue(diverged["diverged"])
            self.assertEqual(diverged["exit_code"], 2)

    def test_fleet_mirrors_per_pass_and_flags_divergence(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fleet"
            root.mkdir()
            destination = Path(tmp) / "mirrors"
            harness = Harness(self)
            workspace = root / "mirrored-task"
            shutil.copytree(harness.engine.workspace, workspace)
            (workspace / "loop-config.json").write_text(json.dumps({
                "mirror_to": str(destination),
            }), encoding="utf-8")
            report = fleet_report(root, dry_run=True)
            row = report["workspaces"][0]
            self.assertEqual(row["autonomy"][0]["kind"], "mirrored")
            mirror_file = destination / "mirrored-task.events.jsonl"
            self.assertTrue(mirror_file.is_file())

            with open(mirror_file, "ab") as handle:
                handle.write(b"tampered\n")
            report = fleet_report(root, dry_run=True)
            row = report["workspaces"][0]
            self.assertEqual(row["autonomy"][0]["kind"], "mirror_failed")
            self.assertEqual(report["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
