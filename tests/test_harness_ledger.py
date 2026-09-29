"""The observation comes from the ledger and fingerprints come from files, never from the model."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from harness import ledger


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LedgerEntryTests(unittest.TestCase):
    def test_exit_marker_is_parsed_from_stdout(self):
        record = ledger.entry("Bash", {"command": "make check; echo EXIT=$?"}, stdout="ok\nEXIT=3\n", at="t")
        self.assertEqual(record["exit_code"], 3)
        self.assertEqual(record["stdout_sha256"], sha(b"ok\nEXIT=3\n"))

    def test_digest_is_deterministic_and_carries_hashes(self):
        records = [
            ledger.entry("Bash", {"command": "ls"}, stdout="a\nb\n", tool_use_id="t1", at="t", duration_ms=12),
            ledger.entry("Write", {"file_path": "payload/out.txt"}, file_sha256="ab" * 32, tool_use_id="t2", at="t"),
        ]
        first = ledger.observation_digest(records)
        self.assertEqual(first, ledger.observation_digest(records))
        self.assertIn("#1 Bash [t1] (12 ms)", first)
        self.assertIn("$ ls", first)
        stdout_hash = sha(b"a\nb\n")[:12]
        self.assertIn(f"stdout[sha256:{stdout_hash}]", first)
        self.assertIn("file sha256: " + "ab" * 32, first)

    def test_digest_respects_the_limit_without_dropping_entries(self):
        records = [ledger.entry("Bash", {"command": f"cmd{i}"}, stdout="x" * 3000, at="t") for i in range(6)]
        text = ledger.observation_digest(records, limit=2000)
        self.assertLessEqual(len(text), 2000 + len("\n[truncated]"))
        self.assertIn("#1 Bash", text)
        self.assertIn("#6 Bash", text)

    def test_empty_ledger_says_so(self):
        self.assertEqual(ledger.observation_digest([]), "No tool calls were recorded in this session.")

    def test_round_trip_through_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger.append(path, ledger.entry("Bash", {"command": "true"}, at="t"))
            ledger.append(path, ledger.entry("Bash", {"command": "false"}, at="t"))
            self.assertEqual([r["input"]["command"] for r in ledger.load(path)], ["true", "false"])


class EvidenceVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "payload").mkdir()
        (self.ws / "payload" / "out.txt").write_bytes(b"hello\n")
        self.digest = sha(b"hello\n")
        self.base = {"ref": "e1", "kind": "artifact", "quality": "direct", "claim": "c", "method": "m",
                     "independence_key": "k", "supports": ["C1"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_artifact_path_is_hashed_by_the_harness(self):
        touched = [ledger.entry("Write", {"file_path": "payload/out.txt"}, at="t")]
        evidence, problems, warnings = ledger.verify_evidence(
            self.ws, [{**self.base, "artifact_path": "payload/out.txt"}], ledger=touched)
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])
        self.assertEqual(evidence[0]["fingerprint"], self.digest)
        self.assertEqual(evidence[0]["locator"], "payload/out.txt")
        self.assertNotIn("artifact_path", evidence[0])

    def test_unobserved_artifact_is_a_warning_not_a_rejection(self):
        evidence, problems, warnings = ledger.verify_evidence(
            self.ws, [{**self.base, "artifact_path": "payload/out.txt"}], ledger=[])
        self.assertEqual(problems, [])
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(warnings), 1)

    def test_a_lying_fingerprint_is_rejected(self):
        _, problems, _ = ledger.verify_evidence(
            self.ws, [{**self.base, "artifact_path": "payload/out.txt", "fingerprint": "0" * 64}])
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match", problems[0])

    def test_escapes_and_missing_files_are_rejected(self):
        _, problems, _ = ledger.verify_evidence(self.ws, [
            {**self.base, "artifact_path": "../etc/passwd"},
            {**self.base, "ref": "e2", "artifact_path": "payload/missing.txt"},
        ])
        self.assertEqual(len(problems), 2)

    def test_exactly_one_of_path_or_verdict(self):
        _, problems, _ = ledger.verify_evidence(self.ws, [
            {**self.base},
            {**self.base, "ref": "e2", "artifact_path": "payload/out.txt", "verdict_id": "V0001"},
        ])
        self.assertEqual(len(problems), 2)

    def test_verdict_id_copies_the_signed_checker_record(self):
        verdict = {"result": {"checker_id": "lean-kernel", "checker_version": "1.0", "accepted": True,
                              "artifact_hash": "a" * 64, "log_hash": "b" * 64, "toolchain_hash": "c" * 64,
                              "attestation": "d" * 64},
                   "evidence_hint": {"kind": "checker-attested", "locator": "validate:lean-kernel",
                                     "method": "adv-loop validate", "formalization_rank": "kernel_proof"}}
        evidence, problems, _ = ledger.verify_evidence(
            self.ws, [{**self.base, "verdict_id": "V0001"}], verdicts={"V0001": verdict})
        self.assertEqual(problems, [])
        self.assertEqual(evidence[0]["fingerprint"], "a" * 64)
        self.assertEqual(evidence[0]["checker"]["attestation"], "d" * 64)
        self.assertEqual(evidence[0]["formalization_rank"], "kernel_proof")
        self.assertEqual(evidence[0]["kind"], "artifact")

    def test_rejecting_verdict_loses_its_rank_and_keeps_the_checker_locator(self):
        verdict = {"result": {"checker_id": "cert-replay", "checker_version": "1.0", "accepted": False,
                              "artifact_hash": "a" * 64, "log_hash": "b" * 64, "attestation": "d" * 64},
                   "evidence_hint": {"kind": "checker-attested", "locator": "validate:cert-replay:command-checker@1.0",
                                     "method": "adv-loop validate", "formalization_rank": "executable_spec"}}
        evidence, problems, _ = ledger.verify_evidence(
            self.ws, [{**self.base, "verdict_id": "V0001", "locator": "my words", "formalization_rank": "executable_spec"}],
            verdicts={"V0001": verdict})
        self.assertEqual(problems, [])
        self.assertNotIn("formalization_rank", evidence[0])
        self.assertEqual(evidence[0]["locator"], "validate:cert-replay:command-checker@1.0")

    def test_verdict_from_another_session_is_rejected(self):
        _, problems, _ = ledger.verify_evidence(self.ws, [{**self.base, "verdict_id": "V0009"}], verdicts={})
        self.assertEqual(len(problems), 1)
        self.assertIn("not from this session", problems[0])


if __name__ == "__main__":
    unittest.main()
