"""End-to-end drill: a throwaway workspace driven through the real kernel.

Every check is a named assertion over what the harness actually did, so a
drill on a fresh box proves the mount, the ledger, the fingerprints, the
broker, the spend row, and the pause path before a real task is dropped.
With `gpu=True` the runtime's box (the test fake, or the live box) carries one
job end to end: the grant, the lock, the pull, the attestation, the kernel's
acceptance, and the spend ledgers are each asserted by name.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

from adv_loop.engine import LoopEngine
from adv_loop.runner import workspace_obligations
from adv_loop.validators import validate_report

from . import budget, ledger, pause, provision, routing, supervisor, tools, transcripts
from . import gpu as gpu_module
from .backends.base import ModelSpec, SessionBudget
from .backends.minimal import GPU_OUTPUT
from .gpu import jobs as gpu_jobs
from .gpu import lock as gpu_lock

CRITERION = {"text": "A script under payload/scratch prints DRILL-OK and its output is replayed by the cert-replay checker",
             "min_formalization_rank": "executable_spec"}
GPU_GRANT = {"enabled": True, "max_hours": 2.0, "queue_wait_seconds": 0}
UNDECLARED = "payload/scratch/undeclared.txt"
EVIDENCE = {"ref": "e1", "kind": "artifact", "quality": "direct", "claim": "c", "method": "m",
            "independence_key": "k", "supports": ["C1"]}


def _spec_for(backend: str, simulate_rate_limit: bool, alias: Optional[str] = None) -> Dict[str, Any]:
    if backend == "http_chat":
        # a live drill: the model comes from the fleet routing table, the credential from the environment
        table = routing.load_defaults()["models"]
        spec = dict(table[alias or "grok"])
        spec["key_file"] = os.environ.get("ADV_DRILL_AZURE_KEY_FILE", spec.get("key_file"))
        spec["auth_header"] = os.environ.get("ADV_DRILL_AZURE_AUTH", spec.get("auth_header", "api-key"))
        return spec
    if backend == "minimal":
        return {"backend": "minimal", "model": "none", "provider": "local", "billing": "local"}
    if backend == "claude_code":
        return {"backend": "claude_code", "model": "claude-opus-5", "provider": "anthropic_subscription",
                "wire": "anthropic", "billing": "subscription", "effort": "high",
                "key_file": "/etc/adv-loop/claude-oauth.token"}
    raise ValueError(f"unknown drill backend {backend}")


class Check:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: Any = None) -> None:
        self.rows.append({"check": name, "ok": bool(ok), "detail": detail})

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.rows)


def _sha(path: Path) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _gpu_prepare(runtime: supervisor.Runtime, ws: Path) -> None:
    """Grant the drill workspace GPU compute and make the fleet lock describe the runtime's box."""

    if runtime.gpu is None:
        raise ValueError("drill --gpu needs a box on the runtime (the test fake or the live controller)")
    config = provision.read_config(ws)
    config.setdefault("harness", {})["gpu"] = dict(GPU_GRANT)
    provision.write_config(ws, config)
    if gpu_lock.read_fleet_lock(runtime.state_dir) is None:
        image = getattr(getattr(runtime.gpu, "config", None), "image", None) or gpu_module.DEFAULTS["image"]
        try:
            observed = gpu_lock.observe(runtime.gpu, image=image)
        except Exception as exc:
            raise RuntimeError(f"no fleet GPU lock and the box could not be observed ({exc}); run"
                               " adv-harness gpu provision-box first") from exc
        gpu_lock.write_fleet_lock(runtime.state_dir, observed)
    mirror = getattr(runtime.gpu, "mirror", None)
    if callable(mirror):
        # a file on the fake box's mirror that no job declares; the pull must leave it there
        planted = mirror(ws) / UNDECLARED
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("never pulled\n", encoding="utf-8")


