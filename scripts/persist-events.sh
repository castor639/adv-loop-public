#!/usr/bin/env bash
# ADV Loop on_record durability hook: commit and push the workspace event log
# after every append. Invoked by the engine with cwd = the workspace directory.
# workspaces/*/ is gitignored, so the audited file set is force-added by name;
# transient sidecars and sandbox build artifacts stay out.
set -euo pipefail
ws="$(pwd)"
repo="$(git -C "$ws" rev-parse --show-toplevel)"
name="$(basename "$ws")"
cd "$repo"

files=(events.jsonl state.json attempts.jsonl evidence.jsonl task.md
       decision-log.md report.md loop-config.json
       sandbox/pyproject.toml sandbox/uv.lock sandbox/lean-toolchain sandbox/setup.sh)
for f in "${files[@]}"; do
  [ -e "$ws/$f" ] && git add -f -- "$ws/$f"
done
if [ -d "$ws/payload" ]; then
  git add -f -- "$ws/payload"
fi
# harness sidecars travel with the workspace: transcripts and the local improvement state
for d in transcripts harness-state; do
  [ -d "$ws/$d" ] && git add -f -- "$ws/$d"
done

if git diff --cached --quiet; then
  exit 0
fi
git commit -q -m "$name: persist events (head append)"
for delay in 0 2 4 8 16; do
  sleep "$delay"
  if git push -q -u origin HEAD; then
    exit 0
  fi
done
echo "persist-events: push failed after retries" >&2
exit 1
