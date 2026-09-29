"""Local terminal console. Viewing never issues directives or starts model calls."""
from __future__ import annotations

import curses
import fcntl
import json
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

from adv_loop.engine import LoopEngine

from . import live, ops, pause, provision, routing, supervisor, transcripts

TABS = ("Conversation", "Overview", "Evidence", "Sessions", "Report", "Models")


def create_workspace(root, task, criteria, attempts):
    task = task.strip()
    items = [c.strip() for c in criteria.split(";") if c.strip()]
    if not task or not items:
        raise ValueError("Enter a task and at least one acceptance criterion.")
    try:
        limit = int(attempts)
    except ValueError:
        raise ValueError("Attempt limit must be a positive whole number.")
    if limit < 1:
        raise ValueError("Attempt limit must be a positive whole number.")
    return LoopEngine.create(root, task, items, {"max_attempts": limit}).workspace


def submit_message(ws, text, expected_question=None):
    """Answers enter the event chain; guidance enters the existing prompt memory."""
    text = text.strip()
    if not text:
        raise ValueError("Write a message first.")
    engine = LoopEngine(ws)
    state = engine.load()
    if expected_question is not None:
        if state.get("human_input") != expected_question:
            raise ValueError("The pending question changed. Reopen Reply before sending.")
        engine.provide_human_input({"request_id": "ui-answer-" + uuid.uuid4().hex, "answer": text})
        return "Answer recorded. Click Run step to continue."
    if state.get("status") != "active":
        raise ValueError("Guidance is available for active tasks. Use Reply for a pending question.")
    target = ws / supervisor.SUPPLEMENTAL_DIR / "memories.jsonl"
    target.parent.mkdir(exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps({"source": "operator", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "text": text}) + "\n")
    return "Guidance saved for the next model session. Click Run step to continue."


def conversation(row, expanded=False):
    state, ws = row["state"], row["path"]
    pending = state.get("human_input")
    lines = []
    lines.extend(["YOU  /  Task", state.get("task", ws.name), ""])
    entries = []
    for a in state.get("attempts", [])[-100:]:
        body = [str(a.get("action", ""))]
        for key in ("observation_notes", "observation", "interpretation", "uncertainty"):
            if a.get(key) and str(a[key]) not in body:
                body.append(str(a[key]))
        body.append("Outcome: " + str(a.get("outcome", "recorded")))
        actor = a.get("actor") or {}
        entries.append((a.get("at", ""), f"{a.get('role', 'agent').upper()}  /  {actor.get('model', '')}",
                        "\n\n".join(body)))
    for exchange in state.get("human_exchanges", []):
        entries.extend([(exchange.get("requested_at", ""), "HARNESS  /  Question", exchange.get("question", "")),
                        (exchange.get("answered_at", ""), "YOU  /  Answer", exchange.get("answer", ""))])
    memory = ws / supervisor.SUPPLEMENTAL_DIR / "memories.jsonl"
    if memory.is_file():
        for raw in memory.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(raw)
                if entry.get("source") == "operator":
                    entries.append((entry.get("at", ""), "YOU  /  Guidance for next session", entry.get("text", "")))
            except ValueError:
                continue
    for stamp, speaker, body in sorted(entries, key=lambda e: e[0]):
        if not expanded and len(body) > 900:
            body = body[:900].rstrip() + "\n[Message shortened. Click Full messages to read everything.]"
        lines.extend([speaker + "  " + stamp, body, "", "-" * 32, ""])
    if not entries:
        lines.extend(["HARNESS", "Workspace created. Click Run step to begin planning.",
                      "Each click runs one scheduled role and records its result here."])
    if pending:
        lines.extend(["", "HARNESS  /  Waiting for your reply", pending.get("question", ""), "",
                      "Click Reply below to answer this question."])
    return "\n".join(lines)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clean(value):
    """Prevent file content from injecting terminal control sequences."""
    return "".join(c if c.isprintable() or c == "\n" else " " for c in str(value))


def snapshot(root):
    rows = []
    for ws in sorted(root.iterdir()):
        if ws.is_symlink() or not ws.is_dir() or not (ws / "events.jsonl").is_file():
            continue
        state = read_json(ws / "state.json")
        rows.append({"path": ws, "state": state, "paused": pause.workspace_paused(ws)})
    return rows


