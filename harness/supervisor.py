"""One process per box: intake, provision, assess, drive, idle-stop.

`run_directive` mirrors the kernel's driver loop but owns the session:
fresh session id per try, prompt assembly, the backend, transcript
persistence before submission, spend, pauses on rate limits, rejection
feedback, stale-directive detection, and the process-fault event when a
burst of retries is exhausted. `pass_once` asks the kernel's fleet scheduler
what is drivable and runs one directive per workspace in parallel.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adv_loop.driver import fault_signature
from adv_loop.engine import LoopEngine
from adv_loop.errors import LoopError, TransitionError
from adv_loop.runner import fleet_report, process_inbox
from adv_loop.sandbox import sandbox_report

from . import (architecture, assembler, budget, live, pause, provision, refine_global, routing, session,
               taskbudget, transcripts)
from . import gpu as gpu_module
from .backends import make_backend
from .backends.base import ModelSpec, WorkspaceHandle
from .broker import Broker, BrokerThread
from .container import Docker, container_name
from .gpu import box_api
from .gpu import jobs as gpu_jobs
from .gpu import lock as gpu_lock_module
from .gpu import spend as gpu_spend
from .gpu.broker import GPU_DIR as GPU_REQUEST_DIR
from .gpu.broker import GpuBroker, GpuBrokerThread
from .gpu.service import GpuService

READY_FILE = ".harness/ready.json"
REJECTIONS_FILE = ".harness/rejections.jsonl"
FLEET_STOP = ".fleet-stop"
WAKE_FILE = "wake_at.json"
SUPPLEMENTAL_DIR = "harness-state"
GPU_BOX_ERROR_FILE = "gpu-box.error.txt"


@dataclass
class Runtime:
    root: Path
    state_dir: Path
    inbox: Optional[Path] = None
    repo: Optional[Path] = None
    docker: Optional[Docker] = None
    image: Optional[str] = None
    max_parallel: int = 2
    adapter_retries: int = 3
    max_cycles_per: int = 1
    fleet_spend_ceiling_usd: float = budget.API_HARD_STOP_USD
    agent_id: str = "adv-harness"
    idle_stop_minutes: int = 20
    pass_interval_seconds: float = 30.0
    shutdown_command: Optional[List[str]] = None
    backend_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    router_factory: Callable[[Dict[str, Any]], routing.Router] = routing.Router
    api_guard: Optional[budget.ApiSpendGuard] = None
    aws_guard: Optional[budget.AwsSpendGuard] = None
    refine_enabled: bool = True
    task_budget_enabled: bool = True
    refine_max_usd_per_day: float = 2.0
    refine_session: Optional[Callable[..., Any]] = None
    global_refine_enabled: bool = True
    # Every pass, not every twelfth: the fleet loop should read back what the workspaces just did
    # while it is still what they just did. Its own triggers and daily cap keep the cost bounded.
    refine_every: int = 1
    global_refine_max_usd_per_day: float = 5.0
    fleet_state: Optional[Path] = None
    # The GPU box controller (`harness.gpu.box_api.Box`), or None when no workspace may use the box.
    gpu: Optional[Any] = None

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.api_guard is None:
            self.api_guard = budget.ApiSpendGuard(self.state_dir / "api-spend.json", hard_stop_usd=self.fleet_spend_ceiling_usd)
        if self.aws_guard is None:
            self.aws_guard = budget.AwsSpendGuard(self.state_dir / "aws-spend.json")
        if self.gpu is None and (os.environ.get("ADV_LOOP_GPU_BOX") or os.environ.get("ADV_LOOP_GPU_ENABLED") == "1"):
            try:
                self.gpu = box_api.load_box(self.state_dir, guard=self.aws_guard, repo=self.repo)
            except Exception as exc:  # a broken controller leaves the fleet on CPU, never down
                (self.state_dir / GPU_BOX_ERROR_FILE).write_text(
                    f"{budget.utc_now()} {type(exc).__name__}: {exc}\n", encoding="utf-8")
                self.gpu = None

    @property
    def containers(self) -> bool:
        return self.docker is not None

    def gpu_active_jobs(self) -> List[Dict[str, Any]]:
        if self.gpu is None:
            return []
        try:
            return list(self.gpu.active_jobs())
        except Exception:
            return []

    def stop_gpu_box(self, reason: str) -> Optional[Dict[str, Any]]:
        if self.gpu is None:
            return None
        try:
            return self.gpu.stop_now(reason=reason)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


def supplemental_text(ws: Path, role: str, mode: str, fleet_state: Optional[Path] = None) -> Optional[str]:
    """Advisory addenda the loops append: fleet-level first, then this workspace's; capped by the assembler."""

    from .improvement import FLEET_STATE_DIR
    parts = []
    roots = [(Path(fleet_state or FLEET_STATE_DIR), "fleet"), (ws / SUPPLEMENTAL_DIR, "workspace")]
    for root, label in roots:
        base = root / "prompt-addenda"
        for name in ("all.md", f"{role}.md", f"{role}.{mode}.md"):
            path = base / name
            if path.is_file():
                parts.append(f"## {label}: {name}\n\n" + path.read_text(encoding="utf-8").strip())
    memories = ws / SUPPLEMENTAL_DIR / "memories.jsonl"
    if memories.is_file():
        lines = [line for line in memories.read_text(encoding="utf-8").splitlines() if line.strip()][-20:]
        notes = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not item.get("modes") or mode in item["modes"]:
                notes.append(f"- {item.get('text', '').strip()}")
        if notes:
            parts.append("## memories\n\n" + "\n".join(notes))
    pending = gpu_jobs.pending_collection_note(ws)
    if pending:
        parts.append(pending)
    return "\n\n".join(parts) if parts else None


