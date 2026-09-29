# The two $2,000 budgets

Neither budget can borrow from the other. Each has a soft line the harness reacts to and a hard stop outside the harness.

## Azure API spend ($2,000 across the whole fleet)

| Layer | Where | What happens |
|---|---|---|
| 1 | `ApiSpendGuard` at `/srv/adv-loop/state/api-spend.json`, refreshed every pass from every `.spend.jsonl` plus the loops' own ledger | At $1,500 cumulative: `max_parallel` drops to 1 and the global refine loop is disabled. At $1,900: no API-billed session starts; the workspace is paused with reason `api_budget_stop`. |
| 2 | Kernel fleet ceiling `--spend-ceiling-usd 1900` (the `fleet_spend_ceiling_usd` runtime field) | The kernel's own `spend_stopped` marks every row undrivable. |
| 3 | Azure Cost Management budget on the Foundry resource: $2,000 with alerts at 50, 80, 95 percent to the owner's email | The owner's stop. Create in the portal: Cost Management, Budgets, scope = the Foundry resource group, monthly reset off, amount 2000. |

Subscription sessions record `estimated_usd: 0.0` and never count here.

## AWS ($2,000 for harness-box, storage, and adv-gpu-box hours)

| Layer | Where | What happens |
|---|---|---|
| 1 | `harness/aws/aws_cost.py` run by `adv-aws-cost.timer` every 6 h (and inline by `GpuBox.ensure_running` when the state is older than 12 h), writes `/srv/adv-loop/state/aws-spend.json` with `by_tag {harness, gpu, untagged}` | At $1,600 (counting the box hours Cost Explorer has not shown yet plus the job about to start): GPU box starts refused with `gpu_budget_stop`, no pause. At $1,850: no sessions start (`aws_budget_stop`). A stale state that cannot be refreshed refuses with `gpu_budget_unknown`. |
| 2 | Per-job ledger `<ws>/.gpu-spend.jsonl` (kind `job`: job_id, session, seconds, usd, state) against the charter's `harness.gpu.max_hours` | A completed job that crosses the cap pauses the workspace with reason `gpu_budget_stop` (indefinite); `adv-harness resume` after the operator raises `max_hours`. |
| 3 | Fleet ledger `/srv/adv-loop/state/gpu-spend.jsonl` (kind `box_run`: one row per start/stop cycle, harness clock cross-checked against `LaunchTime`) | What `adv-harness gpu spend` reports and what `unreported_usd` adds to the guard until Cost Explorer catches up (24 h lag). |
| 4 | AWS Budgets `adv-aws-monthly` ($150, alerts at 50/80/100 percent, no action) and `adv-aws-lifetime` ($1,800 annual from 2026-09-01, alerts at 50/80/95 percent, at 100 percent a `RUN_SSM_DOCUMENTS` action that stops both instances through the `adv-budgets-stop-ec2` role) | AWS stops the instances itself. Only the lifetime budget carries the stop action: a monthly stop would kill a job mid-run. `harness/aws/setup-budgets.sh --apply` creates all of it. |
| 5 | Idle stops | harness-box: after 20 idle minutes the supervisor writes `wake_at.json` and runs `sudo /sbin/shutdown -h +2`. adv-gpu-box: the supervisor's reaper stops it after 10 idle minutes; the box-side watchdog powers it off after 15 minutes without controller contact or 12 h uptime; the CloudWatch alarm `adv-gpu-box-idle-stop` (CPU under 3 percent for 45 minutes) is the outside fallback. EBS-backed stops bill storage only. |

The timer and the GPU key are installed by `box-bootstrap.sh`; check with `systemctl list-timers adv-aws-cost.timer`. Set `ADV_LOOP_AWS_SINCE` in `/etc/adv-loop/env.d/harness` to the first day of the pot. One-time, from the Mac:

```
aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=adv-loop,Status=Active   # 24 h to take effect
harness/aws/setup-iam.sh --apply        # ADV_LOOP_HARNESS_BOX set; harness-box may start/stop only adv-loop=gpu instances and read Cost Explorer
harness/aws/setup-budgets.sh --apply    # ADV_LOOP_OWNER_EMAIL, ADV_LOOP_HARNESS_BOX and ADV_LOOP_GPU_BOX set; confirm the subscription email
```

## Wake

- `adv-ctl drop <dir>` starts harness-box, rsyncs the drop into `/srv/adv-loop/inbox/.incoming/`, and moves it into the inbox, so a new goal wakes the box.
- Rate-limit resets: `wake_at.json` carries the earliest pause expiry. `adv-ctl status` and `adv-ctl stop` cache it on the Mac; `adv-ctl wake --if-due` starts the instance once that time has passed, and `harness/aws/com.adv-loop.wake.plist` runs it from `launchd` every 15 minutes. An EventBridge Scheduler one-shot with the `ec2:startInstances` universal target does the same without the Mac; verify the target shape before relying on it.