def detail(row, tab, expanded=False):
    ws, state = row["path"], row["state"]
    if tab == "Conversation":
        return conversation(row, expanded)
    if tab == "Overview":
        lines = [state.get("task", ws.name), "", "STATUS  " + state.get("status", "unreadable projection"),
                 "ATTEMPTS  " + str(state.get("attempt_count", 0)),
                 "PAUSED  " + str(row["paused"]), "", "ACCEPTANCE CRITERIA"]
        for c in state.get("criteria", []):
            lines.extend([f"[{c.get('status', 'open')}] {c.get('id', '')}  {c.get('text', '')}", ""])
        for key in ("human_input", "blocker", "terminal"):
            if state.get(key):
                lines.extend([key.upper(), json.dumps(state[key], indent=2)])
        lines.extend(["", "Live view of saved projections. The engine validates every submitted step.",
                      "Pause takes effect between sessions. Resume clears an operator hold;",
                      "it does not answer human requests or reopen terminal tasks."])
        return "\n".join(lines)
    if tab == "Models":
        router = routing.Router(provision.read_config(ws))
        lines = ["MODEL ROUTING", ""]
        for role in ("planner", "researcher", "critic", "verifier", "explorer", "synthesizer"):
            try:
                a = router.resolve(role)
                lines.extend([f"{role.upper()}  {a.spec.model}",
                              f"  {a.spec.backend} / {a.spec.billing} / {a.source}", ""])
            except ValueError as exc:
                lines.extend([f"{role.upper()}  unavailable", str(exc), ""])
        return "\n".join(lines)
    if tab == "Sessions":
        lines = ["RECORDED MODEL SESSIONS", ""]
        for r in reversed(transcripts.load_index(ws)[-100:]):
            lines.extend([f"{r.get('started', '')}  {r.get('role')} / {r.get('mode')}",
                          f"{r.get('model')} | accepted: {r.get('accepted')} | tools: {r.get('tool_calls', 0)}",
                          f"tokens: {r.get('tokens')} | USD: {r.get('estimated_usd')}",
                          f"transcripts/sessions/{r.get('session_id')}", ""])
        return "\n".join(lines) if len(lines) > 2 else "No model sessions recorded yet."
    if tab == "Activity":
        lines = []
        for a in reversed(state.get("attempts", [])[-100:]):
            lines.extend([f"{a.get('at', '')}  {a.get('role', '')} / {a.get('mode', '')}  {a.get('id', '')}",
                          str(a.get("action", "")), str(a.get("observation_notes", "")),
                          "Outcome: " + str(a.get("outcome", "")), ""])
        return "\n".join(lines) or "No attempts recorded yet."
    filename = "evidence.jsonl" if tab == "Evidence" else "report.md"
    try:
        with (ws / filename).open(encoding="utf-8") as handle:
            content = handle.read(250000)
        if tab == "Evidence":
            lines = []
            for raw in content.splitlines():
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                lines.extend([f"{item.get('id', '')}  {item.get('kind', '')} / {item.get('quality', '')}",
                              str(item.get("claim", "")), "Source: " + str(item.get("locator", "")),
                              "Fingerprint: " + str(item.get("fingerprint", "")), ""])
            return "\n".join(lines) or "No evidence recorded yet."
        return content or "No content recorded yet."
    except OSError:
        return "No content recorded yet."


