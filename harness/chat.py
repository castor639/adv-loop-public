"""Chat console in the style of a coding-agent terminal: one stream, an editor, a footer.

The stream is rebuilt from the workspace projections on every refresh, so the
console never holds state the kernel does not. A user message is a shaded
full-width block; a model session is a muted `role · model · time` line and
its text; the tools that session ran are collapsed shaded rows with the
command and exit status, read from the session ledger; harness notices are
dim. The editor sits between two rules, the lower one carrying the live status
while a step runs, and a footer shows the workspace, routing, spend and the
GPU box. In remote mode the projections come from the rsync mirror and every
action runs on the box through `adv-harness op` (harness.remote); locally the
same ops run here.
"""

from __future__ import annotations

import curses
import json
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import live, ops, provision, routing, transcripts
from .tui import clean, read_json, snapshot

COMMANDS = {
    "/new": "start a new task (task, then acceptance criteria)",
    "/ws": "list workspaces, or /ws <n|name> to switch",
    "/step": "run one scheduled step (/step 3 runs three)",
    "/run": "keep stepping until the task pauses, asks, or finishes",
    "/stop": "stop after the current step",
    "/pause": "hold future sessions",
    "/resume": "release an operator hold",
    "/status": "budgets, routing, GPU box",
    "/evidence": "recorded evidence and ranks",
    "/sessions": "model sessions, tokens and cost",
    "/report": "the final report, once written",
    "/tools": "expand or collapse tool output",
    "/help": "this list",
    "/quit": "leave (a running step finishes on the box)",
}
TERMINAL = ("completed", "blocked", "unsafe", "budget_exhausted")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Style names resolved to curses attributes at startup; 256-colour terminals get the shaded blocks.
STYLES = ("text", "muted", "dim", "accent", "success", "error", "warning", "user_bg", "tool_bg", "tool_err_bg", "rule")


def when(stamp: str) -> str:
    return stamp[11:16] if len(stamp) >= 16 else ""


def tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 10000:
        return f"{count / 1000:.1f}k"
    if count < 1000000:
        return f"{round(count / 1000)}k"
    return f"{count / 1000000:.1f}M"


def ledger_for(ws: Path, session_id: Optional[str]) -> List[Dict[str, Any]]:
    if not session_id:
        return []
    path = transcripts.session_dir(ws, session_id) / "ledger.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def describe_tool(entry: Dict[str, Any]) -> str:
    tool = entry.get("tool", "")
    args = entry.get("input") or {}
    if tool in ("run_in_sandbox", "gpu_run"):
        cmd = args.get("command")
        text = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
    elif tool == "validate":
        text = str(args.get("hook", ""))
    else:
        text = str(args.get("path") or args.get("job_id") or "")
    return text.replace("\n", " ")[:160]


