"""Capability A groundwork: payload/sandbox dirs are declared, contained, and inert.

Fixtures stay field-free. Setup commands are always python3 -c one-liners so
the suite needs no external tooling; pin files are plain text simulating a
lockfile, never a real environment.
"""

import hashlib
import json
import os
import unittest
from pathlib import Path

from src.adv_loop.errors import IntegrityError, ValidationError
from src.adv_loop.pathsafe import ensure_within, safe_relative
from src.adv_loop.sandbox import sandbox_report

try:
    from test_engine import Harness
except ImportError:  # invoked as tests.test_sandbox rather than via discovery
    from tests.test_engine import Harness


def tree_snapshot(root: Path, exclude: Path = None):
    """Every path under root (with file sizes), optionally ignoring one subtree."""

    snapshot = {}
    for base, dirs, files in os.walk(root):
        base_path = Path(base)
        if exclude is not None and (base_path == exclude or exclude in base_path.parents):
            continue
        snapshot[str(base_path)] = None
        for name in files:
            path = base_path / name
            snapshot[str(path)] = path.stat().st_size
    return snapshot


def write_config(harness, config):
    (harness.engine.workspace / "loop-config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )


class PathSafeTests(unittest.TestCase):
    def test_safe_relative_rejects_escapes_and_normalizes(self):
        self.assertEqual(str(safe_relative("a/./b.txt")), "a/b.txt")
        self.assertEqual(str(safe_relative("a\\b.txt")), "a/b.txt")
        for hostile in ("/etc/passwd", "../up", "a/../../up", "C:/windows", "", "  "):
            with self.assertRaises(ValidationError):
                safe_relative(hostile)

    def test_ensure_within_refuses_paths_outside_the_base(self):
        harness = Harness(self)
        base = harness.engine.workspace
        inside = ensure_within(base, base / "payload" / "file.txt")
        self.assertTrue(str(inside).startswith(str(base.resolve())))
        with self.assertRaises(ValidationError):
            ensure_within(base, base / ".." / "elsewhere")


class SandboxTests(unittest.TestCase):
    def test_undeclared_sandbox_init_creates_dirs_runs_nothing_and_is_idempotent(self):
        harness = Harness(self)
        report = sandbox_report(harness.engine.workspace, init=True)
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["declared"])
        self.assertEqual(report["kind"], "none")
        self.assertIsNone(report["setup"])
        self.assertTrue((harness.engine.workspace / "payload").is_dir())
        self.assertTrue((harness.engine.workspace / "sandbox").is_dir())
        again = sandbox_report(harness.engine.workspace, init=True)
        self.assertEqual(again["exit_code"], 0)
        self.assertTrue(harness.engine.audit()["ok"])

    def test_declared_setup_runs_in_the_sandbox_root_and_writes_the_sidecar(self):
        harness = Harness(self)
        write_config(harness, {"sandbox": {
            "kind": "uv",
            "root": "sandbox",
            "setup": ["python3", "-c", "import pathlib; pathlib.Path('marker').write_text('ok')"],
        }})
        report = sandbox_report(harness.engine.workspace, init=True)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["setup"]["ok"])
        self.assertEqual((harness.engine.workspace / "sandbox" / "marker").read_text(), "ok")
        sidecar = json.loads(
            (harness.engine.workspace / ".sandbox-status.json").read_text(encoding="utf-8")
        )
        self.assertTrue(sidecar["ok"])
        self.assertEqual(report["last_setup"]["ok"], True)

    def test_setup_failure_is_an_observation_not_an_event(self):
        harness = Harness(self)
        before = harness.engine.load()
        write_config(harness, {"sandbox": {
            "kind": "custom",
            "setup": ["python3", "-c", "import sys; sys.exit(3)"],
        }})
        report = sandbox_report(harness.engine.workspace, init=True)
        self.assertEqual(report["exit_code"], 1)
        self.assertFalse(report["setup"]["ok"])
        self.assertEqual(report["setup"]["returncode"], 3)
        after = harness.engine.load()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["status"], "active")

    def test_lockfile_drift_and_absence_are_reported(self):
        harness = Harness(self)
        pin = harness.engine.workspace / "sandbox" / "pins.lock"
        pin.parent.mkdir()
        pin.write_text("pinned-tool==1.0\n", encoding="utf-8")
        good = hashlib.sha256(pin.read_bytes()).hexdigest()
        write_config(harness, {"sandbox": {
            "kind": "uv",
            "lockfile_hashes": {"sandbox/pins.lock": good, "sandbox/missing.lock": "0" * 64},
        }})
        report = sandbox_report(harness.engine.workspace)
        rows = {row["path"]: row for row in report["lockfiles"]}
        self.assertTrue(rows["sandbox/pins.lock"]["match"])
        self.assertFalse(rows["sandbox/missing.lock"]["match"])
        self.assertEqual(report["exit_code"], 1)
        pin.write_text("pinned-tool==2.0\n", encoding="utf-8")
        report = sandbox_report(harness.engine.workspace)
        self.assertFalse(report["lockfiles"][1]["match"])
        self.assertEqual(report["exit_code"], 1)

    def test_sandbox_tools_never_write_outside_the_workspace(self):
        harness = Harness(self)
        root = Path(harness.temp.name)
        sibling = root / "untouched"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep", encoding="utf-8")
        before = tree_snapshot(root, exclude=harness.engine.workspace)
        sandbox_report(harness.engine.workspace, init=True)
        after = tree_snapshot(root, exclude=harness.engine.workspace)
        self.assertEqual(before, after)

    def test_malformed_or_escaping_declarations_are_refused(self):
        harness = Harness(self)
        write_config(harness, {"sandbox": {"setup": "not-an-argv"}})
        with self.assertRaises(ValidationError):
            sandbox_report(harness.engine.workspace)
        write_config(harness, {"sandbox": {"root": "../outside"}})
        with self.assertRaises(ValidationError):
            sandbox_report(harness.engine.workspace)

    def test_sandbox_on_a_missing_workspace_never_creates_litter(self):
        harness = Harness(self)
        ghost = Path(harness.temp.name) / "ghost"
        with self.assertRaises(IntegrityError):
            sandbox_report(ghost, init=True)
        self.assertFalse(ghost.exists())


if __name__ == "__main__":
    unittest.main()