def _gpu_checks(runtime: supervisor.Runtime, ws: Path, check: Check, config: Dict[str, Any],
                ledger_entries: List[Dict[str, Any]], evidence: List[Dict[str, Any]],
                last_attempt: Dict[str, Any], spend_rows: List[Dict[str, Any]]) -> None:
    block = (config.get("harness") or {}).get("gpu") or {}
    ws_lock = gpu_lock.read_workspace_lock(ws)
    hashes = (config.get("sandbox") or {}).get("lockfile_hashes") or {}
    validators = config.get("validators") or {}
    budgets = (config.get("harness") or {}).get("budgets") or {}
    check.add("gpu_provisioned_with_lock_and_hooks",
              block.get("enabled") is True and ws_lock is not None and ws_lock.toolchain_hash == block.get("toolchain_hash")
              and hashes.get(gpu_module.WORKSPACE_LOCK_FILE) == gpu_lock.lock_sha256(ws)
              and all(validators.get(h, {}).get("in_sandbox") is False for h in provision.GPU_CHECKERS)
              and all(key in budgets for key in provision.GPU_BUDGET_KEYS),
              {"toolchain_hash": block.get("toolchain_hash"), "hooks": sorted(validators), "budgets": sorted(budgets)})

    plain = {d["name"] for d in tools.tool_definitions()}
    granted = {d["name"] for d in tools.tool_definitions(gpu=True)}
    probe = tools.ToolRunner(ws, runtime.state_dir / "drill-tool-probe.jsonl")
    refused = probe.run("gpu_run", {"command": ["true"], "outputs": [GPU_OUTPUT]}, tool_use_id="probe")
    check.add("gpu_tools_advertised_only_when_granted",
              not (plain & set(gpu_module.TOOL_NAMES)) and set(gpu_module.TOOL_NAMES) <= granted
              and "not granted" in str(refused.get("error")) and bool(probe.entries and probe.entries[-1].get("error")),
              {"plain": sorted(plain), "granted": sorted(granted), "refusal": refused.get("error")})

    names = [e.get("tool") for e in ledger_entries]
    job_rows = [e for e in ledger_entries if e.get("tool") == "gpu_run"]
    job = (job_rows[0].get("job") or {}) if job_rows else {}
    job_id = job.get("job_id")
    position = names.index("gpu_run") if "gpu_run" in names else -1
    check.add("gpu_job_ran_between_turns",
              bool(job_rows) and job.get("status") == "collected" and job_rows[0].get("exit_code") == 0
              and 0 < position and "validate" in names[position + 1:],
              {"tools": names, "job_id": job_id, "status": job.get("status")})

    record = gpu_jobs.JobStore(ws).read(job_id) if job_id else None
    out = ws / GPU_OUTPUT
    copy = ws / ".harness" / "gpu" / "jobs" / str(job_id) / "outputs" / GPU_OUTPUT
    recorded = record.outputs[0] if record and record.outputs else {}
    check.add("declared_outputs_pulled_and_hashed",
              record is not None and record.outputs_declared == [GPU_OUTPUT] and recorded.get("path") == GPU_OUTPUT
              and out.is_file() and _sha(out) == recorded.get("sha256") == _sha(copy),
              {"output": GPU_OUTPUT, "sha256": recorded.get("sha256")})

    pulled = getattr(runtime.gpu, "pulled", None)
    allowed = {GPU_OUTPUT, f".harness/gpu/jobs/{job_id}"}
    only_declared = record is not None and {o["path"] for o in record.outputs} <= set(record.outputs_declared)
    if isinstance(pulled, list):
        only_declared = only_declared and set(pulled) <= allowed and not (ws / UNDECLARED).exists()
        detail: Any = {"pulled": sorted(set(pulled)), "planted_stayed_on_box": not (ws / UNDECLARED).exists()}
    else:
        detail = {"pulled": [o["path"] for o in record.outputs] if record else None, "note": "record-level check on a live box"}
    check.add("undeclared_files_not_pulled", only_declared, detail)

    _, problems, warnings = ledger.verify_evidence(ws, [{**EVIDENCE, "artifact_path": GPU_OUTPUT}], ledger=ledger_entries)
    unobserved = [u for u in (last_attempt.get("uncertainties") or []) if "not observed" in str(u)]
    check.add("evidence_from_gpu_output_carries_no_unobserved_warning", not problems and not warnings and not unobserved,
              {"problems": problems, "warnings": warnings, "uncertainties": unobserved})

    report = validate_report(ws, "gpu-replay", {"job_id": job_id, "artifact": GPU_OUTPUT}) if job_id else {}
    result = report.get("result") or {}
    check.add("gpu_replay_verdict_signed_with_toolchain_hash",
              report.get("ok") is True and result.get("accepted") is True and len(result.get("attestation", "")) == 64
              and result.get("toolchain_hash") == block.get("toolchain_hash"),
              {k: result.get(k) for k in ("accepted", "toolchain_hash", "checker_id")})

    tampered: Dict[str, Any] = {}
    if out.is_file():
        original = out.read_bytes()
        out.write_bytes(original + b"tampered\n")
        try:
            tampered = validate_report(ws, "gpu-replay", {"job_id": job_id, "artifact": GPU_OUTPUT})
        finally:
            out.write_bytes(original)
    problems_seen = ((tampered.get("result") or {}).get("details") or {}).get("problems") or []
    check.add("tampered_output_rejected_by_gpu_replay",
              tampered.get("ok") is True and (tampered.get("result") or {}).get("accepted") is False
              and any("changed since collection" in str(p) for p in problems_seen), problems_seen)

    attested = [item for item in evidence if (item.get("checker") or {}).get("checker_id") == "gpu-replay"]
    item = attested[-1] if attested else {}
    check.add("kernel_accepted_attempt_with_gpu_verdict",
              bool(item) and item["checker"].get("accepted") is True and item.get("formalization_rank") == "executable_spec"
              and str(item.get("locator", "")).startswith("validate:gpu-replay")
              and item["checker"].get("toolchain_hash") == block.get("toolchain_hash"),
              {"locator": item.get("locator"), "rank": item.get("formalization_rank")})

    ws_rows = budget.gpu_spend_rows(ws / gpu_module.GPU_SPEND_FILE, "job")
    fleet_rows = budget.gpu_spend_rows(runtime.state_dir / gpu_module.STATE_SPEND_FILE, "job")
    check.add("gpu_spend_in_gpu_ledgers_not_in_api_ledger",
              any(r.get("job_id") == job_id for r in ws_rows) and any(r.get("job_id") == job_id for r in fleet_rows)
              and bool(spend_rows) and all("job_id" not in r and r.get("estimated_usd") == 0.0 for r in spend_rows),
              {"workspace_rows": len(ws_rows), "fleet_rows": len(fleet_rows), "api_rows": len(spend_rows)})

    templates = ws / ".harness" / "overlay-templates"
    try:
        pins = json.loads((templates / "pin-toolchain.json").read_text(encoding="utf-8"))["ops"]
        sandbox = json.loads((templates / "register-gpu-sandbox.json").read_text(encoding="utf-8"))["ops"]
    except (OSError, ValueError, KeyError):
        pins, sandbox = [], []
    gpu_pins = {op.get("toolchain_id"): op.get("artifact_hash") for op in pins if op.get("toolchain_id") in provision.GPU_CHECKERS}
    check.add("pin_template_includes_gpu_checkers",
              set(gpu_pins) == set(provision.GPU_CHECKERS) and all(h == block.get("toolchain_hash") for h in gpu_pins.values())
              and bool(sandbox) and sandbox[0].get("op") == "register_sandbox" and sandbox[0].get("sandbox_id") == "gpu-box",
              {"pins": gpu_pins, "sandbox": sandbox})