def build_stream(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Entries in order: user, session (with tools), harness."""

    state, ws = row["state"], row["path"]
    index = {r.get("directive_id"): r for r in transcripts.load_index(ws) if r.get("accepted")}
    items: List[Dict[str, Any]] = [{"kind": "user", "at": "", "text": state.get("task", ws.name)}]
    for a in state.get("attempts", [])[-200:]:
        parts = [str(a.get("action") or a.get("hypothesis") or "").strip()]
        for key in ("observation_notes", "interpretation"):
            if a.get(key) and str(a[key]) not in parts:
                parts.append(str(a[key]).strip())
        assessment = a.get("assessment") if isinstance(a.get("assessment"), dict) else {}
        verdict = assessment.get("verdict")
        outcome = str(a.get("outcome", "recorded")) + (f" · verdict {verdict}" if verdict else "")
        actor = a.get("actor") or {}
        session = index.get(a.get("directive_id")) or {}
        items.append({"kind": "session", "at": a.get("at", ""), "role": str(a.get("role", "agent")),
                      "mode": str(a.get("mode", "")), "model": actor.get("model", ""), "text": "\n".join(p for p in parts if p),
                      "outcome": outcome, "tools": ledger_for(ws, actor.get("session_id") or session.get("session_id")),
                      "usd": float(session.get("estimated_usd") or 0), "tokens": session.get("tokens") or {}})
    for ex in state.get("human_exchanges", []):
        items.append({"kind": "harness", "at": ex.get("requested_at", ""), "text": ex.get("question", "")})
        if ex.get("answer"):
            items.append({"kind": "user", "at": ex.get("answered_at", ""), "text": ex.get("answer", "")})
    memory = ws / "harness-state" / "memories.jsonl"
    if memory.is_file():
        for raw in memory.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if item.get("source") == "operator":
                items.append({"kind": "user", "at": item.get("at", ""), "text": str(item.get("text", "")), "label": "guidance"})
    items.sort(key=lambda e: e.get("at", ""))
    pending = state.get("human_input")
    if pending:
        items.append({"kind": "harness", "at": "", "text": "Waiting for your reply: " + str(pending.get("question", ""))})
    status = state.get("status", "")
    if status in TERMINAL:
        items.append({"kind": "harness", "at": "", "text": f"Task {status}. /report shows the result."})
    return items


def overview(row: Optional[Dict[str, Any]], state_dir: Path, gpu_box: Dict[str, Any]) -> str:
    api = read_json(state_dir / "api-spend.json")
    aws = read_json(state_dir / "aws-spend.json")
    lines = [f"api spend    ${api.get('api_spend_usd', 0):.2f}  (degrade ${api.get('warn_usd', 0):.0f}, stop ${api.get('hard_stop_usd', 0):.0f})",
             f"aws spend    ${aws.get('aws_spend_usd', 0):.2f}  (gpu line ${aws.get('gpu_stop_usd', 0):.0f}, stop ${aws.get('hard_stop_usd', 0):.0f})",
             f"gpu box      {gpu_box.get('state', 'not configured')}  {gpu_box.get('instance_type', '')}  jobs {gpu_box.get('jobs_running', 0)}"]
    if row:
        ws, state = row["path"], row["state"]
        lines = [f"workspace    {ws.name}", f"status       {state.get('status', '?')}   attempts {len(state.get('attempts', []))}"
                 f"   failure streak {state.get('failure_streak', 0)}"] + lines + ["", "routing"]
        for role in ("planner", "researcher", "critic", "verifier", "explorer", "synthesizer"):
            lines.append(f"  {role:12s} {model_for(ws, role)}")
        gpu = provision.read_config(ws).get("harness", {}).get("gpu", {})
        lines.append(f"  gpu grant    {'yes, ' + str(gpu.get('max_hours')) + ' h' if gpu.get('enabled') else 'no'}")
    return "\n".join(lines)


def evidence_view(row: Dict[str, Any]) -> str:
    items = row["state"].get("evidence", {})
    items = list(items.values()) if isinstance(items, dict) else list(items)
    if not items:
        return "No evidence recorded yet."
    out = []
    for e in items:
        rank = e.get("formalization_rank") or "unranked"
        out.append(f"{e.get('id', '?'):9s} {e.get('kind', ''):18s} {rank:22s} {e.get('locator', '')}")
        if e.get("claim"):
            out.append("          " + str(e["claim"])[:160])
    return "\n".join(out)


def sessions_view(row: Dict[str, Any]) -> str:
    rows = transcripts.load_index(row["path"])[-40:]
    if not rows:
        return "No sessions yet."
    out = []
    for r in reversed(rows):
        tk = r.get("tokens") or {}
        out.append(f"{when(str(r.get('started', ''))):5s} {r.get('role', ''):11s} {r.get('mode', ''):24s} {r.get('model', ''):22s}"
                   f" {r.get('ending', ''):14s} ↑{tokens(int(tk.get('input') or 0)):>6} ↓{tokens(int(tk.get('output') or 0)):>6}"
                   f" ${float(r.get('estimated_usd') or 0):.3f} {'accepted' if r.get('accepted') else ''}")
    return "\n".join(out)


def model_for(ws: Path, role: str) -> str:
    """The model a role would get; falls back to the fleet table when the local environment cannot check it."""

    try:
        return routing.Router(provision.read_config(ws)).resolve(role).spec.model
    except Exception:
        defaults = read_json(Path(routing.__file__).resolve().parent / "state" / "routing-defaults.json")
        alias = (defaults.get("roles") or {}).get(role) or (defaults.get("roles") or {}).get("default")
        return str(((defaults.get("models") or {}).get(alias) or {}).get("model") or alias or "?")


def report_view(row: Dict[str, Any]) -> str:
    path = row["path"] / "report.md"
    return path.read_text(encoding="utf-8") if path.is_file() else "No report yet; the synthesizer writes it at completion."


class Chat:
    def __init__(self, root: Path, state: Path, repo: Optional[Path], remote=None) -> None:
        self.root, self.state, self.repo, self.remote = root, state, repo, remote
        self.rows: List[Dict[str, Any]] = []
        self.selected = 0
        self.input = ""
        self.cursor = 0
        self.history: List[str] = []
        self.history_at = 0
        self.scroll = 0
        self.follow = True
        self.notes: List[Dict[str, Any]] = []
        self.view: Optional[Tuple[str, str]] = None
        self.worker: Optional[threading.Thread] = None
        self.stop_flag = False
        self.busy = ""
        self.busy_since = 0.0
        self.pending_new: Optional[Dict[str, str]] = None
        self.composing = False
        self.expand_tools = False
        self.gpu_box: Dict[str, Any] = {}
        self.attrs: Dict[str, int] = {name: 0 for name in STYLES}

    # ----- data ---------------------------------------------------------------------------------
    def refresh(self) -> None:
        if self.remote is not None:
            self.remote.sync()
            if self.remote.last_error:
                self.note("sync failed: " + self.remote.last_error, error=True)
        previous = self.rows[self.selected]["path"] if self.rows and self.selected < len(self.rows) else None
        self.rows = snapshot(self.root)
        self.selected = next((i for i, r in enumerate(self.rows) if r["path"] == previous),
                             min(self.selected, max(0, len(self.rows) - 1)))
        self.gpu_box = read_json(self.state / "gpu-box.json")

    @property
    def row(self) -> Optional[Dict[str, Any]]:
        if self.composing or not self.rows:
            return None
        return self.rows[min(self.selected, len(self.rows) - 1)]

    def note(self, text: str, *, error: bool = False) -> None:
        self.notes.append({"kind": "error" if error else "harness",
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": text})
        self.notes = self.notes[-50:]
        self.follow = True

    def entries(self) -> List[Dict[str, Any]]:
        if self.row is not None:
            base = build_stream(self.row)
        else:
            base = [{"kind": "harness", "at": "", "text": "Describe the task to start a new workspace, or /ws to pick one."}]
        return base + self.notes

    # ----- actions ------------------------------------------------------------------------------
    def act(self, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.remote is not None:
            return self.remote.op(name, payload)
        return ops.run(name, self.root, self.state, self.repo, payload)

    def in_thread(self, label: str, fn: Callable[[], None]) -> None:
        if self.worker and self.worker.is_alive():
            self.note("still busy: " + self.busy, error=True)
            return
        self.busy, self.busy_since = label, time.monotonic()

        def run() -> None:
            try:
                fn()
            except Exception as exc:
                self.note(f"{label} failed: {exc}", error=True)
            finally:
                self.busy = ""
        self.worker = threading.Thread(target=run, daemon=False)
        self.worker.start()

    def submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.history.append(text)
        self.history_at = len(self.history)
        if text.startswith("/"):
            self.command(text)
            return
        if self.pending_new is not None:
            task = self.pending_new["task"]
            self.pending_new = None
            self.in_thread("creating workspace", lambda: self._create(task, text))
            return
        if self.row is None:
            self.pending_new = {"task": text}
            self.notes.append({"kind": "user", "at": "", "text": text})
            self.note("Acceptance criteria for this task? Separate several with ';'.")
            return
        question = self.row["state"].get("human_input")
        ws = self.row["path"].name
        self.in_thread("sending", lambda: self.note(self.act("message", {
            "workspace": ws, "text": text, "question": question})["message"]))

    def _create(self, task: str, criteria: str) -> None:
        result = self.act("new", {"task": task, "criteria": criteria, "attempts": "20"})
        self.composing = False
        self.notes = []
        self.refresh()
        self.selected = next((i for i, r in enumerate(self.rows) if r["path"].name == result["workspace"]), 0)
        self.note("Workspace created. /step runs the first planning session; /run keeps going.")

    def command(self, text: str) -> None:
        parts = text[1:].split()
        name, args = (parts[0] if parts else ""), parts[1:]
        if name in ("help", "?"):
            self.view = ("help", "\n".join(f"{k:12s} {v}" for k, v in COMMANDS.items())
                         + "\n\nTyping without a slash answers a pending question or leaves guidance for the next session.")
        elif name == "quit":
            raise KeyboardInterrupt
        elif name == "new":
            self.pending_new = None
            self.composing = True
            self.notes = []
            self.note("What should the agent accomplish? Type the task.")
        elif name == "ws":
            if not args:
                lines = [f"{i + 1:2d}. {r['path'].name:36s} {r['state'].get('status', '?'):16s}"
                         f"{'paused' if r['paused'] else ''}" for i, r in enumerate(self.rows)]
                self.view = ("workspaces", "\n".join(lines) or "no workspaces")
            else:
                want = args[0]
                for i, r in enumerate(self.rows):
                    if want == str(i + 1) or want == r["path"].name or r["path"].name.startswith(want):
                        self.selected, self.view, self.follow, self.composing = i, None, True, False
                        self.notes = []
                        return
                self.note("no workspace matches " + want, error=True)
        elif name in ("step", "run"):
            if self.row is None:
                self.note("no workspace selected", error=True)
                return
            count = int(args[0]) if args and args[0].isdigit() else (10_000 if name == "run" else 1)
            ws = self.row["path"].name
            self.stop_flag = False
            self.in_thread(f"stepping {ws}", lambda: self._steps(ws, count))
        elif name == "stop":
            self.stop_flag = True
            self.note("stopping after the current step")
        elif name in ("pause", "resume"):
            if self.row is None:
                return
            ws = self.row["path"].name
            self.in_thread(name, lambda: self.note(self.act(name, {"workspace": ws})["message"]))
        elif name == "status":
            self.view = ("status", overview(self.row, self.state, self.gpu_box))
        elif name == "tools":
            self.expand_tools = not self.expand_tools
        elif name == "evidence" and self.row:
            self.view = ("evidence", evidence_view(self.row))
        elif name == "sessions" and self.row:
            self.view = ("sessions", sessions_view(self.row))
        elif name == "report" and self.row:
            self.view = ("report", report_view(self.row))
        else:
            self.note("unknown command; /help lists them", error=True)

    def _steps(self, ws: str, count: int) -> None:
        for _ in range(count):
            if self.stop_flag:
                self.note("stopped")
                return
            result = self.act("step", {"workspace": ws})
            self.refresh()
            self.note(result["message"])
            inner = result.get("result") or {}
            row = next((r for r in self.rows if r["path"].name == ws), None)
            status = row["state"].get("status") if row else None
            if status != "active" or inner.get("driver_status") in ("paused", "awaiting_human", "fault"):
                if count > 1:
                    self.note(f"run stopped: {inner.get('driver_status') or status}")
                return

    # ----- rendering ----------------------------------------------------------------------------
    def styled(self, name: str) -> int:
        return self.attrs.get(name, 0)

    def lines(self, width: int) -> List[Tuple[str, int]]:
        out: List[Tuple[str, int]] = []
        inner = max(10, width - 4)

        def block(text: str, style: str) -> None:
            for para in clean(text).split("\n"):
                for w in textwrap.wrap(para, inner) or [""]:
                    out.append(((" " + w).ljust(width - 1), self.styled(style)))

        for item in self.entries():
            kind = item["kind"]
            if kind == "user":
                out.append(("", 0))
                if item.get("label"):
                    out.append((f" {item['label']} · {when(item.get('at', ''))}".rstrip(" ·"), self.styled("muted")))
                block(item["text"], "user_bg")
            elif kind == "session":
                out.append(("", 0))
                head = f"● {item['role']} · {item['mode']} · {item['model']} · {when(item['at'])}"
                if item.get("usd"):
                    head += f" · ${item['usd']:.2f}"
                out.append((head, self.styled("accent")))
                for para in clean(item["text"]).split("\n"):
                    for w in textwrap.wrap(para, inner) or [""]:
                        out.append(("  " + w, self.styled("text")))
                for t in item.get("tools", []):
                    code = t.get("exit_code")
                    bad = bool(t.get("error")) or (isinstance(code, int) and code != 0)
                    head = f"  {'✗' if bad else '✓'} {t.get('tool', '')}  {describe_tool(t)}"
                    if code is not None:
                        head += f"  exit {code}"
                    if t.get("duration_ms"):
                        head += f"  {t['duration_ms'] / 1000:.1f}s"
                    out.append((head[: width - 1].ljust(width - 1), self.styled("tool_err_bg" if bad else "tool_bg")))
                    if self.expand_tools:
                        tail = (t.get("stdout_tail") or t.get("stderr_tail") or "").strip()
                        for line in tail.splitlines()[-12:]:
                            out.append(("      " + clean(line)[:inner], self.styled("muted")))
                out.append(("  " + item["outcome"], self.styled("muted")))
            elif kind == "error":
                out.append(("", 0))
                out.append((" ! " + clean(item["text"]), self.styled("error")))
            else:
                out.append(("", 0))
                for w in textwrap.wrap(clean(item["text"]), inner) or [""]:
                    out.append((" " + w, self.styled("dim")))
        return out

    def footer(self, width: int) -> str:
        row = self.row
        name = row["path"].name if row else ("new task" if self.composing else "no workspace")
        status = row["state"].get("status", "") if row else ""
        api = read_json(self.state / "api-spend.json").get("api_spend_usd", 0)
        model = model_for(row["path"], "researcher") if row else ""
        where = f"remote {self.remote.host}" if self.remote is not None else "local"
        gpu = self.gpu_box.get("state") or "off"
        parts = [name + (f" · {status}" if status else ""), model, f"api ${api:.2f}", f"gpu {gpu}", where, "/help"]
        return "  ".join(p for p in parts if p)[: width - 1]

    def draw(self, screen) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        cols = max(1, width - 3)
        editor_rows = max(1, min(4, len(self.input) // cols + 1))
        body_height = max(1, height - editor_rows - 4)
        if self.view is not None:
            title, text = self.view
            shown = [(f" {title}   (esc closes)", self.styled("accent"))]
            for line in clean(text).split("\n"):
                shown.extend([(" " + w, self.styled("text")) for w in (textwrap.wrap(line, width - 3) or [""])])
        else:
            shown = self.lines(width)
        if self.follow or self.view is not None:
            self.scroll = max(0, len(shown) - body_height)
        self.scroll = max(0, min(self.scroll, max(0, len(shown) - body_height)))
        for i, (line, attr) in enumerate(shown[self.scroll:self.scroll + body_height]):
            try:
                screen.addnstr(i, 0, line, width - 1, attr)
            except curses.error:
                pass
        y = body_height
        rule = "─" * (width - 1)
        screen.addnstr(y, 0, rule, width - 1, self.styled("rule"))
        for r in range(editor_rows):
            chunk = self.input[r * cols:(r + 1) * cols]
            screen.addnstr(y + 1 + r, 0, ("> " if r == 0 else "  ") + chunk, width - 1)
        status_rule = rule
        if self.busy:
            spin = SPINNER[int(time.monotonic() * 8) % len(SPINNER)]
            elapsed = int(time.monotonic() - self.busy_since)
            tail = live.tail(self.row["path"], 1) if self.row else []
            detail = tail[0][20:] if tail and len(tail[0]) > 20 else ""
            status_rule = f"── {spin} {self.busy} {elapsed // 60}:{elapsed % 60:02d} {detail} ".ljust(width - 1, "─")[: width - 1]
        elif self.input.startswith("/"):
            matches = [k for k in COMMANDS if k.startswith(self.input.split()[0])]
            if matches:
                status_rule = ("── " + "  ".join(matches[:8]) + " ").ljust(width - 1, "─")[: width - 1]
        screen.addnstr(y + 1 + editor_rows, 0, status_rule, width - 1, self.styled("accent" if self.busy else "rule"))
        screen.addnstr(y + 2 + editor_rows, 0, self.footer(width).ljust(width - 1), width - 1, self.styled("muted"))
        try:
            screen.move(y + 1 + self.cursor // cols, 2 + self.cursor % cols)
        except curses.error:
            pass
        screen.refresh()

    def key(self, ch: int) -> None:
        if ch == curses.KEY_RESIZE:
            return
        if ch == 27:
            self.view = None
            return
        if ch in (curses.KEY_ENTER, 10, 13):
            text, self.input, self.cursor = self.input, "", 0
            self.view = None
            self.submit(text)
        elif ch == 9 and self.input.startswith("/"):
            matches = [k for k in COMMANDS if k.startswith(self.input.split()[0])]
            if len(matches) == 1:
                self.input, self.cursor = matches[0] + " ", len(matches[0]) + 1
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor > 0:
                self.input = self.input[:self.cursor - 1] + self.input[self.cursor:]
                self.cursor -= 1
        elif ch == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif ch == curses.KEY_RIGHT:
            self.cursor = min(len(self.input), self.cursor + 1)
        elif ch in (curses.KEY_HOME, 1):
            self.cursor = 0
        elif ch in (curses.KEY_END, 5):
            self.cursor = len(self.input)
        elif ch == curses.KEY_UP:
            if not self.input and self.history:
                self.history_at = max(0, self.history_at - 1)
                self.input = self.history[self.history_at]
                self.cursor = len(self.input)
            else:
                self.follow, self.scroll = False, max(0, self.scroll - 1)
        elif ch == curses.KEY_DOWN:
            if self.history and self.history_at < len(self.history) and self.input == self.history[self.history_at]:
                self.history_at += 1
                self.input = self.history[self.history_at] if self.history_at < len(self.history) else ""
                self.cursor = len(self.input)
            else:
                self.scroll += 1
        elif ch == curses.KEY_PPAGE:
            self.follow, self.scroll = False, max(0, self.scroll - 10)
        elif ch == curses.KEY_NPAGE:
            self.scroll += 10
        elif ch == 21:
            self.input, self.cursor = "", 0
        elif ch == curses.KEY_MOUSE:
            try:
                _, _, _, _, buttons = curses.getmouse()
            except curses.error:
                return
            if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
                self.follow, self.scroll = False, max(0, self.scroll - 3)
            elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                self.scroll += 3
        elif 32 <= ch < 127 or ch > 255:
            try:
                char = chr(ch)
            except ValueError:
                return
            self.input = self.input[:self.cursor] + char + self.input[self.cursor:]
            self.cursor += 1

    def setup_colors(self) -> None:
        if not curses.has_colors():
            self.attrs.update({"accent": curses.A_BOLD, "muted": curses.A_DIM, "dim": curses.A_DIM,
                               "user_bg": curses.A_REVERSE, "tool_bg": curses.A_DIM, "error": curses.A_BOLD,
                               "rule": curses.A_DIM})
            return
        curses.use_default_colors()
        many = curses.COLORS >= 256
        pairs = {"muted": (245 if many else 7, -1), "dim": (243 if many else 7, -1), "accent": (75 if many else 4, -1),
                 "success": (114 if many else 2, -1), "error": (203 if many else 1, -1), "warning": (222 if many else 3, -1),
                 "user_bg": (-1, 237 if many else 4), "tool_bg": (250 if many else 7, 235 if many else 0),
                 "tool_err_bg": (203 if many else 1, 52 if many else 0), "rule": (240 if many else 7, -1)}
        for i, (name, (fg, bg)) in enumerate(pairs.items(), start=1):
            try:
                curses.init_pair(i, fg, bg)
                self.attrs[name] = curses.color_pair(i)
            except curses.error:
                self.attrs[name] = 0
        if not many:
            self.attrs["muted"] |= curses.A_DIM
            self.attrs["dim"] |= curses.A_DIM
            self.attrs["user_bg"] = curses.A_REVERSE

    def loop(self, screen) -> None:
        curses.curs_set(1)
        screen.keypad(True)
        screen.timeout(120)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        self.setup_colors()
        next_refresh = 0.0
        while True:
            if time.monotonic() >= next_refresh:
                self.refresh()
                next_refresh = time.monotonic() + 1.0
            self.draw(screen)
            ch = screen.getch()
            if ch == -1:
                continue
            self.key(ch)


def launch(root: Optional[Path], state: Optional[Path], repo: Optional[Path], remote=None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("adv-harness ui needs an interactive terminal.", file=sys.stderr)
        return 2
    if remote is not None:
        if not remote.sync(force=True):
            print(f"Cannot mirror {remote.host}: {remote.last_error}", file=sys.stderr)
            return 2
        root, state = remote.root, remote.state
    else:
        from .tui import resolve_paths
        root, state, repo = resolve_paths(root, state, repo)
    if not Path(root).is_dir():
        print(f"Workspace root does not exist: {root}", file=sys.stderr)
        return 2
    chat = Chat(Path(root), Path(state), Path(repo) if repo else None, remote=remote)
    try:
        curses.wrapper(chat.loop)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        print(f"Cannot initialize the terminal: {exc}. Try TERM=xterm-256color.", file=sys.stderr)
        return 2
    if chat.worker and chat.worker.is_alive():
        print("A step is still running; it finishes on its own.")
    return 0