def target_transcript_dir(ws: Path, directive: Dict[str, Any]) -> Optional[str]:
    context = directive.get("review_context") or {}
    target = context.get("target") or context.get("attempt") or {}
    wanted = target.get("directive_id") or directive.get("target_directive_id")
    if not wanted:
        return None
    for row in reversed(transcripts.load_index(ws)):
        if row.get("directive_id") == wanted and row.get("accepted"):
            return str(transcripts.session_dir(ws, row["session_id"]))
    return None


def _append_rejection(ws: Path, record: Dict[str, Any]) -> None:
    path = ws / REJECTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _session_error(result) -> Dict[str, Any]:
    return {"error": f"session_{result.ended}", "message": "; ".join(result.errors)[:500] or result.ended,
            "details": {"session_id": result.session_id, "exit_code": result.exit_code}}


def run_session(runtime: Runtime, ws: Path, directive: Dict[str, Any], feedback: List[Dict[str, Any]],
                assignment: routing.Assignment) -> Dict[str, Any]:
    """One model session for one directive try. Returns what happened, never raises for model trouble."""

    spec = assignment.spec
    session_id = uuid.uuid4().hex
    config = provision.read_config(ws)
    container = (config.get("harness") or {}).get("container") if runtime.containers else None
    handle = WorkspaceHandle(ws_id=ws.name, path=ws, session_id=session_id, container=container)
    broker = Broker(ws, session_id, allowed_hooks=sorted((config.get("validators") or {}).keys()) or None)
    role, mode = directive["role"], directive["mode"]
    gpu_enabled = gpu_module.enabled(config)
    gpu_config = gpu_module.config_for(config)
    gpu_service: Optional[GpuService] = None
    if gpu_enabled and runtime.gpu is not None:
        gpu_service = GpuService(runtime.gpu, ws, runtime.state_dir, gpu_config, session_id=session_id,
                                 directive_id=directive.get("directive_id"), role=role, mode=mode,
                                 guard=runtime.aws_guard)
    thread: Optional[BrokerThread] = None
    gpu_thread: Optional[GpuBrokerThread] = None
    if spec.backend == "claude_code":
        thread = BrokerThread(broker)
        thread.start()
        if gpu_service is not None:
            gpu_thread = GpuBrokerThread(GpuBroker(gpu_service, ws / GPU_REQUEST_DIR, session_id,
                                                   ledger_path=handle.session_dir / "ledger.jsonl"))
            gpu_thread.start()
    architecture.ensure(ws)
    prompt = assembler.assemble(directive, workspace=ws, supplemental=supplemental_text(ws, role, mode, runtime.fleet_state),
                                transcript_dir=target_transcript_dir(ws, directive),
                                architecture_tier=spec.architecture_tier, gpu=gpu_enabled)
    if feedback:
        problems = [f"{item.get('error')}: {item.get('message')}" for item in feedback]
        prompt = dataclasses.replace(prompt, user_text=prompt.user_text + "\n\n" + assembler.render_retry(problems))
    live.note(ws, f"[{session_id[:8]}] session start: role={role} mode={mode} model={spec.model} "
                  f"provider={spec.provider} backend={spec.backend} directive={directive.get('directive_id')}"
                  + (f" retry-feedback={len(feedback)}" if feedback else "")
                  + (" gpu=granted" if gpu_service is not None else ""))
    overrides = dict(runtime.backend_overrides.get(spec.backend) or {})
    if spec.backend == "claude_code" and gpu_enabled:
        overrides.setdefault("gpu_max_job_seconds", int(gpu_config["max_job_seconds"]))
    backend = make_backend(spec, handle, docker=runtime.docker, broker=broker,
                           repo_mount="/srv/adv-loop/repo" if runtime.containers else None,
                           overrides=overrides,
                           gpu_service_factory=(lambda h, ledger_path: gpu_service) if gpu_service is not None else None)
    try:
        result = backend.run(directive, handle, spec, assignment.budget, prompt)
    finally:
        if gpu_thread:
            gpu_thread.stop()
        if thread:
            thread.stop()
    spend = budget.record_spend(ws, spec, result.usage, role=role, mode=mode, directive_id=directive.get("directive_id"),
                                session_id=session_id, base_prompt_hash=prompt.manifest["base_prompt_hash"])
    live.note(ws, f"[{session_id[:8]}] session end: {result.ended} requests={result.usage.get('requests')} "
                  f"spend=${spend.get('estimated_usd') or 0:.3f}" + (f" errors={'; '.join(result.errors)[:200]}" if result.errors else ""))
    secrets = [s for s in (spec.key_file and _secret_values(spec.key_file) or [])]
    outcome: Dict[str, Any] = {"session_id": session_id, "spec": spec, "result": result, "prompt": prompt,
                               "spend": spend, "submission": None, "problems": [], "warnings": []}
    if result.rate_limited:
        transcripts.persist(ws, result, prompt, directive=directive, model=spec.model, submission=None,
                            rejection=["rate_limited"], secrets=secrets, accepted=False, spend_usd=spend["estimated_usd"])
        resets = (result.rate_limit or {}).get("resets_at")
        if spec.billing == "subscription":
            record = pause.pause_subscription(runtime.state_dir, resets, workspace=str(ws))
            pause.pause_workspace(ws, "subscription_rate_limit", pause.parse_iso(record["resume_after"]), session_id=session_id)
        else:
            prior = (pause.read(ws / pause.PAUSE_FILE) or {}).get("count", 0)
            until = pause.resume_after(resets, prior)
            pause.pause_workspace(ws, "api_rate_limit", until, count=prior + 1, model=spec.model, session_id=session_id)
        outcome["paused"] = True
        return outcome
    if not result.ok:
        transcripts.persist(ws, result, prompt, directive=directive, model=spec.model, submission=None,
                            rejection=[_session_error(result)["message"]], secrets=secrets, accepted=False,
                            spend_usd=spend["estimated_usd"])
        outcome["problems"] = [_session_error(result)]
        return outcome
    submission, problems, warnings = session.build_submission(
        directive, result, prompt, workspace=ws, spec=spec, agent_id=runtime.agent_id,
        verdicts=broker.session_verdicts(), policy_version=prompt.manifest["policy_version"])
    outcome.update({"submission": submission, "problems": [{"error": "harness_contract", "message": p} for p in problems],
                    "warnings": warnings})
    transcripts.persist(ws, result, prompt, directive=directive, model=spec.model, submission=submission,
                        rejection=problems or None, secrets=secrets, accepted=None if not problems else False,
                        spend_usd=spend["estimated_usd"])
    return outcome


