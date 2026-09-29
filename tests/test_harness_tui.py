"""Console reads remain inert; step controls respect workspace state."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import tui
from adv_loop.engine import LoopEngine


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "task"
        self.ws.mkdir()
        (self.ws / "events.jsonl").write_text("{}\n")
        (self.ws / "state.json").write_text(json.dumps({
            "task": "A test task", "status": "awaiting_human", "attempt_count": 0,
            "criteria": [{"id": "C1", "text": "An observable result", "status": "open"}]}))

    def test_read_all_views_does_not_change_workspace(self):
        before = {p.name: p.read_bytes() for p in self.ws.iterdir()}
        row = tui.snapshot(self.root)[0]
        for tab in tui.TABS:
            self.assertIsInstance(tui.detail(row, tab), str)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ws.iterdir()})
        self.assertIn("An observable result", tui.detail(row, "Overview"))

    def test_human_wait_does_not_start_a_worker(self):
        console = tui.Console(self.root, self.root / "state", self.root)
        console.refresh()
        console.step()
        self.assertIsNone(console.worker)
        self.assertIn("unavailable", console.message)

    def test_corrupt_projection_and_symlink_are_handled(self):
        (self.root / "alias").symlink_to(self.ws, target_is_directory=True)
        (self.ws / "state.json").write_text("{")
        rows = tui.snapshot(self.root)
        self.assertEqual(len(rows), 1)
        self.assertIn("unreadable", tui.detail(rows[0], "Overview"))

    def test_control_sequences_are_removed(self):
        self.assertNotIn("\x1b", tui.clean("hello\x1b[2J"))

    def test_step_uses_supervisor_with_one_cycle(self):
        console = tui.Console(self.root, self.root / "state", self.root)
        console.refresh()
        console.rows[0]["state"]["status"] = "active"
        with patch.object(tui.supervisor, "Runtime") as runtime, patch.object(
                tui.supervisor, "workspace_ready", return_value=True), patch.object(
                tui.supervisor, "run_directive", return_value={"accepted": 1}) as drive:
            console.step()
            console.worker.join(timeout=3)
            drive.assert_called_once_with(runtime.return_value, self.ws, max_cycles=1)
            self.assertIn("Step finished", console.message)

    def test_budget_stop_prevents_execution(self):
        console = tui.Console(self.root, self.root / "state", self.root)
        console.refresh()
        console.rows[0]["state"]["status"] = "active"
        with patch.object(tui.supervisor, "Runtime") as runtime, patch.object(
                tui.supervisor, "run_directive") as drive:
            runtime.return_value.aws_guard.allows_sessions.return_value = False
            console.step()
            console.worker.join(timeout=3)
            drive.assert_not_called()
            self.assertIn("budget stop", console.message)

    def test_non_terminal_has_actionable_error(self):
        with patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(tui.launch(self.root, self.root, self.root), 2)

    def test_launch_from_unrelated_directory_finds_installation(self):
        with patch.object(Path, "cwd", return_value=self.root):
            root, state, repo = tui.resolve_paths()
        self.assertEqual(repo, Path(tui.__file__).resolve().parent.parent)
        self.assertEqual(root, repo / "workspaces")
        self.assertEqual(state, repo / "harness/state")

    def test_explicit_paths_are_never_replaced(self):
        root, state, repo = tui.resolve_paths(self.ws, self.root / "custom-state", self.root)
        self.assertEqual((root, state, repo), (self.ws, self.root / "custom-state", self.root))

    def test_launch_from_checkout_subdirectory_finds_checkout(self):
        checkout = self.root / "checkout"
        (checkout / "harness").mkdir(parents=True)
        (checkout / "harness/cli.py").touch()
        (checkout / "workspaces").mkdir()
        nested = checkout / "workspaces/task"
        nested.mkdir()
        with patch.object(Path, "cwd", return_value=nested):
            self.assertEqual(tui.resolve_paths()[2], checkout)

    def test_navigation_and_resize(self):
        class Screen:
            keys = iter([ord("j"), 9, tui.curses.KEY_NPAGE, ord("?"), 9, ord("q")])
            def timeout(self, value): pass
            def erase(self): pass
            def getmaxyx(self): return (24, 100)
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)
        with patch.object(tui.curses, "curs_set"), patch.object(tui.curses, "has_colors", return_value=False), patch.object(tui.curses, "color_pair", return_value=0):
            console = tui.Console(self.root, self.root, self.root)
            console.run(Screen())
            self.assertEqual(console.tab, 2)
            screen = Screen()
            screen.getmaxyx = lambda: (8, 30)
            screen.keys = iter([ord("q")])
            console.run(screen)

    def test_create_workspace_is_replayable_and_bounded(self):
        ws = tui.create_workspace(self.root, "Build a useful tool", "It runs; It has documentation", "12")
        state = LoopEngine(ws).load()
        self.assertEqual(state["task"], "Build a useful tool")
        self.assertEqual(len(state["criteria"]), 2)
        self.assertEqual(state["budget"]["max_attempts"], 12)
        self.assertEqual(state["attempt_count"], 0)
        self.assertFalse((ws / ".pending-directive.json").exists())

    def test_invalid_creation_writes_nothing(self):
        before = set(self.root.iterdir())
        for task, criteria, budget in [("", "works", "2"), ("task", "", "2"), ("task", "works", "0")]:
            with self.assertRaises(ValueError):
                tui.create_workspace(self.root, task, criteria, budget)
        self.assertEqual(before, set(self.root.iterdir()))

    def test_guidance_reaches_prompt_without_changing_events(self):
        ws = tui.create_workspace(self.root, "Task", "Works", "4")
        before = (ws / "events.jsonl").read_bytes()
        tui.submit_message(ws, "Try a small reproducible example first.")
        prompt = tui.supervisor.supplemental_text(ws, "researcher", "experiment")
        self.assertIn("Try a small reproducible example first.", prompt)
        self.assertEqual(before, (ws / "events.jsonl").read_bytes())
        self.assertIn("YOU  /  Guidance", tui.conversation({"path": ws, "state": LoopEngine(ws).load()}))

    def test_reply_records_answer_and_resumes(self):
        ws = tui.create_workspace(self.root, "Task", "Works", "4")
        engine = LoopEngine(ws)
        state = engine.request_human_input({"request_id": "ask-one", "question": "Which file?",
                                            "classification": "private_data"})
        tui.submit_message(ws, "Use the public fixture.", state["human_input"])
        state = engine.load()
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["human_exchanges"][0]["answer"], "Use the public fixture.")
        with self.assertRaises(ValueError):
            tui.submit_message(ws, "Stale answer", {"question": "Which file?"})

    def test_mouse_hits_and_wheel_navigation(self):
        console = tui.Console(self.root, self.root, self.root)
        console.refresh()
        console.hits = [(2, 3, 19, 1, "new"), (40, 3, 14, 1, ("tab", 1))]
        self.assertEqual(console.mouse_action(5, 3, tui.curses.BUTTON1_RELEASED), "new")
        action = console.mouse_action(42, 3, tui.curses.BUTTON1_CLICKED)
        console.action(action)
        self.assertEqual(console.tab, 1)
        self.assertIsNone(console.mouse_action(70, 12, tui.curses.BUTTON1_CLICKED))
        console.scroll = 10
        console.mouse_action(60, 8, tui.curses.BUTTON4_PRESSED)
        self.assertEqual(console.scroll, 6)

    def test_conversation_can_expand_long_messages(self):
        row = {"path": self.ws, "state": {"task": "Task", "attempts": [
            {"role": "researcher", "action": "a" * 1500, "at": "2026-09-11"}]}}
        self.assertIn("Message shortened", tui.conversation(row))
        self.assertIn("a" * 1500, tui.conversation(row, expanded=True))

    def test_new_form_and_cancel_are_inert(self):
        console = tui.Console(self.root, self.root, self.root)
        before = set(self.root.iterdir())
        console.action("new")
        self.assertEqual(console.form["title"], "New workspace")
        console.action("save")
        self.assertTrue(console.form["error"])
        console.action("cancel")
        self.assertIsNone(console.form)
        self.assertEqual(before, set(self.root.iterdir()))
