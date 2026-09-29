"""The console's remote mode: mirror by rsync, act by `adv-harness op` over ssh."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from adv_loop.engine import LoopEngine

from harness import cli, ops, pause, remote, tui


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.results = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.results:
            return self.results.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")


class RemoteRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        self.clock = [100.0]
        self.rr = remote.RemoteRoot("box", cache=self.tmp / "cache", runner=self.runner, clock=lambda: self.clock[0])

    def test_rsync_mirrors_both_roots_with_the_excludes_and_the_sudo_rsync_path(self):
        self.assertTrue(self.rr.sync())
        argv = [a for a, _ in self.runner.calls]
        self.assertEqual(len(argv), 2)
        self.assertEqual(argv[0][:4], ["rsync", "-az", "--delete", "--timeout=60"])
        self.assertIn("payload/", argv[0])
        self.assertIn(".checker-key", argv[0])
        self.assertNotIn("events.jsonl", argv[0])
        self.assertIn("*.key", argv[0])
        self.assertIn("sudo -u advloop rsync", argv[0])
        self.assertEqual(argv[0][-2:], ["box:/srv/adv-loop/workspaces/", str(self.rr.root) + "/"])
        self.assertEqual(argv[1][-2:], ["box:/srv/adv-loop/state/", str(self.rr.state) + "/"])
        self.assertTrue(self.rr.root.is_dir() and self.rr.state.is_dir())

    def test_sync_is_rate_limited_unless_forced(self):
        self.rr.sync()
        self.assertFalse(self.rr.sync())
        self.clock[0] += remote.SYNC_INTERVAL_SECONDS + 1
        self.assertTrue(self.rr.sync())
        self.assertTrue(self.rr.sync(force=True))
        self.assertEqual(len(self.runner.calls), 6)

    def test_sync_failure_is_recorded_not_raised(self):
        self.runner.results = [subprocess.CompletedProcess([], 255, stdout="", stderr="ssh: connect timed out")]
        self.assertFalse(self.rr.sync())
        self.assertIn("timed out", self.rr.last_error)

    def test_op_sends_the_payload_on_stdin_and_parses_the_last_json_line(self):
        self.runner.results = [subprocess.CompletedProcess([], 0, stdout="noise\n{\"message\": \"done\"}\n", stderr="")]
        result = self.rr.op("pause", {"workspace": "ws-a"})
        self.assertEqual(result, {"message": "done"})
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], "box")
        self.assertIn("sudo -u advloop env ADV_LOOP_STATE=/srv/adv-loop/state", argv[-1])
        self.assertIn("adv-harness op pause --root /srv/adv-loop/workspaces", argv[-1])
        self.assertEqual(json.loads(kwargs["input"]), {"workspace": "ws-a"})
        self.assertEqual(len(self.runner.calls), 3, "a forced sync follows every op")

    def test_op_errors_surface_as_remote_error(self):
        self.runner.results = [subprocess.CompletedProcess([], 1, stdout='{"error": "ValueError", "message": "no such workspace: x"}', stderr="")]
        with self.assertRaises(remote.RemoteError) as caught:
            self.rr.op("step", {"workspace": "x"})
        self.assertIn("no such workspace", str(caught.exception))


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "workspaces"
        self.root.mkdir()
        self.state = self.tmp / "state"

    def test_new_message_pause_resume_round_trip(self):
        created = ops.run("new", self.root, self.state, None, {"task": "print DRILL-OK", "criteria": "it prints", "attempts": "3"})
        ws = self.root / created["workspace"]
        self.assertTrue((ws / "events.jsonl").is_file())
        guidance = ops.run("message", self.root, self.state, None, {"workspace": ws.name, "text": "try sh first"})
        self.assertIn("Guidance saved", guidance["message"])
        memories = (ws / "harness-state" / "memories.jsonl").read_text()
        self.assertIn("try sh first", memories)
        self.assertIn("Paused", ops.run("pause", self.root, self.state, None, {"workspace": ws.name})["message"])
        self.assertTrue(pause.workspace_paused(ws))
        self.assertIn("cleared", ops.run("resume", self.root, self.state, None, {"workspace": ws.name})["message"])
        self.assertFalse(pause.workspace_paused(ws))

    def test_workspace_names_are_contained(self):
        LoopEngine.create(self.root, "t", ["c"], {"max_attempts": 2})
        for bad in ("../etc", "nope", "", "a/b"):
            with self.assertRaises(ValueError):
                ops.run("pause", self.root, self.state, None, {"workspace": bad})

    def test_cli_op_reads_stdin_and_prints_json(self):
        import io, sys
        LoopEngine.create(self.root, "t", ["c"], {"max_attempts": 2})
        ws = next(p for p in self.root.iterdir() if p.is_dir())
        stdin, stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(json.dumps({"workspace": ws.name}))
            sys.stdout = io.StringIO()
            code = cli.main(["op", "pause", "--root", str(self.root), "--state", str(self.state)])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["workspace"], ws.name)
        try:
            sys.stdin = io.StringIO(json.dumps({"workspace": "missing"}))
            sys.stdout = io.StringIO()
            code = cli.main(["op", "step", "--root", str(self.root), "--state", str(self.state)])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.strip())["error"], "ValueError")


class FakeRemote:
    def __init__(self, root, state):
        self.root, self.state, self.host, self.last_error = root, state, "box", None
        self.ops = []

    def sync(self, force=False):
        return True

    def op(self, name, payload):
        self.ops.append((name, payload))
        if name == "new":
            ws = LoopEngine.create(self.root, payload["task"], ["c"], {"max_attempts": 2}).workspace
            return {"workspace": ws.name, "message": "created"}
        return {"workspace": payload["workspace"], "message": f"{name} ok"}


class ConsoleRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "workspaces"
        self.root.mkdir()
        self.remote = FakeRemote(self.root, self.tmp / "state")
        self.console = tui.Console(self.root, self.tmp / "state", self.tmp, remote=self.remote)

    def test_new_workspace_pause_and_step_go_through_the_remote(self):
        self.console.form = {"kind": "new", "values": ["task", "c", "2"], "field": 0, "error": ""}
        self.console.save_form()
        self.assertEqual(self.remote.ops[0][0], "new")
        self.assertEqual(len(self.console.rows), 1)
        self.console.action("pause")
        self.assertEqual(self.remote.ops[-1], ("pause", {"workspace": self.console.rows[0]["path"].name}))
        self.console.rows[0]["paused"] = False
        self.console.action("step")
        self.console.worker.join(5)
        self.assertEqual(self.remote.ops[-1][0], "step")
        self.assertIn("step ok", self.console.message)


if __name__ == "__main__":
    unittest.main()


class ChatTests(unittest.TestCase):
    def setUp(self):
        from harness import chat
        self.chat_mod = chat
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "workspaces"
        self.root.mkdir()
        (self.tmp / "state").mkdir()
        self.remote = FakeRemote(self.root, self.tmp / "state")
        self.chat = chat.Chat(self.root, self.tmp / "state", self.tmp, remote=self.remote)
        self.chat.refresh()

    def wait(self):
        if self.chat.worker:
            self.chat.worker.join(5)

    def test_typing_a_task_then_criteria_creates_a_workspace_through_the_remote(self):
        self.assertIn("Describe the task", self.chat.entries()[0]["text"])
        self.chat.submit("prove the script prints DRILL-OK")
        self.assertIn("Acceptance criteria", self.chat.entries()[-1]["text"])
        self.chat.submit("it prints DRILL-OK; a checker replays it")
        self.wait()
        self.assertEqual(self.remote.ops[0][0], "new")
        self.assertEqual(len(self.chat.rows), 1)
        self.assertEqual(self.chat.entries()[0]["kind"], "user")

    def test_slash_commands_route_step_pause_resume_and_views(self):
        self.chat.submit("task"); self.chat.submit("crit"); self.wait()
        self.chat.submit("/step 2"); self.wait()
        self.assertEqual([n for n, _ in self.remote.ops if n == "step"], ["step", "step"])
        self.chat.submit("/pause"); self.wait()
        self.assertEqual(self.remote.ops[-1][0], "pause")
        self.chat.submit("/status")
        self.assertEqual(self.chat.view[0], "status")
        self.chat.submit("/ws")
        self.assertEqual(self.chat.view[0], "workspaces")
        self.chat.submit("/bogus")
        self.assertIn("unknown command", self.chat.entries()[-1]["text"])

    def test_plain_text_on_a_workspace_is_guidance_or_an_answer(self):
        self.chat.submit("task"); self.chat.submit("crit"); self.wait()
        self.chat.submit("try the simplest script first"); self.wait()
        name, payload = self.remote.ops[-1]
        self.assertEqual(name, "message")
        self.assertEqual(payload["text"], "try the simplest script first")
        self.assertIsNone(payload["question"])