def _secret_values(key_file: str) -> List[str]:
    try:
        text = Path(key_file).read_text(encoding="utf-8")
    except OSError:
        return []
    values = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        values.append(line.split("=", 1)[1].strip().strip('"') if "=" in line else line)
    return [v for v in values if len(v) >= 8]


def run_directive(runtime: Runtime, ws: Path, max_cycles: Optional[int] = None) -> Dict[str, Any]:
    """Drive the workspace, then let it look at what just happened to itself.

    The refinement pass used to run once per fleet pass, which meant a workspace could finish several
    sessions before anything read them back. Running it here puts the improvement loop directly behind
    the work on every drive path, the console's single step included.
    """

    outcome = _drive_directive(runtime, ws, max_cycles)
    if outcome.get("sessions"):
        outcome["refined"] = refine_after_sessions(runtime, ws)
    return outcome


def refine_after_sessions(runtime: Runtime, ws: Path) -> Dict[str, Any]:
    """One local refinement pass; it is gated by its own triggers and daily cap, and never raises."""

    if not runtime.refine_enabled:
        return {"skipped": "refine disabled"}
    from . import refine
    try:
        return refine.refine_local(runtime, ws)
    except Exception as exc:  # the loop never takes a workspace down
        return {"error": str(exc)}


def _drive_directive(runtime: Runtime, ws: Path, max_cycles: Optional[int] = None) -> Dict[str, Any]:
    """Drive one workspace until `max_cycles` accepted attempts, a pause, a fault, or a terminal state."""

    engine = LoopEngine(ws)
    max_cycles = max_cycles or runtime.max_cycles_per
    accepted = 0
    rejections = 0
    sessions: List[str] = []
    while True:
        directive = engine.next_instruction()
        action = directive["action"]
        if action == "stop":
            return {"driver_status": "terminal", "accepted": accepted, "rejections": rejections, "sessions": sessions}
        if action == "finalize":
            engine.finalize()
            return {"driver_status": "terminal", "accepted": accepted, "rejections": rejections, "sessions": sessions,
                    "finalized": True}
        if action == "await_human":
            return {"driver_status": "paused", "reason": directive.get("reason"), "accepted": accepted,
                    "rejections": rejections, "sessions": sessions}
        if action != "attempt":
            return {"driver_status": "error", "reason": f"unsupported action {action}", "accepted": accepted,
                    "rejections": rejections, "sessions": sessions}
        if accepted >= max_cycles:
            return {"driver_status": "paused", "reason": "max_cycles", "accepted": accepted,
                    "rejections": rejections, "sessions": sessions}

        config = provision.read_config(ws)
        try:
            assignment = runtime.router_factory(config).resolve(directive["role"], directive["mode"])
        except routing.RoutingError as exc:
            return {"driver_status": "blocked", "reason": f"routing: {exc}", "accepted": accepted,
                    "rejections": rejections, "sessions": sessions}
        spec = assignment.spec
        if spec.billing == "subscription" and pause.subscription_paused(runtime.state_dir):
            return {"driver_status": "paused", "reason": "subscription_paused", "accepted": accepted,
                    "rejections": rejections, "sessions": sessions}
        if not runtime.api_guard.allows(spec, assignment.budget.max_budget_usd):
            pause.pause_workspace(ws, "api_budget_stop", pause.utc_now(), indefinite=True)
            return {"driver_status": "paused", "reason": "api_budget_stop", "accepted": accepted,
                    "rejections": rejections, "sessions": sessions}
        # The task's own cap sits between the per-session budget and the fleet line. Sizing it costs one
        # session, so it happens once, here, where every drive path passes rather than only the fleet pass.
        if runtime.task_budget_enabled:
            if taskbudget.read(config) is None:
                try:
                    taskbudget.plan(runtime, ws)
                except Exception as exc:  # an unpriced task is capped by the shipped default, never uncapped
                    live.note(ws, f"budget planner failed ({exc}); applying the shipped default")
                    taskbudget.apply_default(ws, str(exc))
                config = provision.read_config(ws)
            crossed = taskbudget.exceeded(ws, config)
            if crossed:
                taskbudget.hold(ws, crossed)
                return {"driver_status": "paused", "reason": pause.TASK_BUDGET_STOP, "accepted": accepted,
                        "rejections": rejections, "sessions": sessions, "task_budget": crossed}
        if gpu_module.enabled(config):
            max_hours = float(gpu_module.config_for(config)["max_hours"])
            hours_used = gpu_spend.hours_used(ws)
            if hours_used >= max_hours:
                pause.pause_gpu_budget(ws, hours_used, max_hours)
                return {"driver_status": "paused", "reason": pause.GPU_BUDGET_STOP, "accepted": accepted,
                        "rejections": rejections, "sessions": sessions}

        feedback: List[Dict[str, Any]] = []
        stale = False
        recorded = False
        for retry in range(runtime.adapter_retries + 1):
            outcome = run_session(runtime, ws, directive, feedback, assignment)
            sessions.append(outcome["session_id"])
            if outcome.get("paused"):
                return {"driver_status": "paused", "reason": "rate_limited", "accepted": accepted,
                        "rejections": rejections, "sessions": sessions, "model": spec.model}
            status = getattr(outcome.get("result"), "api_error_status", None)
            if status in (401, 403):
                # credentials, not research: no retry burst, no process fault
                pause.pause_workspace(ws, "auth_error", pause.utc_now(), indefinite=True, status=status, model=spec.model)
                return {"driver_status": "blocked", "reason": f"auth_error_{status}", "accepted": accepted,
                        "rejections": rejections, "sessions": sessions, "model": spec.model}
            if outcome["problems"]:
                live.note(ws, f"[{outcome['session_id'][:8]}] harness rejected (retry {retry}/{runtime.adapter_retries}): "
                              + "; ".join(str(p.get("message")) for p in outcome["problems"])[:300])
                feedback.extend(outcome["problems"])
                rejections += len(outcome["problems"])
                for item in outcome["problems"]:
                    _append_rejection(ws, {**item, "directive_id": directive["directive_id"],
                                           "session_id": outcome["session_id"], "retry": retry, "at": budget.utc_now()})
                if getattr(outcome.get("result"), "ended", None) == "error_prompt_too_large":
                    # the prompt cannot shrink between tries: one fault, no retry burst
                    break
                continue
            try:
                engine.record_attempt(outcome["submission"])
                recorded = True
                state = engine.load()
                transcripts.mark(ws, outcome["session_id"], accepted=True, event_head=state["integrity"]["event_head"])
                accepted += 1
                live.note(ws, f"[{outcome['session_id'][:8]}] kernel accepted attempt; phase={state.get('phase')} "
                              f"attempts={state.get('attempt_count')}")
                break
            except TransitionError as exc:
                current = engine.next_instruction()
                if current.get("directive_id") != directive.get("directive_id"):
                    stale = True
                    transcripts.mark(ws, outcome["session_id"], accepted=False, note="stale directive")
                    break
                feedback.append(exc.as_dict())
            except LoopError as exc:
                feedback.append(exc.as_dict())
            rejections += 1
            live.note(ws, f"[{outcome['session_id'][:8]}] kernel rejected (retry {retry}/{runtime.adapter_retries}): "
                          f"{feedback[-1].get('error')}: {str(feedback[-1].get('message'))[:300]}")
            transcripts.mark(ws, outcome["session_id"], accepted=False, note=feedback[-1].get("message"))
            _append_rejection(ws, {**feedback[-1], "directive_id": directive["directive_id"],
                                   "session_id": outcome["session_id"], "retry": retry, "at": budget.utc_now()})
        if stale:
            continue
        if not recorded:
            signature = fault_signature(feedback)
            live.note(ws, f"retries exhausted for {directive['directive_id']}; recording process fault {signature}")
            try:
                engine.record_process_fault({
                    "request_id": f"fault:{directive['directive_id']}",
                    "signature": signature,
                    "source": "harness:session_exhausted",
                    "detail": str(feedback[-1].get("message", "session failure"))[:200],
                })
            except LoopError:
                pass
            return {"driver_status": "fault", "fault_signature": signature, "accepted": accepted,
                    "rejections": rejections, "sessions": sessions, "feedback": feedback}


