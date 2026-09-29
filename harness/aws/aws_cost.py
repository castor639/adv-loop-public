#!/usr/bin/env python3
"""Poll Cost Explorer for cumulative unblended cost since a start date and feed the AWS guard.

Runs from the `adv-aws-cost.timer` every six hours, inline from `GpuBox.ensure_running`
when the state is older than twelve hours, or by hand:
    python3 aws_cost.py --since 2026-09-01 --state /srv/adv-loop/state/aws-spend.json --group-by-tag adv-loop
Uses the aws CLI with argv, no SDK. Credits are ignored on purpose: the
$2,000 budget is list-price burn, the same number the Budgets alarm sees.
With `--group-by-tag` the same query is split by the cost allocation tag so
`aws-spend.json` also carries `by_tag {harness, gpu, untagged}`; the total is
the sum of the groups, which equals the ungrouped total once the tag is active.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness.budget import AwsSpendGuard  # noqa: E402

TAG_BUCKETS = ("harness", "gpu", "untagged")


def query_argv(since: str, until: str, group_by_tag: Optional[str] = None) -> list:
    argv = ["aws", "ce", "get-cost-and-usage", "--time-period", f"Start={since},End={until}",
            "--granularity", "MONTHLY", "--metrics", "UnblendedCost",
            "--filter", json.dumps({"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}})]
    if group_by_tag:
        argv += ["--group-by", f"Type=TAG,Key={group_by_tag}"]
    return argv


def parse_costs(data: Dict[str, Any], group_by_tag: Optional[str] = None) -> Tuple[float, Optional[Dict[str, float]]]:
    """(total, by_tag) from a get-cost-and-usage response; `by_tag` is None for an ungrouped query."""

    if not group_by_tag:
        total = sum(float(r["Total"]["UnblendedCost"]["Amount"]) for r in data.get("ResultsByTime", []))
        return round(total, 2), None
    by_tag: Dict[str, float] = {name: 0.0 for name in TAG_BUCKETS}
    for result in data.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            key = (group.get("Keys") or [""])[0]
            # Cost Explorer spells a tag group as `<key>$<value>`; an empty value is the untagged bucket.
            value = key.split("$", 1)[1] if "$" in key else ""
            name = value or "untagged"
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            by_tag[name] = by_tag.get(name, 0.0) + amount
    by_tag = {name: round(amount, 2) for name, amount in by_tag.items()}
    return round(sum(by_tag.values()), 2), by_tag


def fetch(since: str, until: Optional[str] = None, *, group_by_tag: Optional[str] = None,
          runner=subprocess.run) -> Tuple[float, Optional[Dict[str, float]]]:
    until = until or (date.today() + timedelta(days=1)).isoformat()
    done = runner(query_argv(since, until, group_by_tag), capture_output=True, text=True, check=True)
    return parse_costs(json.loads(done.stdout), group_by_tag)


def cumulative_usd(since: str, until: Optional[str] = None, runner=subprocess.run) -> float:
    return fetch(since, until, runner=runner)[0]


def refresh(state_path: Path, since: str, group_by_tag: Optional[str] = None, runner=subprocess.run,
            until: Optional[str] = None) -> Dict[str, Any]:
    """Query Cost Explorer and rewrite the guard state; returns the state written."""

    total, by_tag = fetch(since, until, group_by_tag=group_by_tag, runner=runner)
    return AwsSpendGuard(Path(state_path)).update(total, by_tag=by_tag)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--until", default=None)
    parser.add_argument("--group-by-tag", default=None, help="cost allocation tag key, e.g. adv-loop")
    args = parser.parse_args()
    state = refresh(Path(args.state), args.since, group_by_tag=args.group_by_tag, until=args.until)
    print(json.dumps(state, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