class Console:
    def __init__(self, root, state, repo, remote=None):
        # `remote` is a harness.remote.RemoteRoot: the root is then a mirror and every action goes over ssh.
        self.root, self.state, self.repo, self.remote = root, state, repo, remote
        self.rows = []
        self.selected = self.tab = self.scroll = 0
        self.message = "Select a workspace. Press ? for help."
        self.worker = None
        self.content = ""
        self.hits = []
        self.form = None
        self.follow = True
        self.sidebar_end = 34
        self.expanded = False

    def refresh(self):
        previous = self.rows[self.selected]["path"] if self.rows else None
        if self.remote is not None:
            self.remote.sync()
            if self.remote.last_error and not (self.worker and self.worker.is_alive()):
                self.message = "Remote sync failed: " + self.remote.last_error
        self.rows = snapshot(self.root)
        self.selected = next((i for i, r in enumerate(self.rows) if r["path"] == previous), 0)
        self.content = detail(self.rows[self.selected], TABS[self.tab], self.expanded) if self.rows else (
            "Welcome to ADV Loop.\n\nClick [+ New workspace] to describe a task and what success looks like.\n"
            "This console refreshes automatically every two seconds.")

    def open_form(self, kind):
        if self.worker and self.worker.is_alive():
            self.message = "Wait for the current operation to finish before editing."
            return
        if kind == "new":
            self.form = {"kind": kind, "title": "New workspace", "labels": ["What should the agent accomplish?",
                         "Acceptance criteria (separate multiple criteria with semicolons)", "Maximum attempts"],
                         "values": ["", "", "20"], "field": 0, "error": ""}
        elif self.rows:
            row = self.rows[self.selected]
            question = row["state"].get("human_input")
            if row["state"].get("status") not in ("active", "awaiting_human"):
                self.message = "This task is closed. Create a new workspace to begin new work."
                return
            self.form = {"kind": "message", "title": "Reply to harness" if question else "Guidance for next session",
                         "labels": ["Your answer" if question else "Your guidance"], "values": [""], "field": 0,
                         "error": "", "workspace": row["path"], "question": question}

    def save_form(self):
        form = self.form
        if not form:
            return
        try:
            if form["kind"] == "new":
                if self.remote is not None:
                    task, criteria, attempts = form["values"]
                    created = self.remote.op("new", {"task": task, "criteria": criteria, "attempts": attempts})
                    ws = self.root / created["workspace"]
                else:
                    ws = create_workspace(self.root, *form["values"])
                self.refresh()
                self.selected = next(i for i, row in enumerate(self.rows) if row["path"] == ws)
                self.tab = self.scroll = 0
                self.message = "Workspace created. Click Run step to start the agent."
                self.form = None
            else:
                if not form["values"][0].strip():
                    raise ValueError("Write a message first.")
                def send():
                    try:
                        if self.remote is not None:
                            self.message = self.remote.op("message", {
                                "workspace": form["workspace"].name, "text": form["values"][0],
                                "question": form["question"]})["message"]
                        else:
                            self.message = submit_message(form["workspace"], form["values"][0], form["question"])
                    except Exception as exc:
                        form["error"] = str(exc)
                        self.form = form
                self.form = None
                self.worker = threading.Thread(target=send, daemon=False)
                self.worker.start()
                self.follow = True
        except Exception as exc:
            form["error"] = str(exc)

    def mouse_action(self, x, y, buttons):
        if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
            if not self.form:
                if x < self.sidebar_end and self.rows:
                    return ("workspace", max(0, self.selected - 1))
                self.follow = False
                self.scroll = max(0, self.scroll - 4)
            return None
        if buttons & getattr(curses, "BUTTON5_PRESSED", 0):
            if not self.form:
                if x < self.sidebar_end and self.rows:
                    return ("workspace", min(len(self.rows) - 1, self.selected + 1))
                self.follow = False
                self.scroll += 4
            return None
        if buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED | curses.BUTTON1_DOUBLE_CLICKED):
            return next((action for hx, hy, w, h, action in reversed(self.hits)
                         if hx <= x < hx + w and hy <= y < hy + h), None)
        return None

    def action(self, action):
        if action == "expand":
            self.expanded = not self.expanded
            self.follow = True
        elif action == "new":
            self.open_form("new")
        elif action == "message":
            self.open_form("message")
        elif action == "save":
            self.save_form()
        elif action == "cancel":
            self.form = None
        elif isinstance(action, tuple):
            kind, index = action
            if kind == "field" and self.form:
                self.form["field"] = index
            elif kind == "workspace":
                self.selected, self.scroll, self.follow = index, 0, self.tab == 0
            elif kind == "tab":
                self.tab, self.scroll, self.follow = index, 0, index == 0
        elif action == "step" and self.rows:
            self.follow = True
            self.step()
        elif action in ("pause", "resume") and self.rows:
            ws = self.rows[self.selected]["path"]
            if self.remote is not None:
                try:
                    self.message = self.remote.op(action, {"workspace": ws.name})["message"]
                except Exception as exc:
                    self.message = "Action failed: " + str(exc)
            elif action == "pause":
                pause.human_hold(ws, "Paused from terminal UI")
                self.message = "Paused future sessions for " + ws.name
            elif (pause.read(ws / pause.PAUSE_FILE) or {}).get("reason") == "human_hold":
                pause.clear(ws / pause.PAUSE_FILE)
                self.message = "Operator hold cleared. Click Run step to continue."
            else:
                self.message = "Only an operator hold can be cleared here."

    def step(self):
        if self.worker and self.worker.is_alive():
            self.message = "A step is already running."
            return
        row = self.rows[self.selected]
        if row["paused"] or row["state"].get("status") != "active":
            self.message = "Step unavailable: workspace must be active and unpaused."
            return
        ws = row["path"]
        self.message = "Running one step: " + ws.name

        self.step_started = time.monotonic()
        self.step_workspace = ws

        def work():
            try:
                if self.remote is not None:
                    self.message = self.remote.op("step", {"workspace": ws.name})["message"]
                    return
                if (self.root / supervisor.FLEET_STOP).is_file():
                    raise ValueError("Fleet stop file is active")
                runtime = supervisor.Runtime(root=self.root, state_dir=self.state, repo=self.repo)
                runtime.api_guard.refresh(self.root, extra_ledgers=[self.state / ".spend.jsonl"])
                if not runtime.aws_guard.allows_sessions():
                    raise ValueError("AWS budget stop is active")
                if not supervisor.workspace_ready(ws):
                    supervisor.provision_workspace(runtime, ws)
                    self.message = "Preparing workspace before starting the agent..."
                    until = time.monotonic() + 15
                    while not supervisor.workspace_ready(ws):
                        ready = read_json(ws / supervisor.READY_FILE)
                        if ready.get("sandbox_init") == "failed":
                            raise ValueError("Sandbox setup failed; inspect .harness/ready.json")
                        if time.monotonic() >= until:
                            self.message = "Setup still running. Click Run step again after it finishes."
                            return
                        time.sleep(0.2)
                if pause.workspace_paused(ws):
                    self.message = "Workspace paused before the session began."
                    return
                result = supervisor.run_directive(runtime, ws, max_cycles=1)
                self.message = (f"Step finished: {result.get('accepted', 0)} accepted. "
                                + str(result.get("reason") or result.get("driver_status", "saved"))
                                + ". See Conversation for recorded work.")
            except Exception as exc:
                self.message = "Step failed: " + str(exc)

        # Keep the process alive until the bounded session has persisted its results.
        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()

    def run(self, screen):
        try:
            curses.curs_set(0)
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        screen.timeout(200)
        refresh_at = 0
        while True:
            if time.monotonic() >= refresh_at:
                try:
                    self.refresh()
                except Exception as exc:
                    self.message = "Refresh failed: " + str(exc)
                refresh_at = time.monotonic() + 2
            screen.erase()
            height, width = screen.getmaxyx()
            self.hits = []

            def put(y, x, text, style=0, limit=None):
                if 0 <= y < height and 0 <= x < width - 1:
                    try:
                        screen.addnstr(y, x, clean(text).replace("\n", " "),
                                       min(width - x - 1, limit or width), style)
                    except curses.error:
                        pass

            def button(y, x, label, action, selected=False):
                text = "[" + label + "]"
                put(y, x, text, curses.A_REVERSE if selected else curses.A_BOLD)
                self.hits.append((x, y, min(len(text), width - x - 1), 1, action))
                return x + len(text) + 1

            if width < 80 or height < 24:
                put(0, 0, "ADV LOOP | Enlarge terminal to 80 x 24. q: quit")
            elif self.form:
                form = self.form
                put(1, 3, "ADV / LOOP   /   " + form["title"], curses.A_BOLD)
                put(3, 3, "Click a field to type. Tab / Enter: next field. F2: submit. Esc: cancel.")
                if form["kind"] == "new":
                    put(4, 3, "Creates an isolated task. Run step starts paid model work after creation.", curses.A_DIM)
                else:
                    put(4, 3, "Saves your answer or guidance. Run step continues the agent afterward.", curses.A_DIM)
                for i, (label, value) in enumerate(zip(form["labels"], form["values"])):
                    y = 6 + i * 4
                    put(y, 3, label, curses.A_BOLD)
                    box_width = width - 8
                    # Tail stays visible when editing long text; full value is preserved.
                    shown = clean(value).replace("\n", " ")
                    put(y + 1, 3, (" " + shown[-(box_width - 3):] + ("_" if i == form["field"] else "")).ljust(box_width),
                        curses.A_REVERSE if i == form["field"] else curses.A_UNDERLINE)
                    self.hits.append((3, y + 1, box_width, 2, ("field", i)))
                    put(y + 2, 3, str(len(value)) + " characters", curses.A_DIM)
                put(height - 5, 3, form["error"], curses.A_BOLD)
                x = button(height - 3, 3, "Create workspace" if form["kind"] == "new" else "Send", "save")
                button(height - 3, x + 2, "Cancel", "cancel")
            else:
                accent = curses.color_pair(1) | curses.A_BOLD
                put(0, 2, "ADV / LOOP", accent)
                busy = bool(self.worker and self.worker.is_alive())
                put(0, 19, "WORKSPACE CONSOLE  /  " + ("WORKING" if busy else "READY"))
                put(1, 2, str(self.root), curses.A_DIM)
                split = min(34, width // 3)
                self.sidebar_end = split
                button(3, 2, "+ New workspace", "new")
                put(5, 2, "WORKSPACES  " + str(len(self.rows)), accent)
                available = max(1, (height - 12) // 3)
                start = max(0, self.selected - available + 1)
                for i, row in enumerate(self.rows[start:start + available], start):
                    y = 7 + (i - start) * 3
                    style = curses.A_REVERSE if i == self.selected else 0
                    put(y, 2, row["path"].name[:split - 4], style)
                    label = "paused" if row["paused"] else row["state"].get("status", "unreadable")
                    put(y + 1, 2, label.replace("_", " "), curses.A_DIM, split - 4)
                    self.hits.append((2, y, split - 3, 2, ("workspace", i)))
                for y in range(3, height - 6):
                    put(y, split, "|", curses.A_DIM)
                x = split + 2
                tx, ty = x, 3
                for index, tab in enumerate(TABS):
                    if tx + len(tab) + 3 >= width:
                        ty, tx = ty + 1, x
                    tx = button(ty, tx, tab, ("tab", index), index == self.tab)
                if self.tab == 0:
                    button(5, x, "Compact messages" if self.expanded else "Full messages", "expand")
                lines = []
                for line in clean(self.content).splitlines():
                    lines.extend(textwrap.wrap(line, max(10, width - x - 2), replace_whitespace=False) or [""])
                visible = height - 14
                if self.follow:
                    self.scroll = max(0, len(lines) - visible)
                self.scroll = min(self.scroll, max(0, len(lines) - visible))
                for y, line in enumerate(lines[self.scroll:self.scroll + visible], 7):
                    put(y, x, line)
                put(height - 7, x, f"Lines {self.scroll + 1}-{min(len(lines), self.scroll + visible)} / {len(lines)}  |  Mouse wheel to scroll", curses.A_DIM)
                row = self.rows[self.selected] if self.rows else None
                waiting = bool(row and row["state"].get("human_input"))
                editable = bool(row and row["state"].get("status") in ("active", "awaiting_human"))
                if editable:
                    hint = "Answer the pending question..." if waiting else "Write guidance for the next model session..."
                    put(height - 5, 2, ("  " + hint).ljust(width - 4), curses.A_REVERSE)
                    self.hits.append((2, height - 5, width - 4, 1, "message"))
                else:
                    put(height - 5, 2, "Start a new task with [+ New workspace]." if not row else "Task closed. Read the report or create a new workspace.", curses.A_DIM)
                bx = 2
                if editable:
                    bx = button(height - 3, bx, "Reply" if waiting else "Send guidance", "message")
                if row:
                    bx = button(height - 3, bx, "Run step", "step")
                    bx = button(height - 3, bx, "Resume" if row["paused"] else "Pause", "resume" if row["paused"] else "pause")
                button(height - 3, bx, "Help", "help")
                footer = self.message
                if busy and getattr(self, "step_workspace", None) is not None:
                    elapsed = int(time.monotonic() - self.step_started)
                    last = live.tail(self.step_workspace, 1)
                    footer = f"[{elapsed // 60}m{elapsed % 60:02d}s] " + (last[0][20:] if last else self.message)
                put(height - 2, 2, footer, accent)
                put(height - 1, 2, "Click to navigate | n new | i write | s run step | j/k select | Tab views | q quit", curses.A_DIM)
            screen.refresh()
            try:
                key = screen.get_wch() if hasattr(screen, "get_wch") else screen.getch()
            except curses.error:
                continue
            char = key if isinstance(key, str) else (chr(key) if 0 <= key < 256 else "")
            code = ord(key) if isinstance(key, str) and len(key) == 1 else key
            action = None
            if code == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, buttons = curses.getmouse()
                    action = self.mouse_action(mx, my, buttons)
                except curses.error:
                    continue
            elif self.form:
                index = self.form["field"]
                if code == 27:
                    action = "cancel"
                elif code == curses.KEY_F2:
                    action = "save"
                elif code in (9, 10, 13, curses.KEY_DOWN):
                    self.form["field"] = (index + 1) % len(self.form["values"])
                elif code in (curses.KEY_BTAB, curses.KEY_UP):
                    self.form["field"] = (index - 1) % len(self.form["values"])
                elif code in (curses.KEY_BACKSPACE, 127, 8):
                    self.form["values"][index] = self.form["values"][index][:-1]
                elif code == 21:
                    self.form["values"][index] = ""
                elif char and char.isprintable() and len(self.form["values"][index]) < 8000:
                    self.form["values"][index] += char
            elif char == "q":
                if self.worker and self.worker.is_alive():
                    self.message = "Wait for the running operation to save before quitting."
                else:
                    return
            elif char in ("j", "k") or code in (curses.KEY_DOWN, curses.KEY_UP):
                if self.rows:
                    action = ("workspace", (self.selected + (1 if char == "j" or code == curses.KEY_DOWN else -1)) % len(self.rows))
            elif code in (9, curses.KEY_RIGHT, curses.KEY_LEFT):
                action = ("tab", (self.tab + (-1 if code == curses.KEY_LEFT else 1)) % len(TABS))
            elif code in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                self.follow = False
                self.scroll = max(0, self.scroll + (1 if code == curses.KEY_NPAGE else -1) * max(1, height - 14))
            else:
                action = {"n": "new", "i": "message", "s": "step", "p": "pause", "r": "resume", "?": "help"}.get(char)
            if action == "help":
                self.content = ("GETTING STARTED\n\n1. Click [+ New workspace]. Enter your task, acceptance criteria, and attempt limit.\n"
                                "2. Click Create workspace.\n3. Click Run step to start a real model session.\n\n"
                                "CONVERSATION\n\nThe conversation shows recorded agent work, your guidance, and questions.\n"
                                "Click the input box or press i to compose. Guidance reaches the next model session.\n"
                                "Reply records an answer to a pending question. Run step continues the agent.\n\n"
                                "MOUSE\n\nClick workspaces, tabs, input fields, and buttons. Use the wheel to scroll.\n"
                                "Hold Shift while selecting text to copy in most terminals.\n\n"
                                "KEYBOARD\n\nn: new | i: write | Tab: views | j/k: workspace | PgUp/PgDn: scroll\n"
                                "In forms: Tab/Enter change field; F2 submits; Esc cancels; Ctrl+U clears field.\n"
                                "s: run step | p: pause future sessions | r: resume operator hold | q: quit\n\n"
                                "Run step uses your configured model budgets. Closing this UI does not start a background fleet.")
                self.scroll, self.follow = 0, False
                refresh_at = float("inf")
            elif action:
                try:
                    self.action(action)
                except Exception as exc:
                    self.message = "Action failed: " + str(exc)
                refresh_at = 0


def resolve_paths(root=None, state=None, repo=None):
    """Honor explicit paths; otherwise discover a checkout, even from a home shell."""
    if repo is None:
        cwd = Path.cwd().resolve()
        candidates = [cwd, *cwd.parents, Path(__file__).resolve().parent.parent]
        repo = next((p for p in candidates if (p / "harness/cli.py").is_file()
                     and (p / "workspaces").is_dir()), candidates[-1])
    repo = Path(repo).resolve()
    return (Path(root).resolve() if root is not None else repo / "workspaces",
            Path(state).resolve() if state is not None else repo / "harness/state", repo)


def launch(root=None, state=None, repo=None, remote=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("adv-harness ui needs an interactive terminal (use ssh -t for SSH).", file=sys.stderr)
        return 2
    if remote is not None:
        if not remote.sync(force=True):
            print(f"Cannot mirror {remote.host}: {remote.last_error}", file=sys.stderr)
            return 2
        root, state = remote.root, remote.state
        repo = Path(repo).resolve() if repo else Path(__file__).resolve().parent.parent
    else:
        root, state, repo = resolve_paths(root, state, repo)
    if not root.is_dir():
        print(f"Workspace root does not exist: {root}", file=sys.stderr)
        return 2
    try:
        curses.wrapper(Console(root, state, repo, remote=remote).run)
    except curses.error as exc:
        print(f"Cannot initialize terminal UI: {exc}. Try TERM=xterm-256color adv-harness ui.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Console closed. Any active step will finish saving before this process exits.")
    return 0
