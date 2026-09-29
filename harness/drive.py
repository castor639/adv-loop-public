"""Drive one workspace in the foreground and print every outcome.

`python3 -m harness.drive WORKSPACE --state STATE` repeats `run_directive`
for that workspace only, stopping on a terminal state, a pause, a block, or
a process fault. Other workspaces under the same root are never touched.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from adv_loop.engine import LoopEngine

from . import live, pause, supervisor


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="adv-harness drive")
    parser.add_argument("workspace")
    parser.add_argument("--state", required=True)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--max-steps", type=int, default=None, help="stop after this many accepted attempts")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between steps")
    args = parser.parse_args(argv)
    ws = Path(args.workspace).resolve()
    runtime = supervisor.Runtime(root=ws.parent, state_dir=Path(args.state).resolve(),
                                 repo=Path(args.repo).resolve() if args.repo else None)
    accepted = 0
    while True:
        state = LoopEngine(ws).load()
        if state.get("status") != "active":
            print(json.dumps({"stop": "workspace not active", "status": state.get("status")}), flush=True)
            return 0
        if pause.workspace_paused(ws):
            print(json.dumps({"stop": "workspace paused", "pause": pause.read(ws / pause.PAUSE_FILE)}), flush=True)
            return 0
        runtime.api_guard.refresh(runtime.root, extra_ledgers=[runtime.state_dir / ".spend.jsonl"])
        if not runtime.aws_guard.allows_sessions():
            print(json.dumps({"stop": "AWS budget stop is active"}), flush=True)
            return 1
        if not supervisor.workspace_ready(ws):
            supervisor.provision_workspace(runtime, ws)
            while not supervisor.workspace_ready(ws):
                try:
                    ready = json.loads((ws / supervisor.READY_FILE).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    ready = {}
                if ready.get("sandbox_init") == "failed":
                    print(json.dumps({"stop": "sandbox setup failed", "ready": ready}), flush=True)
                    return 1
                time.sleep(1)
        live.note(ws, f"drive: step start (phase={state.get('phase')} attempts={state.get('attempt_count')})")
        result = supervisor.run_directive(runtime, ws, max_cycles=1)
        result = {k: v for k, v in result.items() if k != "feedback"} | {"feedback": [
            f"{f.get('error')}: {str(f.get('message'))[:200]}" for f in result.get("feedback", [])]}
        print(json.dumps({"at": time.strftime("%H:%M:%SZ", time.gmtime()), **result}, sort_keys=True, default=str), flush=True)
        accepted += int(result.get("accepted") or 0)
        status = result.get("driver_status")
        if status in ("terminal", "blocked", "error", "fault") or (status == "paused" and result.get("reason") != "max_cycles"):
            live.note(ws, f"drive: stopped ({status}: {result.get('reason') or result.get('fault_signature') or ''})")
            return 0 if status == "terminal" else 2
        if args.max_steps is not None and accepted >= args.max_steps:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