def run(runtime: supervisor.Runtime, *, backend: str = "minimal", simulate_rate_limit: bool = False,
        lie: bool = False, fake_claude: Optional[Path] = None, alias: Optional[str] = None,
        gpu: bool = False) -> Dict[str, Any]:
    if gpu and backend != "minimal":
        raise ValueError("drill --gpu runs on the minimal backend")
    check = Check()
    name = f"drill-{secrets.token_hex(4)}"
    engine = LoopEngine.create(runtime.root, "Drill: prove the harness plumbing on this box", [CRITERION], {}, task_id=name)
    ws = engine.workspace
    if gpu:
        _gpu_prepare(runtime, ws)
    marker = supervisor.provision_workspace(runtime, ws)
    check.add("provisioned", (ws / ".checker-key").is_file() and (ws / "loop-config.json").is_file(), marker)

    state = engine.load()
    owed = workspace_obligations(state, engine.supervision(state))
    check.add("guard_owes_a_directive_before_work", bool(owed), [o.get("kind") or o for o in owed][:2])

    config = provision.read_config(ws)
    main_spec = _spec_for(backend, simulate_rate_limit, alias)
    # The drill's reviewer has to be a different model from the drill's worker, the same way the fleet's is.
    reviewer = "deepseek" if (alias or "grok") != "deepseek" else "grok"
    other = {**_spec_for(backend, False, alias), "model": "drill-critic"} if backend != "http_chat" else \
        {**_spec_for(backend, False, reviewer)}
    config.setdefault("harness", {})["roles"] = {"default": {"alias": alias or "drill", **main_spec},
                                                 "critic": {"alias": "drill-critic", **other},
                                                 "verifier": {"alias": "drill-verifier", **other}}
    provision.write_config(ws, config)
    if backend == "minimal":
        runtime.backend_overrides["minimal"] = {"lie": lie, "gpu": gpu}
    if backend == "claude_code":
        overrides = {"claude_binary": [str(fake_claude or Path(__file__).resolve().parent / "container" / "fake_claude.py")],
                     "repo_mount": None, "hook_path": str(Path(__file__).resolve().parent / "container" / "ledger_hook.py"),
                     "token_reader": lambda s: "drill-token",
                     "env": {"PATH": __import__("os").environ.get("PATH", ""), "HOME": str(runtime.state_dir),
                             "FAKE_CLAUDE_SCENARIO": "rate_limit" if simulate_rate_limit else "ok"}}
        overrides["claude_binary"] = [__import__("sys").executable] + overrides["claude_binary"]
        runtime.backend_overrides["claude_code"] = overrides
    if backend == "http_chat":
        runtime.router_factory = lambda cfg: routing.Router(cfg, prices=routing.load_prices(), token_exists=lambda p: True)
    else:
        runtime.router_factory = lambda cfg: routing.Router(cfg, prices={}, env={"AZURE_OPENAI_ENDPOINT": "https://drill.invalid"},
                                                            token_exists=lambda p: True)

    outcome = supervisor.run_directive(runtime, ws, max_cycles=1)
    check.add("planner_directive_driven", outcome["driver_status"] in ("paused", "terminal") and outcome.get("accepted", 0) >= 1
              or (simulate_rate_limit and outcome["driver_status"] == "paused"), outcome)
    if simulate_rate_limit:
        check.add("rate_limit_pauses_without_a_chain_event",
                  pause.workspace_paused(ws) and pause.subscription_paused(runtime.state_dir)
                  and len(outcome["sessions"]) == 1 and not (ws / ".harness" / "rejections.jsonl").exists(), outcome)
        spend = budget.ledger_total(ws / ".spend.jsonl")
        check.add("no_spend_under_rate_limit", spend == 0.0, spend)
        state = engine.load()
        check.add("no_fault_recorded", not state.get("process_faults"), state.get("process_faults"))
        return {"workspace": str(ws), "ok": check.ok, "checks": check.rows}

    if outcome.get("accepted", 0) >= 1:
        outcome = supervisor.run_directive(runtime, ws, max_cycles=1)
    directive_mode = None
    rows = transcripts.load_index(ws)
    if rows:
        directive_mode = rows[-1].get("mode")
    if lie:
        check.add("lying_fingerprint_rejected_before_the_kernel",
                  outcome["driver_status"] == "fault" and any("does not match" in f.get("message", "") for f in outcome.get("feedback", [])),
                  outcome.get("feedback"))
        return {"workspace": str(ws), "ok": check.ok, "checks": check.rows}

    check.add("researcher_directive_driven", directive_mode == "experiment" and outcome.get("accepted", 0) >= 1, outcome)
    state = engine.load()
    stored_attempts = state.get("attempts") or []
    attempts = list(stored_attempts.values()) if isinstance(stored_attempts, dict) else list(stored_attempts)
    last = attempts[-1] if attempts else {}
    session_id = rows[-1]["session_id"] if rows else None
    check.add("fresh_session_id_is_the_context_id", bool(session_id) and last.get("actor", {}).get("context_id") == session_id,
              {"context_id": last.get("actor", {}).get("context_id"), "session": session_id})
    ledger_entries = transcripts._read_ledger(transcripts.session_dir(ws, session_id)) if session_id else []
    observation = last.get("observation", "")
    check.add("observation_derived_from_the_ledger",
              bool(ledger_entries) and all(str(e.get("tool_use_id")) in observation for e in ledger_entries), len(ledger_entries))
    stored = state.get("evidence") or []
    evidence = list(stored.values()) if isinstance(stored, dict) else list(stored)
    if evidence:
        # Every item must carry a host-computed fingerprint: a signed checker record (accepted or not, a
        # live model may run a negative control) or the sha256 of the named file.
        problems = []
        for item in evidence:
            if str(item.get("locator", "")).startswith("validate:"):
                checker = item.get("checker") or {}
                good = (item.get("fingerprint") == checker.get("artifact_hash")
                        and len(checker.get("attestation", "")) == 64)
            else:
                path = ws / str(item.get("locator", ""))
                good = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == item.get("fingerprint")
            if not good:
                problems.append(item.get("locator"))
        check.add("evidence_fingerprint_recomputed_on_host", not problems,
                  problems or [e.get("locator") for e in evidence])
    else:
        check.add("evidence_fingerprint_recomputed_on_host", False, "no evidence recorded")
    spend_file = ws / ".spend.jsonl"
    spend_rows = [json.loads(l) for l in spend_file.read_text().splitlines() if l.strip()] if spend_file.is_file() else []
    expected_zero = main_spec["billing"] != "api"
    check.add("spend_row_matches_billing", bool(spend_rows) and ((spend_rows[-1]["estimated_usd"] == 0.0) == expected_zero), spend_rows[-1] if spend_rows else None)
    transcript_dir = transcripts.session_dir(ws, session_id) if session_id else None
    check.add("transcript_persisted", bool(transcript_dir) and (transcript_dir / "submission.json").is_file()
              and json.loads((transcript_dir / "submission.json").read_text())["accepted"] is True, str(transcript_dir))

    (ws / "payload" / "scratch").mkdir(parents=True, exist_ok=True)
    script = ws / "payload" / "scratch" / "drill.sh"
    script.write_text("#!/bin/sh\necho DRILL-OK\n", encoding="utf-8")
    report = validate_report(ws, "cert-replay", {"command": ["sh", "payload/scratch/drill.sh"], "artifact": "payload/scratch/drill.sh"})
    check.add("checker_verdict_signed_via_broker_path", report.get("ok") and report["result"].get("accepted")
              and len(report["result"].get("attestation", "")) == 64, {k: report.get(k) for k in ("ok", "exit_code")})
    if runtime.containers:
        check.add("verdict_carries_toolchain_hash", report["result"].get("toolchain_hash") == config["harness"].get("toolchain_hash"),
                  report["result"].get("toolchain_hash"))
    if gpu:
        _gpu_checks(runtime, ws, check, provision.read_config(ws), ledger_entries, evidence, last, spend_rows)
    audit = engine.audit() if hasattr(engine, "audit") else {"ok": True}
    check.add("audit_clean", bool(audit.get("ok", True)), {k: audit.get(k) for k in ("ok", "exit_code") if k in audit})
    return {"workspace": str(ws), "ok": check.ok, "checks": check.rows}