def provision_workspace(runtime: Runtime, ws: Path) -> Dict[str, Any]:
    """Container, hooks, key, lock, then the sandbox init in a background thread."""

    ready = ws / READY_FILE
    if ready.is_file():
        return json.loads(ready.read_text(encoding="utf-8"))
    gpu_lock = gpu_lock_module.read_fleet_lock(runtime.state_dir) if gpu_module.enabled(provision.read_config(ws)) else None
    if runtime.containers:
        report = provision.provision(ws, runtime.docker, image=runtime.image or provision.ct.DEFAULT_IMAGE, repo=runtime.repo,
                                     gpu_lock=gpu_lock)
    else:
        report = provision.provision_local(ws, repo=runtime.repo, gpu_lock=gpu_lock)
    marker = {"provisioned_at": budget.utc_now(), "sandbox_init": "pending",
              **{k: report[k] for k in ("container", "hooks", "gpu") if k in report}}
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def init() -> None:
        try:
            outcome = sandbox_report(ws, init=True)
            marker["sandbox_init"] = "ok" if outcome.get("exit_code") == 0 else "failed"
            marker["sandbox"] = {k: outcome.get(k) for k in ("lockfiles", "setup", "exit_code")}
        except Exception as exc:  # a broken sandbox is a workspace problem, never a supervisor crash
            marker["sandbox_init"] = "failed"
            marker["error"] = str(exc)
        ready.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    threading.Thread(target=init, name=f"sandbox-init-{ws.name}", daemon=True).start()
    return marker


