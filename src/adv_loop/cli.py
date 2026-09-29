from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .commons import commons_report
from .engine import CHECKER_KEY_FILE, LoopEngine
from .driver import SubprocessAdapter, drive
from .errors import LoopError
from .frontier import frontier_report
from .mirror import mirror_report
from .intake import intake_report
from .runner import fleet_report, fleet_watch, workspace_obligations
from .sandbox import sandbox_report
from .validators import validate_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adv-loop",
        description="Durable, proof-gated control plane for persistent agent work.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create an event-sourced task workspace")
    init.add_argument("task")
    criteria_source = init.add_mutually_exclusive_group()
    criteria_source.add_argument("--criterion", action="append", dest="criteria")
    criteria_source.add_argument(
        "--criteria-file", type=Path,
        help="JSON array of criteria; items are strings or {text, min_formalization_rank} (4.0)",
    )
    init.add_argument("--control", action="append", dest="controls",
                      help="Negative-control object a claim must NOT certify (repeatable)")
    init.add_argument("--root", type=Path, default=Path("workspaces"))
    init.add_argument("--task-id")
    init.add_argument("--policy", choices=["5.0", "4.0", "3.0"],
                      help="Policy version to pin into the workspace (default: the current version)")
    init.add_argument("--charter", type=Path,
                      help="Charter file: hashed into task_created (5.0) and copied to payload/charter.md")
    init.add_argument("--max-attempts", type=int)
    init.add_argument("--max-failures", type=int)
    init.add_argument("--deadline", help="Hard ISO-8601 deadline with timezone")

    intake = sub.add_parser(
        "intake",
        help="Sort a drop (dir, archive, file, or '-' brief) into isolated task workspaces",
    )
    intake.add_argument("source", help="Directory, archive, single file, or '-' to read a text brief from stdin")
    intake.add_argument("--root", type=Path, default=Path("workspaces"))
    intake_mode = intake.add_mutually_exclusive_group()
    intake_mode.add_argument("--dry-run", action="store_true", help="Propose the partition only (default)")
    intake_mode.add_argument("--apply", action="store_true", help="Create the proposed workspaces and copy payloads")
    intake.add_argument("--link", action="store_true", help="Hardlink payload files from a directory drop when possible")
    intake.add_argument("--max-files", type=int, default=10000)
    intake.add_argument("--max-bytes", type=int, default=500_000_000)
    intake.add_argument("--timeout", type=float, default=600.0, help="Classifier adapter timeout in seconds")
    intake.add_argument("adapter", nargs="*",
                        help="Optional classifier command after --; reads JSON stdin and writes a proposal")

    migrate = sub.add_parser("migrate", help="Preserve a v1 workspace and create a proof-clean v2 continuation")
    migrate.add_argument("source", type=Path)
    migrate.add_argument("--root", type=Path)
    migrate.add_argument("--task-id")

    status = sub.add_parser("status", help="Replay and print authoritative state plus supervision info")
    status.add_argument("workspace", type=Path)

    nxt = sub.add_parser("next", help="Print the currently legal next directive")
    nxt.add_argument("workspace", type=Path)
    nxt.add_argument("--explore", action="store_true",
                     help="Return the voluntary ideation alternate when it is legal")

    scaffold = sub.add_parser("scaffold", help="Print a submission skeleton for the next directive")
    scaffold.add_argument("workspace", type=Path)
    scaffold.add_argument("--explore", action="store_true",
                          help="Scaffold the voluntary ideation alternate when it is legal")

    record = sub.add_parser("record", help="Validate and atomically record one attempt JSON file")
    record.add_argument("workspace", type=Path)
    record.add_argument("result", type=Path)

    audit = sub.add_parser("audit", help="Verify the event chain and all materialized projections")
    audit.add_argument("workspace", type=Path)
    audit.add_argument("--repair", action="store_true", help="Recover pending writes and rebuild projections")

    finalize = sub.add_parser("finalize", help="Complete only after every proof gate passes")
    finalize.add_argument("workspace", type=Path)
    finalize.add_argument("--request-id")

    blocked = sub.add_parser("block", help="Apply the evidence-backed blocked terminal gate")
    blocked.add_argument("workspace", type=Path)
    blocked.add_argument("decision", type=Path)

    unsafe = sub.add_parser("unsafe", help="Stop at a stated safety or authorization boundary")
    unsafe.add_argument("workspace", type=Path)
    unsafe.add_argument("decision", type=Path)

    guard = sub.add_parser(
        "guard",
        help="Exit 0 only when every workspace is terminal, awaiting_human, or obligation-free",
    )
    guard.add_argument("workspaces", type=Path, nargs="+")

    fleet = sub.add_parser(
        "fleet",
        help="Assess every workspace under a root and drive the ones that owe work, N at a time",
    )
    fleet.add_argument("--root", type=Path, default=Path("workspaces"))
    fleet.add_argument("--max-parallel", type=int, default=1)
    fleet.add_argument("--max-cycles-per", type=int, default=1,
                       help="Accepted attempts per workspace per pass")
    fleet.add_argument("--adapter-retries", type=int, default=3)
    fleet.add_argument("--timeout", type=float, default=600.0)
    fleet.add_argument("--dry-run", action="store_true",
                       help="Report the schedule without launching any drives")
    fleet.add_argument("--watch", action="store_true",
                       help="Daemon mode: keep passing until quiescent, killed (<root>/.fleet-stop), or capped")
    fleet.add_argument("--interval", type=float, default=60.0,
                       help="Seconds between watch passes")
    fleet.add_argument("--max-passes", type=int,
                       help="Stop the watch after this many passes")
    fleet.add_argument("--inbox", type=Path,
                       help="Directory of drops to auto-intake before each pass")
    fleet.add_argument("--spend-ceiling-usd", type=float,
                       help="Stop driving when the summed adapter spend ledgers reach this")
    fleet.add_argument("adapter", nargs="*",
                       help="Fallback adapter command after --; per-workspace loop-config adapters win")

    retest = sub.add_parser(
        "retest",
        help="Report blocked-premise and wish rechecks that are due; --record files an outcome",
    )
    retest.add_argument("workspace", type=Path)
    retest.add_argument("--record", type=Path,
                        help="JSON: {request_id, outcome: premise_holds|premise_no_longer_holds, note[, recheck_after]}")

    unblock = sub.add_parser("unblock", help="Revive a blocked task whose blocking premise no longer holds")
    unblock.add_argument("workspace", type=Path)
    unblock.add_argument("decision", type=Path)

    ask = sub.add_parser("ask-human", help="Pause honestly on a recorded question to the operator")
    ask.add_argument("workspace", type=Path)
    ask.add_argument("decision", type=Path)

    answer = sub.add_parser("answer-human", help="Record the operator's answer and resume the task")
    answer.add_argument("workspace", type=Path)
    answer.add_argument("decision", type=Path)

    add_criterion = sub.add_parser("add-criterion", help="Add a strictly additive acceptance criterion (3.0)")
    add_criterion.add_argument("workspace", type=Path)
    add_criterion.add_argument("decision", type=Path)

    fulfill = sub.add_parser("fulfill-wish", help="Mark a recorded wish fulfilled with a note (3.0)")
    fulfill.add_argument("workspace", type=Path)
    fulfill.add_argument("decision", type=Path)

    record_fault = sub.add_parser(
        "record-fault",
        help="Record one exhausted burst of a repeating process fault (4.0; task stays active)",
    )
    record_fault.add_argument("workspace", type=Path)
    record_fault.add_argument("decision", type=Path,
                              help="JSON: {request_id, signature (sha256 hex), source, detail}")

    commons = sub.add_parser(
        "commons",
        help="Export every workspace's assets, lessons, and completed reports into a citable commons dir",
    )
    commons.add_argument("--root", type=Path, default=Path("workspaces"))
    commons.add_argument("--out", type=Path, required=True)

    frontier = sub.add_parser(
        "frontier",
        help="Ask a generator adapter to propose the fleet's next problems as inbox drops",
    )
    frontier.add_argument("--root", type=Path, default=Path("workspaces"))
    frontier.add_argument("--out", type=Path, help="Inbox directory the accepted proposals are written into")
    frontier.add_argument("--max-proposals", type=int, default=5)
    frontier.add_argument("--apply", action="store_true",
                          help="Write the proposals as drops (default: dry-run print only)")
    frontier.add_argument("--timeout", type=float, default=600.0)
    frontier.add_argument("adapter", nargs="+",
                          help="Generator command after --; reads the fleet-signals envelope, writes {proposals}")

    sandbox = sub.add_parser(
        "sandbox",
        help="Report the workspace payload/sandbox dirs; --init creates them and runs the declared setup",
    )
    sandbox.add_argument("workspace", type=Path)
    sandbox.add_argument("--init", action="store_true",
                         help="Create payload/ and the sandbox root, then run the declared setup argv")
    sandbox.add_argument("--timeout", type=float, help="Override the declared setup timeout in seconds")

    keygen = sub.add_parser(
        "keygen",
        help="Create the workspace attestation key so validate verdicts are signed and enforceable",
    )
    keygen.add_argument("workspace", type=Path)

    mirror = sub.add_parser(
        "mirror",
        help="Append-only sync of the event log to a mirror directory; divergence is refused loudly",
    )
    mirror.add_argument("workspace", type=Path)
    mirror.add_argument("--to", type=Path, required=True, dest="destination")

    validate = sub.add_parser(
        "validate",
        help="Run a registered checker hook and print its attested verdict with an evidence hint",
    )
    validate.add_argument("workspace", type=Path)
    validate.add_argument("hook", help="Hook name from loop-config validators or an adopted overlay")
    validate.add_argument("--input", type=Path, help="JSON object passed to the hook envelope")
    validate.add_argument("--timeout", type=float, help="Override the hook timeout in seconds")

    run = sub.add_parser("drive", help="Continuously feed directives to a JSON subprocess adapter")
    run.add_argument("workspace", type=Path)
    run.add_argument("--max-cycles", type=int)
    run.add_argument("--adapter-retries", type=int, default=3)
    run.add_argument("--timeout", type=float, default=600.0)
    run.add_argument("adapter", nargs="+", help="Command after --; reads JSON stdin and writes JSON stdout")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
    except LoopError as exc:
        print(json.dumps(exc.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": "input_error", "message": str(exc), "details": {}}, indent=2), file=sys.stderr)
        raise SystemExit(2)
    exit_code = result.pop("exit_code", 0) if isinstance(result, dict) else 0
    print(json.dumps(result, indent=2, sort_keys=True))
    if exit_code:
        raise SystemExit(exit_code)


def dispatch(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "init":
        budget = {
            key: value
            for key, value in {
                "max_attempts": args.max_attempts,
                "max_failures": args.max_failures,
                "deadline": args.deadline,
            }.items()
            if value is not None
        }
        criteria = args.criteria
        if args.criteria_file:
            criteria = json.loads(args.criteria_file.read_text(encoding="utf-8"))
            if not isinstance(criteria, list):
                raise json.JSONDecodeError(
                    "criteria file must contain a JSON array",
                    args.criteria_file.read_text(encoding="utf-8"), 0,
                )
        charter_hash = None
        charter_text = None
        if args.charter:
            charter_text = args.charter.read_text(encoding="utf-8")
            charter_hash = hashlib.sha256(charter_text.encode("utf-8")).hexdigest()
        engine = LoopEngine.create(
            args.root, args.task, criteria, budget, args.task_id,
            controls=args.controls, policy_version=args.policy, charter_hash=charter_hash,
        )
        if charter_text is not None:
            payload_dir = engine.workspace / "payload"
            payload_dir.mkdir(exist_ok=True)
            (payload_dir / "charter.md").write_text(charter_text, encoding="utf-8")
        return {"workspace": str(engine.workspace), "state": engine.load(), "next": engine.next_instruction()}
    if args.command == "migrate":
        engine = LoopEngine.migrate_legacy(args.source, args.root, args.task_id)
        return {"workspace": str(engine.workspace), "state": engine.load(), "next": engine.next_instruction()}
    if args.command == "guard":
        return guard_report(args.workspaces)
    if args.command == "fleet":
        fleet_kwargs = {
            "max_parallel": args.max_parallel,
            "max_cycles_per": args.max_cycles_per,
            "adapter_retries": args.adapter_retries,
            "timeout": args.timeout,
            "dry_run": args.dry_run,
            "adapter": args.adapter or None,
            "spend_ceiling_usd": args.spend_ceiling_usd,
        }
        if args.watch or args.inbox:
            return fleet_watch(
                args.root,
                interval=args.interval if args.watch else 0.0,
                max_passes=args.max_passes if args.watch else 1,
                inbox=args.inbox,
                **fleet_kwargs,
            )
        return fleet_report(args.root, **fleet_kwargs)
    if args.command == "commons":
        return commons_report(args.root, args.out)
    if args.command == "frontier":
        return frontier_report(
            args.root,
            SubprocessAdapter(args.adapter, args.timeout),
            out=args.out,
            max_proposals=args.max_proposals,
            apply=args.apply,
        )
    if args.command == "sandbox":
        return sandbox_report(args.workspace, init=args.init, timeout=args.timeout)
    if args.command == "keygen":
        import os
        import secrets
        from .sandbox import require_workspace
        workspace = require_workspace(args.workspace)
        key_path = workspace / CHECKER_KEY_FILE
        if key_path.exists():
            raise LoopError(f"An attestation key already exists: {key_path}")
        key_path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        os.chmod(key_path, 0o600)
        return {"workspace": str(workspace), "key_file": str(key_path), "created": True}
    if args.command == "mirror":
        return mirror_report(args.workspace, args.destination)
    if args.command == "validate":
        input_value = _read_json_object(args.input) if args.input else None
        return validate_report(args.workspace, args.hook, input_value=input_value, timeout=args.timeout)
    if args.command == "intake":
        adapter = SubprocessAdapter(args.adapter, args.timeout) if args.adapter else None
        return intake_report(
            args.source,
            root=args.root,
            apply=args.apply,
            link=args.link,
            max_files=args.max_files,
            max_bytes=args.max_bytes,
            adapter=adapter,
        )

    engine = LoopEngine(args.workspace)
    if args.command == "status":
        state = engine.load()
        return {**state, "supervision": engine.supervision(state)}
    if args.command == "next":
        return engine.next_instruction(explore=args.explore)
    if args.command == "scaffold":
        return submission_scaffold(engine.next_instruction(explore=args.explore), engine.load())
    if args.command == "record":
        return engine.record_attempt(_read_json_object(args.result))
    if args.command == "audit":
        return engine.audit(repair=args.repair)
    if args.command == "finalize":
        return engine.finalize(args.request_id)
    if args.command == "block":
        return engine.mark_blocked(_read_json_object(args.decision))
    if args.command == "unsafe":
        return engine.mark_unsafe(_read_json_object(args.decision))
    if args.command == "retest":
        if args.record:
            return engine.record_retest(_read_json_object(args.record))
        supervision = engine.supervision()
        return {
            "status": supervision["status"],
            "retest": supervision.get("retest"),
            "wishes_due": supervision.get("wishes_due", []),
        }
    if args.command == "unblock":
        return engine.unblock(_read_json_object(args.decision))
    if args.command == "ask-human":
        return engine.request_human_input(_read_json_object(args.decision))
    if args.command == "answer-human":
        return engine.provide_human_input(_read_json_object(args.decision))
    if args.command == "add-criterion":
        return engine.add_criterion(_read_json_object(args.decision))
    if args.command == "fulfill-wish":
        return engine.fulfill_wish(_read_json_object(args.decision))
    if args.command == "record-fault":
        return engine.record_process_fault(_read_json_object(args.decision))
    if args.command == "drive":
        adapter = SubprocessAdapter(args.adapter, args.timeout)
        return drive(engine, adapter, args.max_cycles, args.adapter_retries)
    raise AssertionError(f"Unhandled command: {args.command}")


def guard_report(workspaces: Sequence[Path]) -> Dict[str, Any]:
    """Session-end guard: nonzero exit while any workspace holds an obligation.

    Exit codes: 0 all clear; 1 at least one unmet obligation; 2 at least one
    workspace failed to load. Wire this into a session Stop hook so an agent
    cannot idle while its loop still owes work.
    """

    rows = []
    worst = 0
    for path in workspaces:
        engine = LoopEngine(path)
        try:
            state = engine.load()
            supervision = engine.supervision(state)
        except LoopError as exc:
            rows.append({"workspace": str(path), "ok": False, "error": exc.as_dict()})
            worst = max(worst, 2)
            continue
        obligations = workspace_obligations(state, supervision)
        ok = not obligations
        if not ok:
            worst = max(worst, 1)
        rows.append({
            "workspace": str(path),
            "status": state["status"],
            "ok": ok,
            "obligations": obligations,
        })
    return {"ok": worst == 0, "workspaces": rows, "exit_code": worst}


def submission_scaffold(directive: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if directive["action"] != "attempt":
        return directive
    if directive["role"] == "surgeon":
        demand = directive.get("demand", {})
        diagnosis: Dict[str, Any] = {
            "raw_detail": "replace with the raw repeating condition, verbatim",
            "classification": "harness_gap | research_failure | human_dependency",
            "kill_test": "replace with what outcome would show this amendment is wrong",
            "next_experiment": "replace with the next experiment that uses the new rung",
            "proposed_delta": {
                "schema_version": directive.get("overlay_schema_version", 1),
                "ops": [{
                    "op": " | ".join(directive.get("overlay_ops", [])),
                }],
            },
            "delta_fingerprint": "replace-with-sha256-of-the-delta-document",
        }
        if demand.get("kind") == "fault":
            diagnosis["fault_signature"] = demand.get("signature")
        return {
            "request_id": "replace-with-unique-request-id",
            "directive_id": directive["directive_id"],
            "role": "surgeon",
            "mode": "overlay_diagnosis",
            "actor": {"agent_id": "agent-name", "model": "model-name", "context_id": "fresh-context-id"},
            "diagnosis": diagnosis,
            "decisions": [],
        }
    if directive.get("mode") == "overlay_review":
        return {
            "request_id": "replace-with-unique-request-id",
            "directive_id": directive["directive_id"],
            "role": "critic",
            "mode": "overlay_review",
            "actor": {"agent_id": "agent-name", "model": "model-name", "context_id": "fresh-review-context-id"},
            "overlay_review": {
                "target_attempt_id": directive["target_attempt_id"],
                "verdict": "adopt | reject | narrow",
                "reasons": ["replace with the judgement on the proposed amendment"],
                "uncertainty": "replace with what this review could have missed",
            },
            "decisions": [],
        }
    if directive["role"] == "explorer":
        seed_count = directive["seed_count_range"][0]
        seeds = []
        for index in range(seed_count):
            seed = {
                "claim": f"replace with falsifiable claim {index + 1} (distinct from every prior seed)",
                "basin": f"replace with the conceptual family of approach {index + 1}",
                "first_unjustified_step": "replace with the first step this claim cannot yet justify",
                "kill_test": "replace with the fastest concrete test that would kill this claim",
            }
            if directive["ideation_kind"] == "evolve":
                seed["parents"] = [item["id"] for item in directive.get("top_candidates", [])[:1]]
            seeds.append(seed)
        return {
            "request_id": "replace-with-unique-request-id",
            "directive_id": directive["directive_id"],
            "role": "explorer",
            "mode": "ideation",
            "actor": {"agent_id": "agent-name", "model": "model-name", "context_id": "fresh-minimal-context-id"},
            "context_scope": "minimal",
            "ideation_kind": directive["ideation_kind"],
            "seeds": seeds,
            "decisions": [],
        }
    if directive["mode"] == "triage":
        verdicts = [
            {
                "seed_index": seed["seed_index"],
                "decision": "killed",
                "reason": f"replace with the judgement on seed {seed['seed_index']}",
            }
            for seed in directive["seeds"]
        ]
        payload_triage: Dict[str, Any] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdicts": verdicts,
        }
        if directive.get("open_candidates"):
            payload_triage["comparisons"] = [{
                "seed_index": 0,
                "candidate_id": directive["open_candidates"][0]["id"],
                "winner": "candidate",
                "rationale": "replace: which is likelier to survive its kill test, and why",
            }]
        return {
            "request_id": "replace-with-unique-request-id",
            "directive_id": directive["directive_id"],
            "role": "critic",
            "mode": "triage",
            "actor": {"agent_id": "agent-name", "model": "model-name", "context_id": "fresh-context-id"},
            "triage": payload_triage,
            "decisions": [],
        }
    payload: Dict[str, Any] = {
        "request_id": "replace-with-unique-request-id",
        "directive_id": directive["directive_id"],
        "role": directive["role"],
        "mode": directive["mode"],
        "actor": {"agent_id": "agent-name", "model": "model-name", "context_id": "fresh-context-id"},
        "strategy": {
            "decomposition": "what subproblem is isolated",
            "source_class": "what class of source or input is used",
            "retrieval_method": "how information is acquired",
            "reasoning_method": "how the hypothesis is tested",
            "tool": "what tool executes the experiment",
            "verification_method": "how the observation can be checked",
        },
        "hypothesis": "falsifiable prediction",
        "action": "smallest useful experiment actually executed",
        "observation": "raw result, including failures",
        "interpretation": "what the observation does and does not establish",
        "uncertainties": ["remaining uncertainty"],
        "next_step": "distinct follow-up if this is insufficient",
        "outcome": "progress",
        "evidence": [],
        "criterion_updates": [],
        "contradictions": [],
        "contradiction_resolutions": [],
        "decisions": [],
    }
    mode = directive["mode"]
    if directive["role"] == "planner":
        subproblems = ["independently testable subproblem"]
        if mode == "decompose":
            subproblems.append("second independently testable subproblem")
        payload["plan"] = {
            "assumptions": ["assumption to test"],
            "subproblems": subproblems,
            "candidate_experiments": ["candidate experiment"],
            "falsification_tests": ["condition that would disprove the working approach"],
            "rejected_assumptions": ["stale assumption rejected during replanning"] if mode == "fresh_replan" else [],
        }
    elif directive["role"] == "researcher":
        payload["plan_id"] = directive["active_plan_id"]
        targets = [item["id"] for item in directive["open_criteria"][:1]]
        if not targets and state:
            targets = [state["criteria"][0]["id"]]
        payload["criterion_targets"] = targets
        if state is None or state.get("policy_version") != "2.0":
            payload["basin"] = "replace with the conceptual family this approach belongs to"
            move = directive.get("required_move", "test")
            payload["move"] = move
            if move == "survey":
                payload["assets_registered"] = [{
                    "name": "replace-with-unique-asset-name",
                    "kind": "technique | dataset | tool | source | result",
                    "locator": "replace with a reproducible locator",
                }]
            elif move == "combine":
                payload["combination"] = {"asset_ids": ["replace-asset-id-1", "replace-asset-id-2"]}
            elif move == "barrier_probe":
                payload["barrier_probe"] = {
                    "new_barrier": {
                        "name": "replace-with-barrier-name",
                        "statement": "replace with the exact statement of the obstruction",
                    },
                    "pattern": "bound | dual | shift_representation",
                    "approach": "replace with how this probe attacks the wall",
                }
    elif mode == "attempt_review":
        payload["assessment"] = {
            "target_attempt_id": directive["target_attempt_id"],
            "verdict": "validated_progress",
            "reasons": ["directly observed reason"],
            "uncertainty": "what the critique could have missed",
        }
        if "lens" in directive:
            payload["lens"] = directive["lens"]
            if directive["lens"] == "proves_too_much" and directive.get("controls"):
                payload["assessment"]["control_check"] = {
                    "control_id": directive["controls"][0]["id"],
                    "outcome": "rejects_control | endorses_control | not_applicable",
                    "note": "replace with what happened when the claim met the control",
                }
        if directive.get("multi_cause_required"):
            payload["assessment"]["candidate_causes"] = [
                {"cause": "replace candidate cause 1", "discriminating_test": "replace test that separates it"},
                {"cause": "replace candidate cause 2", "discriminating_test": "replace test that separates it"},
            ]
    elif mode == "contradiction_search":
        payload["contradiction_search"] = {
            "assumptions_checked": ["assumption checked"],
            "disconfirming_queries": ["disconfirming test run"],
            "conclusion": "result of the contradiction search",
        }
    elif mode == "blocker_audit":
        payload["blocker_audit"] = {
            "dependency": "specific external dependency",
            "dependency_evidence_ids": ["replace-with-direct-evidence-id"],
            "safe_alternative_attempt_ids": ["A000002", "A000004", "A000006"],
            "unsafe_alternatives_rejected": [],
            "exhaustion_reason": "why no remaining safe method can proceed",
            "human_action": "precise action that unblocks the task",
            "safe_alternatives_exhausted": True,
        }
    elif directive["role"] == "verifier":
        for criterion in directive["criteria_to_verify"]:
            criterion_id = criterion["id"]
            ref = f"verify-{criterion_id.lower()}"
            payload["evidence"].append({
                "ref": ref,
                "kind": "independent-check",
                "quality": "direct",
                "claim": f"replace with the independent observation for {criterion_id}",
                "locator": "replace with a reproducible locator",
                "method": "replace with the independent method actually used",
                "fingerprint": hashlib.sha256(f"replace-{criterion_id}".encode("utf-8")).hexdigest(),
                "independence_key": f"replace-with-independent-key-{criterion_id}",
                "supports": [criterion_id],
            })
        payload["verification_results"] = [
            {
                "criterion_id": criterion_id,
                "verdict": "pass",
                "evidence_refs": [f"verify-{criterion_id.lower()}"],
                "method": "independent check",
                "observation": "direct verification observation",
            }
            for criterion in directive["criteria_to_verify"]
            for criterion_id in [criterion["id"]]
        ]
    elif directive["role"] == "synthesizer":
        verified = directive["verified_criteria"]
        payload["report"] = {
            "summary": "evidence-backed result",
            "criterion_results": [
                {
                    "criterion_id": criterion["id"],
                    "conclusion": f"replace with the conclusion for {criterion['id']}",
                    "primary_evidence_ids": criterion["primary_evidence_ids"],
                    "verification_evidence_ids": criterion["verification_evidence_ids"],
                }
                for criterion in verified
            ],
            "facts": [{
                "claim": "replace with a directly evidenced fact",
                "evidence_ids": verified[0]["primary_evidence_ids"],
            }],
            "inferences": [],
            "uncertainties": [],
            "limitations": ["The harness validates provenance and process integrity, not truth by itself."],
            "unresolved_noncritical_contradiction_ids": [
                item["id"] for item in directive["unresolved_contradictions"] if item["severity"] == "noncritical"
            ],
            "next_actions": [],
        }
    return payload


def _read_json_object(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise json.JSONDecodeError("top-level JSON value must be an object", path.read_text(encoding="utf-8"), 0)
    return value


if __name__ == "__main__":
    main()
