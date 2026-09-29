"""adv-harness command line. Subcommands land phase by phase."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import HARNESS_ID, __version__
from . import assembler, schema

GPU_HELP = {
    "status": "box state, locks, running jobs, and spend; never touches AWS",
    "jobs": "job records from the fleet index or one workspace",
    "collect": "pull and hash the declared outputs of a job whose session ended",
    "stop": "cancel one job (WORKSPACE JOB_ID), or stop the box (--box or no job)",
    "start": "start the box and wait for its agent",
    "reap": "settle orphaned jobs and stop the box when idle",
    "exec": "run an argv on the box: adv-harness gpu exec --state S -- nvidia-smi -L",
    "bootstrap": "push and run the box bootstrap script",
    "keygen": "generate the controller's ssh key for the box",
    "trust": "pin the box host key (--reset after checking the console output)",
    "spend": "GPU dollars: box runs and job rows",
    "provision-box": "start the box, optionally build the image, observe and write the fleet lock",
}


def _gpu_parser(sub: Any) -> None:
    gpu = sub.add_parser("gpu", help="the GPU box: status, jobs, collection, stops, bootstrap, spend")
    commands = gpu.add_subparsers(dest="gpu_command")
    with_root = {"status", "reap", "spend"}
    with_repo = {"collect", "stop", "start", "reap", "exec", "bootstrap", "keygen", "trust", "provision-box"}
    for name, help_text in GPU_HELP.items():
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("--state", required=True, help="runtime state directory")
        if name in with_root:
            cmd.add_argument("--root", default=None, help="workspace root, for per-workspace rows")
        if name in with_repo:
            cmd.add_argument("--repo", default=None, help="checkout holding harness/gpu/agent and harness/aws")
        if name == "jobs":
            cmd.add_argument("--workspace", default=None)
            cmd.add_argument("--status", default=None, help="only jobs in this state")
        elif name == "collect":
            cmd.add_argument("workspace")
            cmd.add_argument("job_id")
        elif name == "stop":
            cmd.add_argument("workspace", nargs="?")
            cmd.add_argument("job_id", nargs="?")
            cmd.add_argument("--box", action="store_true", help="stop the instance, killing any job")
            cmd.add_argument("--reason", default="operator")
        elif name == "exec":
            cmd.add_argument("argv", nargs=argparse.REMAINDER)
        elif name == "bootstrap":
            cmd.add_argument("--force", action="store_true")
        elif name == "trust":
            cmd.add_argument("--reset", action="store_true")
        elif name == "provision-box":
            cmd.add_argument("--image", default=None)
            cmd.add_argument("--build", action="store_true", help="run the bootstrap (image build) before observing")


def _emit(value: Any, code: int = 0) -> int:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))
    return code


def _latest_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if isinstance(row.get("job_id"), str):
            latest[row["job_id"]] = row
    return list(latest.values())


def _gpu_workspaces(root: Path) -> Dict[str, Dict[str, Any]]:
    from . import provision
    from . import gpu as gpu_module
    from .gpu import jobs, spend
    report: Dict[str, Dict[str, Any]] = {}
    if not root.is_dir():
        return report
    for ws in sorted(p for p in root.iterdir() if p.is_dir() and (p / "loop-config.json").is_file()):
        config = provision.read_config(ws)
        if not gpu_module.enabled(config):
            continue
        merged = gpu_module.config_for(config)
        report[ws.name] = {"hours_used": spend.hours_used(ws), "max_hours": merged["max_hours"],
                           "hours_remaining": spend.hours_remaining(ws, merged),
                           "awaiting_collection": [j.job_id for j in jobs.pending_collection(ws)]}
    return report


def _fake_gpu_box(state_dir: Path) -> Any:
    """The test double for `drill --gpu`; it ships with the test suite, not the package."""

    try:
        from tests.fake_gpu_box import FakeBox
    except ImportError:
        tests_dir = Path(__file__).resolve().parents[1] / "tests"
        if not (tests_dir / "fake_gpu_box.py").is_file():
            raise SystemExit("drill --gpu needs tests/fake_gpu_box.py from a checkout; use --gpu-live for the real box")
        sys.path.insert(0, str(tests_dir))
        from fake_gpu_box import FakeBox
    return FakeBox(state_dir / "fake-gpu-remote")


def _gpu_main(args: argparse.Namespace) -> int:
    from . import budget, provision
    from . import gpu as gpu_module
    from .gpu import box_api, index, jobs, lock, spend
    from .gpu.box_api import GpuBoxError

    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    guard = budget.AwsSpendGuard(state / "aws-spend.json")
    repo = Path(args.repo).resolve() if getattr(args, "repo", None) else None

    def box() -> Any:
        return box_api.load_box(state, guard=guard, repo=repo)

    name = args.gpu_command
    try:
        if name == "status":
            fleet = lock.read_fleet_lock(state)
            try:
                box_state = json.loads((state / gpu_module.STATE_BOX_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                box_state = None
            try:
                active = box().active_jobs()
            except Exception as exc:
                active = {"error": f"{type(exc).__name__}: {exc}"}
            report: Dict[str, Any] = {
                "box": box_state, "active_jobs": active, "fleet_lock": fleet.as_dict() if fleet else None,
                "toolchain_hash": fleet.toolchain_hash if fleet else None, "running": index.running_jobs(state),
                "spend_usd": budget.gpu_spend_total(state), "aws": guard.state(),
            }
            if args.root:
                report["workspaces"] = _gpu_workspaces(Path(args.root))
            return _emit(report)
        if name == "jobs":
            if args.workspace:
                rows = [jobs.public_view(job.as_dict()) for job in jobs.JobStore(Path(args.workspace)).list()]
            else:
                rows = _latest_rows(index.rows(state))
            if args.status:
                rows = [row for row in rows if row.get("status") == args.status]
            return _emit(rows)
        if name == "collect":
            ws = Path(args.workspace).resolve()
            config = gpu_module.config_for(provision.read_config(ws))
            result, _, _, _ = jobs.collect(ws, state, args.job_id, session_id="cli", box=box(), config=config)
            return _emit(result, 0 if result.get("status") != "refused" else 1)
        if name == "stop":
            if args.box or not args.workspace:
                return _emit(box().stop_now(reason=args.reason))
            if not args.job_id:
                raise SystemExit("gpu stop needs WORKSPACE JOB_ID, or --box to stop the instance")
            return _emit(jobs.cancel(box(), Path(args.workspace).resolve(), state, args.job_id))
        if name == "start":
            return _emit(box().ensure_running().as_dict())
        if name == "reap":
            controller = box()
            report = {}
            if args.root:
                report["sweep"] = jobs.sweep(controller, Path(args.root), state)
            report["reap"] = controller.reap()
            return _emit(report)
        if name == "exec":
            argv = list(args.argv)
            if argv and argv[0] == "--":
                argv = argv[1:]
            if not argv:
                raise SystemExit("gpu exec needs an argv after --")
            done = box().exec_remote(argv)
            sys.stdout.write(done.stdout)
            sys.stderr.write(done.stderr)
            return done.returncode
        if name == "bootstrap":
            # ensure_running starts the box, waits for ssh, and installs the agent when it is missing.
            controller = box()
            info = controller.ensure_running()
            result = controller.bootstrap(force=True) if args.force else {"bootstrapped": info.bootstrapped}
            return _emit({**result, "box": info.as_dict()})
        if name == "keygen":
            return _emit({"key": str(box().keygen())})
        if name == "trust":
            return _emit(box().trust(reset=args.reset))
        if name == "spend":
            fleet_file = spend.state_spend_path(state)
            report = {"fleet_usd": budget.gpu_spend_total(state), "box_runs": budget.gpu_spend_rows(fleet_file, "box_run"),
                      "jobs": budget.gpu_spend_rows(fleet_file, "job")}
            if args.root:
                report["workspaces"] = _gpu_workspaces(Path(args.root))
            return _emit(report)
        if name == "provision-box":
            controller = box()
            info = controller.ensure_running()
            if args.build:
                controller.bootstrap(force=True)
            image = args.image or getattr(getattr(controller, "config", None), "image", None) or gpu_module.DEFAULTS["image"]
            observed = lock.observe(controller, image=image)
            path = lock.write_fleet_lock(state, observed)
            return _emit({"box": info.as_dict(), "lock": observed.as_dict(), "toolchain_hash": observed.toolchain_hash,
                          "path": str(path), "stop": controller.stop_if_idle(0)})
    except GpuBoxError as exc:
        print(json.dumps(exc.as_dict(), sort_keys=True), file=sys.stderr)
        return 2
    raise SystemExit("gpu needs a subcommand: " + ", ".join(GPU_HELP))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="adv-harness", description=f"{HARNESS_ID} {__version__}")
    sub = parser.add_subparsers(dest="command")
    ui = sub.add_parser("ui", help="open the interactive terminal workspace console")
    ui.add_argument("--root", type=Path, help="workspace root (default: discovered checkout/workspaces)")
    ui.add_argument("--state", type=Path, help="runtime state directory (default: checkout/harness/state)")
    ui.add_argument("--repo", type=Path, help="checkout (default: discover from current directory or installation)")
    ui.add_argument("--classic", action="store_true", help="the tabbed console instead of the chat console")
    ui.add_argument("--remote", metavar="HOST", help="mirror HOST's workspaces locally and send actions over ssh")
    ui.add_argument("--remote-root", default="/srv/adv-loop/workspaces")
    ui.add_argument("--remote-state", default="/srv/adv-loop/state")
    ui.add_argument("--remote-repo", default="/srv/adv-loop/repo")
    ui.add_argument("--remote-user", default="advloop", help="the supervisor's user on HOST")
    ui.add_argument("--cache", type=Path, help="local mirror directory (default ~/.cache/adv-loop/remote)")
    op = sub.add_parser("op", help="one operator action on a workspace; the JSON payload arrives on stdin")
    op.add_argument("name", choices=("new", "message", "pause", "resume", "step"))
    op.add_argument("--root", type=Path, required=True)
    op.add_argument("--state", type=Path, required=True)
    op.add_argument("--repo", type=Path)
    show = sub.add_parser("schema", help="print the model-facing schema for a role/mode")
    show.add_argument("role")
    show.add_argument("mode")
    show.add_argument("--policy", default="5.0")
    sub.add_parser("prompts", help="list shipped prompt files with their sha256")
    route = sub.add_parser("route", help="show which model answers each role for a workspace")
    route.add_argument("workspace", nargs="?")
    for name, help_text in (("run", "supervise a fleet root until stopped"), ("pass", "one supervisor pass"),
                            ("drill", "drive a throwaway workspace end to end and report named checks")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--root", required=True)
        cmd.add_argument("--state", required=True)
        cmd.add_argument("--inbox", default=None)
        cmd.add_argument("--repo", default=None)
        cmd.add_argument("--containers", action="store_true", help="use per-workspace docker containers")
        cmd.add_argument("--image", default=None)
        cmd.add_argument("--max-parallel", type=int, default=2)
        cmd.add_argument("--gpu", action="store_true",
                         help="attach the GPU box controller" + (" (the test fake; --gpu-live for the real box)" if name == "drill" else ""))
        if name == "run":
            cmd.add_argument("--idle-stop-minutes", type=int, default=20)
            cmd.add_argument("--shutdown", action="store_true", help="power the box off after the idle window")
            cmd.add_argument("--max-passes", type=int, default=None)
        if name == "drill":
            cmd.add_argument("--backend", default="minimal", choices=["minimal", "claude_code", "http_chat"])
            cmd.add_argument("--simulate-rate-limit", action="store_true")
            cmd.add_argument("--lie", action="store_true")
            cmd.add_argument("--alias", default=None, help="http_chat only: model alias from routing-defaults (astra, grok, deepseek, deepseek-flash)")
            cmd.add_argument("--gpu-live", action="store_true", help="run the GPU drill against the real box (spends AWS hours)")
    for name in ("pause", "resume"):
        cmd = sub.add_parser(name, help=f"{name} a workspace")
        cmd.add_argument("workspace")
        if name == "pause":
            cmd.add_argument("--note", default="owner hold")
    rf = sub.add_parser("refine", help="run the local self-improvement pass for one workspace")
    rf.add_argument("workspace")
    rf.add_argument("--state", required=True)
    rf.add_argument("--dry-run", action="store_true", help="propose and validate, apply nothing")
    rf.add_argument("--force", action="store_true", help="run even when no trigger fired")
    rf.add_argument("--signal", action="store_true", help="print the local signal and exit")
    rg = sub.add_parser("refine-global", help="run the global self-improvement pass over a fleet root")
    rg.add_argument("--root", required=True)
    rg.add_argument("--state", required=True)
    rg.add_argument("--fleet-state", default=None, help="global supplemental state root (default harness/state)")
    rg.add_argument("--dry-run", action="store_true")
    rg.add_argument("--force", action="store_true")
    rg.add_argument("--signal", action="store_true", help="print the cross-workspace signal and measures, then exit")
    st = sub.add_parser("status", help="fleet status: drivable, paused, spend, budgets")
    st.add_argument("--root", required=True)
    st.add_argument("--state", required=True)
    prov = sub.add_parser("provision", help="containerize a workspace: docker sandbox, checker hooks, lock, key")
    prov.add_argument("workspace")
    prov.add_argument("--image", default=None)
    prov.add_argument("--repo", default=None, help="repo checkout to mount read-only at /srv/adv-loop/repo")
    prov.add_argument("--hooks", default=None, help="comma-separated subset of lean-kernel,z3,cert-replay")
    prov.add_argument("--gpu", action="store_true", help="grant GPU compute; needs the fleet lock under --state")
    prov.add_argument("--state", default=None, help="runtime state directory holding the fleet GPU lock")
    _gpu_parser(sub)
    sub.add_parser("version", help="print the harness version")
    args = parser.parse_args(argv)
    if args.command == "ui":
        from . import tui
        remote = None
        if args.remote:
            from .remote import RemoteRoot
            kwargs = {"remote_root": args.remote_root, "remote_state": args.remote_state,
                      "remote_repo": args.remote_repo, "user": args.remote_user}
            if args.cache:
                kwargs["cache"] = args.cache
            remote = RemoteRoot(args.remote, **kwargs)
        if args.classic:
            return tui.launch(args.root, args.state, args.repo, remote=remote)
        from . import chat
        return chat.launch(args.root, args.state, args.repo, remote=remote)
    if args.command == "op":
        from . import ops
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            result = ops.run(args.name, args.root, args.state, args.repo, payload)
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
            return 1
        print(json.dumps(result, default=str))
        return 0
    if args.command == "schema":
        print(json.dumps(schema.load_cached(args.role, args.mode, args.policy), indent=2, sort_keys=True))
        return 0
    if args.command == "gpu":
        return _gpu_main(args)
    if args.command == "provision":
        from . import container, provision
        gpu_lock = None
        gpu_config = None
        if args.gpu:
            if not args.state:
                parser.error("--gpu needs --state to find the fleet GPU lock")
            from .gpu import lock as gpu_lock_module
            gpu_lock = gpu_lock_module.read_fleet_lock(Path(args.state))
            gpu_config = {"enabled": True}
        report = provision.provision(
            Path(args.workspace), container.Docker(), image=args.image or container.DEFAULT_IMAGE,
            repo=Path(args.repo).resolve() if args.repo else None,
            hooks=args.hooks.split(",") if args.hooks else None, gpu_lock=gpu_lock, gpu_config=gpu_config)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command in ("run", "pass", "drill"):
        from . import container, supervisor
        live_box = args.gpu and args.command != "drill" or getattr(args, "gpu_live", False)
        if live_box:
            os.environ["ADV_LOOP_GPU_ENABLED"] = "1"
        runtime = supervisor.Runtime(
            root=Path(args.root), state_dir=Path(args.state), inbox=Path(args.inbox) if args.inbox else None,
            repo=Path(args.repo).resolve() if args.repo else None,
            docker=container.Docker() if args.containers else None, image=args.image, max_parallel=args.max_parallel)
        if live_box and runtime.gpu is None:
            error_file = runtime.state_dir / supervisor.GPU_BOX_ERROR_FILE
            detail = error_file.read_text(encoding="utf-8").strip() if error_file.is_file() else "no controller loaded"
            print(f"--gpu: the GPU box controller did not load: {detail}", file=sys.stderr)
            return 2
        if args.command == "pass":
            print(json.dumps(supervisor.pass_once(runtime), indent=2, sort_keys=True, default=str))
            return 0
        if args.command == "drill":
            from . import drill
            if args.gpu and not args.gpu_live:
                runtime.gpu = _fake_gpu_box(runtime.state_dir)
            report = drill.run(runtime, backend=args.backend, simulate_rate_limit=args.simulate_rate_limit, lie=args.lie,
                               alias=args.alias, gpu=bool(args.gpu or args.gpu_live))
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
            return 0 if report["ok"] else 1
        runtime.idle_stop_minutes = args.idle_stop_minutes
        runtime.shutdown_command = ["sudo", "/sbin/shutdown", "-h", "+2"] if args.shutdown else None
        outcome = supervisor.serve(runtime, max_passes=args.max_passes)
        print(json.dumps(outcome, sort_keys=True))
        return 0
    if args.command in ("pause", "resume"):
        from . import pause
        ws = Path(args.workspace)
        if args.command == "pause":
            print(json.dumps(pause.human_hold(ws, args.note), sort_keys=True))
        else:
            pause.clear(ws / pause.PAUSE_FILE)
            print(f"resumed {ws}")
        return 0
    if args.command == "refine":
        from . import refine, supervisor
        ws = Path(args.workspace).resolve()
        if args.signal:
            print(json.dumps(refine.local_signal(ws), indent=2, sort_keys=True, default=str))
            return 0
        runtime = supervisor.Runtime(root=ws.parent, state_dir=Path(args.state))
        print(json.dumps(refine.refine_local(runtime, ws, dry_run=args.dry_run, force=args.force), indent=2, sort_keys=True, default=str))
        return 0
    if args.command == "refine-global":
        from . import refine_global, signal, supervisor
        runtime = supervisor.Runtime(root=Path(args.root), state_dir=Path(args.state),
                                     fleet_state=Path(args.fleet_state) if args.fleet_state else None)
        if args.signal:
            sig = signal.build(runtime.root, runtime.fleet_state)
            print(json.dumps({"fleet": sig["fleet"], "measures": signal.measures(sig, runtime.fleet_state),
                              "workspaces": [w["workspace"] for w in sig["workspaces"]], "errors": sig["errors"]},
                             indent=2, sort_keys=True, default=str))
            return 0
        report = refine_global.run(runtime, fleet_state=runtime.fleet_state, dry_run=args.dry_run, force=args.force)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 2 if report.get("aborted") else 0
    if args.command == "status":
        from adv_loop.runner import fleet_report
        from . import budget, pause, provision
        from . import gpu as gpu_module
        from .gpu import index as gpu_index
        from .gpu import lock as gpu_lock
        from .gpu import spend as gpu_spend
        root, state = Path(args.root), Path(args.state)
        report = fleet_report(root, dry_run=True, adapter=["/bin/true"])
        rows = {}
        for row in report["workspaces"]:
            ws = Path(row["workspace"])
            config = provision.read_config(ws)
            gpu_row: Optional[Dict[str, Any]] = None
            if gpu_module.enabled(config):
                merged = gpu_module.config_for(config)
                gpu_row = {"hours_used": gpu_spend.hours_used(ws), "max_hours": merged["max_hours"],
                           "hours_remaining": gpu_spend.hours_remaining(ws, merged)}
            rows[ws.name] = {"status": row.get("status"), "drivable": row.get("drivable"), "spend_usd": row.get("spend_usd"),
                             "paused": pause.workspace_paused(ws), "pause": pause.read(ws / pause.PAUSE_FILE),
                             "obligations": [o.get("kind", o) if isinstance(o, dict) else o for o in row.get("obligations", [])][:3],
                             "gpu": gpu_row}
        try:
            box_state = json.loads((state / gpu_module.STATE_BOX_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            box_state = None
        print(json.dumps({"workspaces": rows, "fleet_spend_usd": report["spend_usd"],
                          "api": budget.ApiSpendGuard(state / "api-spend.json").state(),
                          "aws": budget.AwsSpendGuard(state / "aws-spend.json").state(),
                          "subscription_paused": pause.subscription_paused(state),
                          "gpu": {"box": box_state, "fleet_lock": gpu_lock.read_fleet_lock(state) is not None,
                                  "running": gpu_index.running_jobs(state)},
                          "gpu_spend_usd": budget.gpu_spend_total(state)}, indent=2, sort_keys=True, default=str))
        return 0
    if args.command == "route":
        from . import provision, routing
        config = provision.read_config(Path(args.workspace)) if args.workspace else {}
        router = routing.Router(config)
        rows = {}
        for role in sorted(set(router.defaults.get("roles", {})) | set(router.config.get("roles", {}))):
            if role == "default" or "/" in role:
                continue
            try:
                rows[role] = {**router.resolve(role).summary(), "ok": True}
            except routing.RoutingError as exc:
                rows[role] = {"ok": False, "error": str(exc)}
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0 if all(r["ok"] for r in rows.values()) else 1
    if args.command == "prompts":
        for name, digest in assembler.prompt_files().items():
            print(f"{digest}  {name}")
        print(f"prompt_set_hash {assembler.prompt_set_hash()}")
        return 0
    if args.command == "version":
        print(f"{HARNESS_ID} {__version__}")
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
