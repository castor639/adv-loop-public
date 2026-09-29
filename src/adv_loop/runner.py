"""Multi-workspace scheduling above the stale-directive protocol (supervisor plane).

One fleet pass assesses every workspace under a root, then drives the ones
that owe work, at most N at a time. There is no global lock and none is
needed: each drive serializes on its workspace's own file lock, and a
directive issued before a competing write is already rejected as stale. A
corrupt workspace is one bad row, never a poisoned pass.
"""

from __future__ import annotations

import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .driver import SubprocessAdapter, drive
from .engine import LoopEngine
from .errors import AdapterError, LoopError
from .intake import intake_report
from .mirror import mirror_report
from .sandbox import read_loop_config

DEFAULT_MAX_PARALLEL = 1
DEFAULT_MAX_CYCLES_PER = 1
DEFAULT_ADAPTER_RETRIES = 3
DEFAULT_ADAPTER_TIMEOUT = 600.0
DEFAULT_WATCH_INTERVAL = 60.0
DEFAULT_PROBE_TIMEOUT = 120.0
KILL_FILE_NAME = ".fleet-stop"
# The one classification the steward may never answer, no matter what the
# charter says: a safety boundary always reaches a human.
NEVER_AUTO_ANSWERED = "safety_boundary"


def workspace_obligations(state: Dict[str, Any], supervision: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The unmet obligations one workspace owes right now (shared by guard and fleet)."""

    obligations: List[Dict[str, Any]] = []
    if state["status"] == "active":
        obligation = supervision["obligation"]
        label = obligation["action"]
        if obligation.get("role"):
            label += f":{obligation['role']}/{obligation['mode']}"
        if obligation.get("required_move"):
            label += f" (move={obligation['required_move']})"
        obligations.append({"kind": "pending_directive", "detail": label,
                            "directive_id": obligation.get("directive_id")})
        if supervision.get("owed_escalation"):
            obligations.append({"kind": "owed_escalation", "detail": supervision["owed_escalation"]["mark"]})
        if supervision.get("owed_diagnosis"):
            obligations.append({"kind": "owed_diagnosis", "detail": supervision["owed_diagnosis"]["mark"]})
        if supervision.get("stalled"):
            obligations.append({"kind": "stalled", "detail": f"idle {supervision['idle_seconds']}s"})
    elif state["status"] == "blocked":
        retest = supervision.get("retest") or {}
        if retest.get("premise_no_longer_holds"):
            obligations.append({"kind": "unblock_due", "detail": "recorded retest says the premise no longer holds"})
        elif retest.get("due"):
            obligations.append({"kind": "retest_due", "detail": retest.get("premise")})
    return obligations


def _workspace_adapter(workspace: Path, fallback: Optional[Sequence[str]],
                       timeout: float, retries: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the adapter for one workspace: its own config wins, the CLI is fallback.

    Returns ({command, timeout, retries, source}, None) or (None, note).
    """

    config = read_loop_config(workspace)
    declared = config.get("adapter")
    if declared is not None:
        if (not isinstance(declared, list) or not declared
                or any(not isinstance(item, str) or not item.strip() for item in declared)):
            return None, "invalid adapter config: loop-config.json adapter must be an argv list"
        declared_timeout = config.get("adapter_timeout_seconds", timeout)
        declared_retries = config.get("adapter_retries", retries)
        if not isinstance(declared_timeout, (int, float)) or isinstance(declared_timeout, bool) or declared_timeout <= 0:
            return None, "invalid adapter config: adapter_timeout_seconds must be a positive number"
        if not isinstance(declared_retries, int) or isinstance(declared_retries, bool) or declared_retries < 0:
            return None, "invalid adapter config: adapter_retries must be a non-negative integer"
        return {
            "command": list(declared),
            "timeout": float(declared_timeout),
            "retries": declared_retries,
            "source": "workspace-config",
        }, None
    if fallback:
        return {"command": list(fallback), "timeout": timeout, "retries": retries, "source": "cli"}, None
    return None, "no adapter: declare a loop-config adapter or pass one on the command line"


def _workspace_spend(path: Path) -> float:
    """Sum of the adapter spend ledger's estimated_usd entries; 0.0 when absent."""

    total = 0.0
    try:
        for line in (path / ".spend.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line).get("estimated_usd")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(value, (int, float)):
                total += float(value)
    except (OSError, UnicodeDecodeError):
        return 0.0
    return round(total, 6)


def _run_probe(command: Sequence[str], envelope: Dict[str, Any],
               timeout: float = DEFAULT_PROBE_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Run one registered probe hook; a failed probe is a None, never a crash."""

    if (not isinstance(command, list) or not command
            or any(not isinstance(item, str) or not item.strip() for item in command)):
        return None
    try:
        return SubprocessAdapter(command, timeout)(envelope)
    except (AdapterError, LoopError):
        return None


def _charter_text(path: Path) -> Optional[str]:
    try:
        return (path / "payload" / "charter.md").read_text(encoding="utf-8")[:65536]
    except (OSError, UnicodeDecodeError):
        return None


def _autonomy_pass(engine: LoopEngine, path: Path, state: Dict[str, Any],
                   supervision: Dict[str, Any], config: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """Steward answers and probe filings for one workspace, inside its charter.

    Every action lands as an ordinary audited chain event (answer, retest,
    unblock, wish fulfillment); the fleet only automates the filing. Returns
    possibly-refreshed (state, supervision, actions).
    """

    actions: List[Dict[str, Any]] = []
    head = state["integrity"]["event_head"]

    if state["status"] == "awaiting_human":
        steward = config.get("steward")
        classification = (state.get("human_input") or {}).get("classification")
        if isinstance(steward, dict) and classification:
            covered = classification in (steward.get("auto_answer") or [])
            if covered and classification != NEVER_AUTO_ANSWERED:
                answer = None
                command = steward.get("command")
                if command:
                    verdict = _run_probe(command, {
                        "protocol": "adv-loop-steward/1",
                        "workspace": str(path),
                        "question": state["human_input"].get("question"),
                        "context": state["human_input"].get("context"),
                        "classification": classification,
                        "charter": _charter_text(path),
                    })
                    if verdict and not verdict.get("escalate") and isinstance(verdict.get("answer"), str):
                        answer = verdict["answer"]
                else:
                    answer = (
                        f"[steward] Pre-authorized by the workspace charter"
                        f" (classification: {classification}). Proceed within the granted"
                        f" envelope; the grants are recorded in payload/charter.md."
                    )
                if answer:
                    try:
                        state = engine.provide_human_input({
                            "request_id": f"steward:{head[:16]}",
                            "answer": answer,
                        })
                        supervision = engine.supervision(state)
                        actions.append({"kind": "steward_answered", "classification": classification})
                    except LoopError as exc:
                        actions.append({"kind": "steward_failed", "error": exc.as_dict()})
                else:
                    actions.append({"kind": "steward_escalated", "classification": classification})

    if state["status"] == "blocked":
        retest = supervision.get("retest") or {}
        probe = config.get("retest_probe")
        if retest.get("premise_no_longer_holds"):
            try:
                state = engine.unblock({
                    "request_id": f"probe-unblock:{head[:16]}",
                    "reason": "a recorded retest found the blocking premise no longer holds",
                })
                supervision = engine.supervision(state)
                actions.append({"kind": "auto_unblocked"})
            except LoopError as exc:
                actions.append({"kind": "auto_unblock_failed", "error": exc.as_dict()})
        elif retest.get("due") and probe:
            verdict = _run_probe(probe, {
                "protocol": "adv-loop-retest-probe/1",
                "workspace": str(path),
                "premise": retest.get("premise"),
                "probe": retest.get("probe"),
                "recheck_after": retest.get("recheck_after"),
                "last_outcome": retest.get("last_outcome"),
            })
            if verdict and verdict.get("outcome") in {"premise_holds", "premise_no_longer_holds"}:
                record = {
                    "request_id": f"probe-retest:{head[:16]}",
                    "outcome": verdict["outcome"],
                    "note": str(verdict.get("note") or "filed by the registered retest probe"),
                }
                if verdict.get("recheck_after"):
                    record["recheck_after"] = str(verdict["recheck_after"])
                try:
                    state = engine.record_retest(record)
                    actions.append({"kind": "retest_filed", "outcome": verdict["outcome"]})
                    if verdict["outcome"] == "premise_no_longer_holds":
                        state = engine.unblock({
                            "request_id": f"probe-unblock:{head[:16]}",
                            "reason": record["note"],
                        })
                        actions.append({"kind": "auto_unblocked"})
                    supervision = engine.supervision(state)
                except LoopError as exc:
                    actions.append({"kind": "retest_probe_failed", "error": exc.as_dict()})
            elif probe:
                actions.append({"kind": "retest_probe_inconclusive"})

    wish_probe = config.get("wish_probe")
    if wish_probe and state["status"] == "active":
        for wish in list(supervision.get("wishes_due") or []):
            verdict = _run_probe(wish_probe, {
                "protocol": "adv-loop-wish-probe/1",
                "workspace": str(path),
                "wish": wish,
            })
            if verdict and verdict.get("fulfilled") is True:
                try:
                    state = engine.fulfill_wish({
                        "request_id": f"probe-wish:{wish['id']}:{head[:12]}",
                        "wish_id": wish["id"],
                        "note": str(verdict.get("note") or "confirmed by the registered wish probe"),
                    })
                    supervision = engine.supervision(state)
                    actions.append({"kind": "wish_fulfilled", "wish_id": wish["id"]})
                except LoopError as exc:
                    actions.append({"kind": "wish_probe_failed", "error": exc.as_dict()})

    mirror_to = config.get("mirror_to")
    if isinstance(mirror_to, str) and mirror_to.strip():
        try:
            outcome = mirror_report(path, Path(mirror_to))
        except LoopError as exc:
            outcome = {"ok": False, "error": exc.as_dict()}
        actions.append({"kind": "mirrored" if outcome.get("ok") else "mirror_failed",
                        **{k: outcome[k] for k in outcome if k in
                           ("mirror", "appended_bytes", "diverged", "error", "detail")}})

    return state, supervision, actions


def _drive_row(path: Path, resolved: Dict[str, Any], max_cycles: Optional[int]) -> Dict[str, Any]:
    engine = LoopEngine(path)
    adapter = SubprocessAdapter(resolved["command"], resolved["timeout"])
    outcome = drive(engine, adapter, max_cycles=max_cycles, adapter_retries=resolved["retries"])
    return {
        "driver_status": outcome["driver_status"],
        "accepted_attempts": outcome["accepted_attempts"],
        "adapter_rejections": outcome["adapter_rejections"],
    }


def fleet_report(
    root: Path,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    max_cycles_per: int = DEFAULT_MAX_CYCLES_PER,
    adapter_retries: int = DEFAULT_ADAPTER_RETRIES,
    timeout: float = DEFAULT_ADAPTER_TIMEOUT,
    dry_run: bool = False,
    adapter: Optional[Sequence[str]] = None,
    spend_ceiling_usd: Optional[float] = None,
) -> Dict[str, Any]:
    if max_parallel <= 0:
        raise AdapterError("max_parallel must be positive")
    if max_cycles_per <= 0:
        raise AdapterError("max_cycles_per must be positive")
    root = Path(root)
    if not root.is_dir():
        raise LoopError(f"Fleet root is not a directory: {root}")
    fallback = list(adapter) if adapter else None
    if fallback and fallback[0] == "--":
        fallback = fallback[1:]

    rows: List[Dict[str, Any]] = []
    drivable: List[Tuple[int, str, str, Path, Dict[str, Any], Dict[str, Any]]] = []
    worst = 0
    scanned = 0
    # Enumeration never constructs an engine for a non-workspace directory:
    # taking the store lock would create the directory and a lock file.
    for path in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        scanned += 1
        if not (path / "events.jsonl").is_file():
            if (path / "state.json").is_file():
                rows.append({
                    "workspace": str(path), "ok": False,
                    "error": {"error": "legacy_workspace",
                              "message": "v1 snapshot workspace; migrate it explicitly",
                              "details": {"workspace": str(path)}},
                })
                worst = max(worst, 2)
            continue
        engine = LoopEngine(path)
        config = read_loop_config(path)
        try:
            state = engine.load()
            supervision = engine.supervision(state)
            # The charter's standing automation runs before classification: a
            # steward answer or a probe unblock can make this row drivable in
            # the same pass.
            state, supervision, autonomy = _autonomy_pass(engine, path, state, supervision, config)
        except LoopError as exc:
            rows.append({"workspace": str(path), "ok": False, "error": exc.as_dict()})
            worst = max(worst, 2)
            continue
        obligations = workspace_obligations(state, supervision)
        row: Dict[str, Any] = {
            "workspace": str(path),
            "status": state["status"],
            "policy_version": state["policy_version"],
            "drivable": state["status"] == "active",
            "ok": not obligations,
            "obligations": obligations,
            "spend_usd": _workspace_spend(path),
        }
        if autonomy:
            row["autonomy"] = autonomy
            if any(action["kind"] == "mirror_failed" for action in autonomy):
                worst = max(worst, 1)
        if not obligations:
            pass
        elif not row["drivable"] or dry_run:
            worst = max(worst, 1)
        if row["drivable"]:
            ceiling = config.get("spend_ceiling_usd")
            if (isinstance(ceiling, (int, float)) and not isinstance(ceiling, bool)
                    and row["spend_usd"] >= float(ceiling)):
                row["driven"] = False
                row["note"] = f"workspace spend ceiling reached ({row['spend_usd']} >= {ceiling})"
                worst = max(worst, 1)
                rows.append(row)
                continue
            resolved, note = _workspace_adapter(path, fallback, timeout, adapter_retries)
            row["adapter_source"] = resolved["source"] if resolved else None
            if resolved is None:
                row["driven"] = False
                row["note"] = note
                worst = max(worst, 1)
            else:
                pending = supervision.get("pending_directive") or {}
                stalled_lease = bool(pending.get("stale")) or (
                    pending.get("age_seconds", 0) >= supervision.get("stall_horizon_seconds", float("inf"))
                )
                urgent = bool(
                    supervision.get("owed_escalation")
                    or supervision.get("owed_diagnosis")
                    or stalled_lease
                )
                priority = 0 if urgent else 1
                reasons = []
                if supervision.get("owed_escalation"):
                    reasons.append("owed_escalation")
                if supervision.get("owed_diagnosis"):
                    reasons.append("owed_diagnosis")
                if stalled_lease:
                    reasons.append("stale_or_old_lease")
                row["priority"] = priority
                row["priority_reasons"] = reasons
                drivable.append((priority, state["updated_at"], str(path), path, resolved, row))
        rows.append(row)

    drivable.sort(key=lambda item: (item[0], item[1], item[2]))
    drive_order = [item[2] for item in drivable]
    driven = 0
    spend_total = round(sum(row.get("spend_usd", 0.0) for row in rows if "spend_usd" in row), 6)
    spend_stopped = bool(
        spend_ceiling_usd is not None and spend_total >= float(spend_ceiling_usd)
    )
    if spend_stopped and drivable:
        for _priority, _at, _key, _path, _resolved, row in drivable:
            row["driven"] = False
            row["note"] = f"global spend ceiling reached ({spend_total} >= {spend_ceiling_usd})"
        worst = max(worst, 1)
        drivable = []
    if not dry_run and drivable:
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = [
                (row, path, pool.submit(_drive_row, path, resolved, max_cycles_per))
                for _priority, _at, _key, path, resolved, row in drivable
            ]
            for row, path, future in futures:
                row["driven"] = True
                driven += 1
                try:
                    row["drive"] = future.result()
                except AdapterError as exc:
                    row["drive"] = None
                    row["adapter_error"] = exc.as_dict()
                    worst = max(worst, 1)
                except LoopError as exc:
                    row["ok"] = False
                    row["error"] = exc.as_dict()
                    worst = max(worst, 2)
                    continue
                except Exception as exc:  # a runner bug must stay one bad row
                    row["ok"] = False
                    row["error"] = {"error": "runner_failure", "message": str(exc), "details": {}}
                    worst = max(worst, 2)
                    continue
                # Post-pass reassessment: the exit code reflects what is owed now.
                try:
                    engine = LoopEngine(path)
                    state = engine.load()
                    supervision = engine.supervision(state)
                    row["status"] = state["status"]
                    row["obligations"] = workspace_obligations(state, supervision)
                    row["ok"] = not row["obligations"]
                    if row["obligations"]:
                        worst = max(worst, 1)
                except LoopError as exc:
                    row["ok"] = False
                    row["error"] = exc.as_dict()
                    worst = max(worst, 2)

    assessed = [row for row in rows if "status" in row]
    return {
        "root": str(root),
        "scanned": scanned,
        "workspaces": rows,
        "drive_order": drive_order,
        "driven": driven,
        "skipped": len(assessed) - driven,
        "errors": sum(1 for row in rows if row.get("error")),
        "spend_usd": spend_total,
        "spend_stopped": spend_stopped,
        "ok": worst == 0,
        "exit_code": worst,
    }


def process_inbox(inbox: Path, root: Path) -> List[Dict[str, Any]]:
    """Auto-intake every drop waiting in the inbox; move each to processed/ or failed/.

    A drop directory may carry `charter.md` at its top level (it lands at
    payload/charter.md through the normal payload copy) and `charter.json`,
    whose keys are merged into each created workspace's loop-config.json —
    that is how a drop arrives with its standing authorization attached.
    """

    inbox = Path(inbox)
    if not inbox.is_dir():
        raise LoopError(f"Inbox is not a directory: {inbox}")
    processed_dir = inbox / "processed"
    failed_dir = inbox / "failed"
    results: List[Dict[str, Any]] = []
    for entry in sorted(inbox.iterdir()):
        if entry.name in {"processed", "failed"} or entry.name.startswith("."):
            continue
        charter_config: Optional[Dict[str, Any]] = None
        if entry.is_dir() and (entry / "charter.json").is_file():
            try:
                loaded = json.loads((entry / "charter.json").read_text(encoding="utf-8"))
                charter_config = loaded if isinstance(loaded, dict) else None
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                charter_config = None
        try:
            report = intake_report(str(entry), root=root, apply=True)
        except (LoopError, OSError) as exc:
            failed_dir.mkdir(exist_ok=True)
            destination = failed_dir / entry.name
            shutil.move(str(entry), str(destination))
            error = exc.as_dict() if isinstance(exc, LoopError) else {
                "error": "io_error", "message": str(exc), "details": {},
            }
            (failed_dir / f"{entry.name}.error.json").write_text(
                json.dumps(error, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            results.append({"drop": entry.name, "ok": False, "error": error})
            continue
        workspaces = []
        for row in report.get("results", []):
            if not row.get("ok"):
                continue
            workspaces.append(row["workspace"])
            if charter_config:
                config_path = Path(row["workspace"]) / "loop-config.json"
                merged: Dict[str, Any] = {}
                if config_path.is_file():
                    try:
                        existing = json.loads(config_path.read_text(encoding="utf-8"))
                        merged = existing if isinstance(existing, dict) else {}
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        merged = {}
                merged.update(charter_config)
                config_path.write_text(
                    json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
        processed_dir.mkdir(exist_ok=True)
        shutil.move(str(entry), str(processed_dir / entry.name))
        results.append({
            "drop": entry.name,
            "ok": report["exit_code"] == 0,
            "workspaces": workspaces,
            "failed_candidates": [
                row["task_id"] for row in report.get("results", []) if not row.get("ok")
            ],
        })
    return results


def fleet_watch(
    root: Path,
    interval: float = DEFAULT_WATCH_INTERVAL,
    max_passes: Optional[int] = None,
    inbox: Optional[Path] = None,
    **fleet_kwargs: Any,
) -> Dict[str, Any]:
    """Daemon mode: keep intaking and driving until quiescent, killed, or capped.

    Stop conditions, checked in order: the kill file (`<root>/.fleet-stop`),
    the global spend ceiling, quiescence (a pass with nothing owed), and
    `max_passes`. Every stop is honest — the report says which fired.
    """

    root = Path(root)
    kill_file = root / KILL_FILE_NAME
    passes = 0
    last: Optional[Dict[str, Any]] = None
    inbox_results: List[Dict[str, Any]] = []
    stop_reason = None
    while True:
        if kill_file.exists():
            stop_reason = "kill_file"
            break
        if inbox is not None:
            inbox_results.extend(process_inbox(inbox, root))
        last = fleet_report(root, **fleet_kwargs)
        passes += 1
        if last["spend_stopped"]:
            stop_reason = "spend_ceiling"
            break
        if last["exit_code"] == 0:
            stop_reason = "quiescent"
            break
        if max_passes is not None and passes >= max_passes:
            stop_reason = "max_passes"
            break
        if interval > 0:
            time.sleep(interval)
    return {
        "passes": passes,
        "stop_reason": stop_reason,
        "inbox": inbox_results,
        "last": last,
        "exit_code": last["exit_code"] if last else 0,
    }