def workspace_ready(ws: Path) -> bool:
    ready = ws / READY_FILE
    if not ready.is_file():
        return False
    try:
        return json.loads(ready.read_text(encoding="utf-8")).get("sandbox_init") == "ok"
    except ValueError:
        return False


def pass_once(runtime: Runtime) -> Dict[str, Any]:
    started = time.monotonic()
    intake: List[Dict[str, Any]] = []
    if runtime.inbox and runtime.inbox.is_dir():
        intake = process_inbox(runtime.inbox, runtime.root)
    for ws in sorted(p for p in runtime.root.iterdir() if p.is_dir() and (p / "events.jsonl").is_file()):
        if not (ws / READY_FILE).is_file():
            try:
                provision_workspace(runtime, ws)
            except Exception as exc:
                (ws / ".harness").mkdir(exist_ok=True)
                (ws / ".harness" / "provision-error.txt").write_text(str(exc), encoding="utf-8")
    gpu_report = _gpu_pass(runtime)
    runtime.api_guard.refresh(runtime.root, extra_ledgers=[runtime.state_dir / ".spend.jsonl"])
    report = fleet_report(runtime.root, dry_run=True, adapter=["/bin/true"],
                          spend_ceiling_usd=runtime.fleet_spend_ceiling_usd)
    candidates = []
    skipped: Dict[str, str] = {}
    for row in report["workspaces"]:
        ws = Path(row["workspace"])
        if not row.get("drivable") or row.get("driven") is False:
            skipped[ws.name] = row.get("note") or row.get("status", "not drivable")
            continue
        if pause.workspace_paused(ws):
            skipped[ws.name] = "paused"
            continue
        if not workspace_ready(ws):
            skipped[ws.name] = "provisioning"
            continue
        if runtime.aws_guard and not runtime.aws_guard.allows_sessions():
            skipped[ws.name] = "aws_budget_stop"
            continue
        candidates.append((row.get("priority", 1), row.get("workspace"), ws))
    candidates.sort(key=lambda item: (item[0], item[1]))
    results: Dict[str, Dict[str, Any]] = {}
    parallel = runtime.api_guard.max_parallel(runtime.max_parallel)
    if candidates:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {ws.name: pool.submit(run_directive, runtime, ws) for _p, _k, ws in candidates}
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except Exception as exc:  # one bad workspace stays one bad row
                    results[name] = {"driver_status": "error", "reason": str(exc)}
    # Refinement now rides with the directive (see run_directive), so the pass only collects what it did.
    refined: Dict[str, Dict[str, Any]] = {
        name: row["refined"] for name, row in results.items()
        if isinstance(row, dict) and row.get("refined")
    }
    idle = not candidates and not intake and not gpu_report.get("active_jobs") and not any(
        (ws / READY_FILE).is_file() and not workspace_ready(ws) and _pending_init(ws)
        for ws in runtime.root.iterdir() if ws.is_dir())
    return {"intake": intake, "driven": results, "skipped": skipped, "refined": refined, "idle": idle,
            "spend_usd": report["spend_usd"], "spend_stopped": report["spend_stopped"],
            "api_state": runtime.api_guard.state(), "gpu": gpu_report,
            "elapsed_seconds": round(time.monotonic() - started, 1)}


