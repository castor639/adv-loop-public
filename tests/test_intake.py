"""Capability A: intake sorts any drop into isolated workspaces, or refuses safely.

Drops are synthetic temp trees and hand-crafted archives; the classifier is
structural, so no fixture needs domain content, network access, or tooling.
"""

import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src.adv_loop.driver import SubprocessAdapter
from src.adv_loop.engine import LoopEngine, SAFE_ID
from src.adv_loop.errors import AdapterError, ValidationError
from src.adv_loop.intake import PARTITION_CRITERION, intake_report
from src.adv_loop.storage import object_hash


def make_tree(root: Path, files: dict) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def tree_snapshot(root: Path):
    snapshot = {}
    for base, dirs, files in os.walk(root):
        snapshot[str(Path(base))] = None
        for name in files:
            path = Path(base) / name
            snapshot[str(path)] = path.read_bytes()
    return snapshot


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_two_top_level_directories_partition_into_one_project_each(self):
        drop = make_tree(self.root / "drop", {
            "alpha/main.py": "print('a')",
            "alpha/data.csv": "1,2",
            "beta/notes.txt": "notes",
            "README.md": "# The drop",
        })
        report = intake_report(str(drop), root=self.root / "ws")
        proposal = report["proposal"]
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(proposal["classification"], "partitioned")
        self.assertEqual(len(proposal["candidates"]), 2)
        for candidate in proposal["candidates"]:
            self.assertIn("README.md", candidate["payload_paths"])
        names = [candidate["payload_paths"] for candidate in proposal["candidates"]]
        self.assertIn("alpha/main.py", names[0])
        self.assertIn("beta/notes.txt", names[1])
        self.assertFalse((self.root / "ws").exists())

    def test_a_single_wrapper_directory_is_stripped_before_partitioning(self):
        drop = make_tree(self.root / "drop", {
            "inner/alpha/a.py": "a",
            "inner/beta/b.py": "b",
        })
        report = intake_report(str(drop), root=self.root / "ws")
        proposal = report["proposal"]
        self.assertEqual(proposal["classification"], "partitioned")
        self.assertEqual(len(proposal["candidates"]), 2)
        self.assertTrue(any("wrapper directory stripped" in item for item in proposal["warnings"]))

    def test_mixed_loose_files_fall_back_to_one_partition_task(self):
        drop = make_tree(self.root / "drop", {
            "alpha/a.py": "a",
            "beta/b.py": "b",
            "loose_script.py": "print('x')",
        })
        proposal = intake_report(str(drop), root=self.root / "ws")["proposal"]
        self.assertEqual(proposal["classification"], "partition_task")
        self.assertEqual(len(proposal["candidates"]), 1)
        self.assertEqual(proposal["candidates"][0]["criteria"][0], PARTITION_CRITERION)

    def test_markdown_brief_yields_task_and_criteria_structurally(self):
        brief = self.root / "drop.md"
        brief.write_text(
            "# Reconcile the supplier feed\n\nSome context.\n\n"
            "## Acceptance criteria\n\n- The discrepancy is explained\n- The fix is verified twice\n",
            encoding="utf-8",
        )
        proposal = intake_report(str(brief), root=self.root / "ws")["proposal"]
        self.assertEqual(proposal["classification"], "single")
        candidate = proposal["candidates"][0]
        self.assertEqual(candidate["task"], "Reconcile the supplier feed")
        self.assertEqual(candidate["criteria"],
                         ["The discrepancy is explained", "The fix is verified twice"])

    def test_url_list_is_one_candidate_and_never_fetched(self):
        listing = self.root / "sources.txt"
        listing.write_text(
            "# reading list\nhttps://example.org/a\nhttp://example.org/b\n",
            encoding="utf-8",
        )
        with mock.patch("socket.socket", side_effect=AssertionError("intake must never fetch")):
            report = intake_report(str(listing), root=self.root / "ws")
        proposal = report["proposal"]
        self.assertEqual(proposal["source"]["source_kind"], "url_list")
        self.assertEqual(len(proposal["candidates"]), 1)
        self.assertIn("2 listed sources", proposal["candidates"][0]["task"])

    def test_task_ids_are_stable_across_reruns_and_filesystem_safe(self):
        drop = make_tree(self.root / "drop", {"alpha/a.py": "a", "beta/b.py": "b"})
        first = intake_report(str(drop), root=self.root / "ws")["proposal"]
        second = intake_report(str(drop), root=self.root / "ws")["proposal"]
        ids_first = [candidate["task_id"] for candidate in first["candidates"]]
        ids_second = [candidate["task_id"] for candidate in second["candidates"]]
        self.assertEqual(ids_first, ids_second)
        for task_id in ids_first:
            self.assertTrue(SAFE_ID.fullmatch(task_id))

    def test_manifest_hashes_every_file(self):
        drop = make_tree(self.root / "drop", {"alpha/a.py": "content-a", "b.md": "content-b"})
        manifest = intake_report(str(drop), root=self.root / "ws")["proposal"]["source"]
        self.assertEqual(manifest["file_count"], 2)
        for entry in manifest["entries"]:
            self.assertEqual(set(entry), {"path", "bytes", "sha256"})
            self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(manifest["manifest_hash"], object_hash(manifest["entries"]))

    def test_pin_files_suggest_a_sandbox_without_running_anything(self):
        drop = make_tree(self.root / "drop", {
            "alpha/uv.lock": "pinned-tool==1.0",
            "alpha/a.py": "a",
            "beta/b.txt": "b",
        })
        proposal = intake_report(str(drop), root=self.root / "ws")["proposal"]
        alpha = next(candidate for candidate in proposal["candidates"]
                     if "alpha/a.py" in candidate["payload_paths"])
        self.assertEqual(alpha["sandbox"], {"kind": "uv", "root": "sandbox"})
        self.assertEqual(alpha["profile"], "systems-software")
        beta = next(candidate for candidate in proposal["candidates"]
                    if "beta/b.txt" in candidate["payload_paths"])
        self.assertIsNone(beta["sandbox"])


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_zip_members_with_hostile_names_are_skipped_everywhere(self):
        archive_path = self.root / "drop.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("alpha/ok.txt", "fine")
            archive.writestr("beta/also.txt", "fine")
            archive.writestr("../evil.txt", "escape")
            archive.writestr("/abs.txt", "escape")
            link = zipfile.ZipInfo("alpha/link")
            link.external_attr = (0o120777 << 16)
            archive.writestr(link, "../../target")
        before = tree_snapshot(self.root)
        report = intake_report(str(archive_path), root=self.root / "ws", apply=True)
        proposal = report["proposal"]
        paths = {entry["path"] for entry in proposal["source"]["entries"]}
        self.assertEqual(paths, {"alpha/ok.txt", "beta/also.txt"})
        self.assertTrue(any("unsafe member name skipped" in item for item in proposal["warnings"]))
        self.assertTrue(any("symlink skipped" in item for item in proposal["warnings"]))
        self.assertFalse((self.root.parent / "evil.txt").exists())
        self.assertFalse(Path("/abs.txt").exists())
        after = tree_snapshot(self.root)
        new_paths = set(after) - set(before)
        self.assertTrue(all(str(self.root / "ws") in path for path in new_paths))

    def test_tar_link_members_and_traversal_names_are_skipped(self):
        archive_path = self.root / "drop.tar"
        with tarfile.open(archive_path, "w") as archive:
            data = b"fine"
            member = tarfile.TarInfo("alpha/ok.txt")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            member = tarfile.TarInfo("beta/ok.txt")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            sym = tarfile.TarInfo("alpha/escape")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "../../etc/hosts"
            archive.addfile(sym)
            traversal = tarfile.TarInfo("../evil.txt")
            traversal.size = len(data)
            archive.addfile(traversal, io.BytesIO(data))
        report = intake_report(str(archive_path), root=self.root / "ws", apply=True)
        proposal = report["proposal"]
        paths = {entry["path"] for entry in proposal["source"]["entries"]}
        self.assertEqual(paths, {"alpha/ok.txt", "beta/ok.txt"})
        self.assertTrue(any("link member skipped" in item for item in proposal["warnings"]))
        self.assertTrue(any("unsafe member name skipped" in item for item in proposal["warnings"]))
        for row in report["results"]:
            self.assertTrue(row["ok"])
            payload = Path(row["workspace"]) / "payload"
            landed = {str(p.relative_to(payload)) for p in payload.rglob("*") if p.is_file()}
            self.assertNotIn("escape", landed)

    def test_byte_and_file_caps_abort_the_intake(self):
        archive_path = self.root / "drop.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("alpha/big.bin", "x" * 4096)
        with self.assertRaises(ValidationError):
            intake_report(str(archive_path), root=self.root / "ws", max_bytes=100)
        drop = make_tree(self.root / "many", {f"f{i}.txt": "x" for i in range(5)})
        with self.assertRaises(ValidationError):
            intake_report(str(drop), root=self.root / "ws", max_files=3)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = make_tree(self.root / "drop", {
            "alpha/main.py": "print('a')",
            "alpha/pyproject.toml": "[project]\nname='alpha'",
            "beta/notes.txt": "notes",
            "README.md": "# The drop",
        })

    def test_apply_creates_isolated_workspaces_with_payload_and_manifest(self):
        report = intake_report(str(self.drop), root=self.root / "ws", apply=True)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(len(report["results"]), 2)
        heads = []
        for row in report["results"]:
            self.assertTrue(row["ok"])
            workspace = Path(row["workspace"])
            engine = LoopEngine(workspace)
            state = engine.load()
            self.assertEqual(state["policy_version"], "5.0")
            self.assertTrue(engine.audit()["ok"])
            heads.append(state["integrity"]["event_head"])
            manifest = json.loads((workspace / "payload" / ".intake-manifest.json").read_text())
            for entry in manifest["entries"]:
                copied = workspace / "payload" / entry["path"]
                self.assertEqual(copied.stat().st_size, entry["bytes"])
        self.assertNotEqual(heads[0], heads[1])
        alpha_row = next(row for row in report["results"] if "alpha" in row["task_id"])
        config = json.loads((Path(alpha_row["workspace"]) / "loop-config.json").read_text())
        self.assertEqual(config["sandbox"], {"kind": "uv", "root": "sandbox"})
        self.assertEqual(config["profile"], "systems-software")

    def test_recording_in_one_workspace_never_touches_the_other(self):
        report = intake_report(str(self.drop), root=self.root / "ws", apply=True)
        first, second = [Path(row["workspace"]) for row in report["results"]]
        untouched_before = tree_snapshot(second)
        engine = LoopEngine(first)
        directive = engine.next_instruction()
        self.assertEqual(directive["mode"], "initial_plan")
        self.assertEqual(untouched_before, tree_snapshot(second))
        other_state = LoopEngine(second).load()
        self.assertEqual(other_state["revision"], 1)

    def test_apply_is_per_candidate_isolated_on_collision(self):
        proposal = intake_report(str(self.drop), root=self.root / "ws")["proposal"]
        blocked_id = proposal["candidates"][0]["task_id"]
        (self.root / "ws").mkdir()
        (self.root / "ws" / blocked_id).mkdir()
        report = intake_report(str(self.drop), root=self.root / "ws", apply=True)
        self.assertEqual(report["exit_code"], 1)
        rows = {row["task_id"]: row for row in report["results"]}
        self.assertFalse(rows[blocked_id]["ok"])
        self.assertEqual(rows[blocked_id]["error"]["error"], "validation_error")
        other = next(row for row in report["results"] if row["task_id"] != blocked_id)
        self.assertTrue(other["ok"])
        self.assertTrue((Path(other["workspace"]) / "events.jsonl").is_file())

    def test_dry_run_writes_nothing_anywhere(self):
        before = tree_snapshot(self.root)
        intake_report(str(self.drop), root=self.root / "ws")
        self.assertEqual(before, tree_snapshot(self.root))

    def test_link_mode_hardlinks_directory_payloads(self):
        report = intake_report(str(self.drop), root=self.root / "ws", apply=True, link=True)
        self.assertEqual(report["exit_code"], 0)
        alpha_row = next(row for row in report["results"] if "alpha" in row["task_id"])
        copied = Path(alpha_row["workspace"]) / "payload" / "alpha" / "main.py"
        original = self.drop / "alpha" / "main.py"
        self.assertEqual(copied.stat().st_ino, original.stat().st_ino)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drop = make_tree(self.root / "drop", {"alpha/a.py": "a", "beta/b.py": "b"})

    def test_subprocess_adapter_receives_the_manifest_and_its_proposal_wins(self):
        script = (
            "import json,sys\n"
            "envelope = json.load(sys.stdin)\n"
            "assert envelope['protocol'] == 'adv-loop-intake/1'\n"
            "assert envelope['samples'], 'samples missing'\n"
            "proposal = envelope['heuristic_proposal']\n"
            "proposal['candidates'][0]['task'] = 'Adapter-refined task statement'\n"
            "print(json.dumps(proposal))\n"
        )
        adapter = SubprocessAdapter(["python3", "-c", script], timeout_seconds=60)
        report = intake_report(str(self.drop), root=self.root / "ws", adapter=adapter)
        proposal = report["proposal"]
        self.assertTrue(proposal["adapter_used"])
        self.assertEqual(proposal["candidates"][0]["task"], "Adapter-refined task statement")

    def test_adapter_proposal_violating_the_contract_is_refused(self):
        def bad_adapter(envelope):
            return {"classification": "single", "candidates": []}

        with self.assertRaises(AdapterError):
            intake_report(str(self.drop), root=self.root / "ws", adapter=bad_adapter)

        def uncovered_adapter(envelope):
            proposal = json.loads(json.dumps(envelope["heuristic_proposal"]))
            candidate = proposal["candidates"][0]
            candidate["payload_paths"] = [candidate["payload_paths"][0]]
            candidate["manifest"] = candidate["manifest"][:1]
            proposal["candidates"] = [candidate]
            return proposal

        with self.assertRaises(AdapterError):
            intake_report(str(self.drop), root=self.root / "ws", adapter=uncovered_adapter)
        self.assertFalse((self.root / "ws").exists())


if __name__ == "__main__":
    unittest.main()