def _gpu_pass(runtime: Runtime) -> Dict[str, Any]:
    """Settle orphaned jobs and let the reaper stop an idle box; a GPU fault never takes the pass down."""

    if runtime.gpu is None:
        return {"enabled": False, "active_jobs": []}
    report: Dict[str, Any] = {"enabled": True}
    try:
        report["sweep"] = gpu_jobs.sweep(runtime.gpu, runtime.root, runtime.state_dir)
    except Exception as exc:
        report["sweep_error"] = f"{type(exc).__name__}: {exc}"
    try:
        report["reap"] = runtime.gpu.reap()
    except Exception as exc:
        report["reap_error"] = f"{type(exc).__name__}: {exc}"
    report["active_jobs"] = [job.get("job_id") for job in runtime.gpu_active_jobs()]
    return report


def _pending_init(ws: Path) -> bool:
    try:
        return json.loads((ws / READY_FILE).read_text(encoding="utf-8")).get("sandbox_init") == "pending"
    except (OSError, ValueError):
        return False


def serve(runtime: Runtime, *, max_passes: Optional[int] = None, sleep=time.sleep,
          log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Pass until stopped: `.fleet-stop`, `max_passes`, or an idle window long enough to power off."""

    idle_since: Optional[float] = None
    passes = 0
    while True:
        if (runtime.root / FLEET_STOP).is_file():
            log("fleet-stop present; exiting")
            stopped = runtime.stop_gpu_box("fleet_stop")
            if stopped is not None:
                log(json.dumps({"gpu_box": stopped}))
            return {"reason": "fleet_stop", "passes": passes}
        outcome = pass_once(runtime)
        passes += 1
        log(json.dumps({"pass": passes, "driven": {k: v.get("driver_status") for k, v in outcome["driven"].items()},
                        "skipped": outcome["skipped"], "idle": outcome["idle"], "api": outcome["api_state"].get("api_spend_usd")}))
        if refine_global.scheduled(runtime, passes, outcome["idle"]):
            try:
                summary = refine_global.run(runtime, fleet_state=runtime.fleet_state)
                log(json.dumps({"global_refine": {k: summary.get(k) for k in ("skipped", "null", "applied", "rejected_by", "pr_bundle", "aborted") if k in summary}}))
            except Exception as exc:  # the global loop never takes the supervisor down
                log(f"global refine failed: {exc}")
        if max_passes is not None and passes >= max_passes:
            return {"reason": "max_passes", "passes": passes}
        if outcome["idle"]:
            idle_since = idle_since or time.monotonic()
            if runtime.shutdown_command and time.monotonic() - idle_since >= runtime.idle_stop_minutes * 60:
                active = runtime.gpu_active_jobs()
                if active:
                    # a job started by another process still runs on the box; the controller must outlive it
                    log(json.dumps({"idle_stop_deferred": [job.get("job_id") for job in active]}))
                    idle_since = None
                    sleep(runtime.pass_interval_seconds)
                    continue
                stopped = runtime.stop_gpu_box("harness_idle_stop")
                if stopped is not None:
                    log(json.dumps({"gpu_box": stopped}))
                wake = _next_wake(runtime)
                (runtime.state_dir / WAKE_FILE).write_text(json.dumps({"wake_at": wake, "written_at": budget.utc_now()}) + "\n")
                log(f"idle for {runtime.idle_stop_minutes} minutes; wake_at={wake}; shutting down")
                subprocess.run(runtime.shutdown_command, check=False)
                return {"reason": "idle_stop", "passes": passes, "wake_at": wake}
        else:
            idle_since = None
        sleep(runtime.pass_interval_seconds)


def _next_wake(runtime: Runtime) -> Optional[str]:
    """Earliest pause expiry across the fleet, so a wake job can start the box in time."""

    candidates = []
    sub = pause.read(runtime.state_dir / pause.SUBSCRIPTION_PAUSE)
    if sub and pause.is_active(runtime.state_dir / pause.SUBSCRIPTION_PAUSE):
        candidates.append(sub["resume_after"])
    for ws in runtime.root.iterdir():
        record = pause.read(ws / pause.PAUSE_FILE) if ws.is_dir() else None
        if record and not record.get("indefinite") and pause.is_active(ws / pause.PAUSE_FILE):
            candidates.append(record["resume_after"])
    return min(candidates) if candidates else None
