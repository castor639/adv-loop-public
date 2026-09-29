# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ADV Loop is an event-sourced, proof-gated control plane for persistent agent work. Models propose attempts; a deterministic kernel decides whether each transition is legal and whether its proof suffices. Pure-stdlib Python (>=3.9), no runtime dependencies.

## Commands

```bash
python3 -m pip install -e .                     # install (provides the adv-loop CLI)
python3 -m unittest discover -s tests -v        # run all tests
python3 -m unittest tests.test_engine.ClassName.test_name -v   # run a single test
```

CLI workflow for a task workspace: `adv-loop init | next | scaffold | record | audit [--repair] | finalize | drive | migrate` (see README.md for usage). The 4.0 supervisor plane adds `adv-loop intake | fleet | sandbox | validate | record-fault`; the 5.0 autonomy plane adds `fleet --watch/--inbox`, `commons`, `frontier`, `init --charter`, plus `adapters/` (reference Claude adapter + frontier generator — the only code allowed to depend on the Anthropic SDK — and a stdlib Fireworks adapter speaking the OpenAI-compatible wire format) and `checkers/` (reference validate hooks). Both directories live outside `src/` on purpose.

`harness/` is the runtime around the kernel (containers, model backends, budgets, the two improvement loops). `adv-harness run|pass|drill|provision|gpu|status` drives it; `harness/gpu/` runs GPU jobs on a self-started AWS box (`gpu_run`, `gpu_collect`, `gpu_status` tools, `checkers/gpu_replay.py` and `gpu_replicate.py`), `harness/aws/adv-ctl` is the Mac control script, and `harness/prompts/ARCHITECTURE.md` is the hash-pinned system reference injected per role (regenerate its tables with `python3 -m harness.architecture --write`; `--check` runs in the tests).

## Architecture

Source lives in `src/adv_loop/`. Data flows in a loop: policy issues a directive → adapter submits an attempt → engine validates → storage appends an event → replay rebuilds state → policy again.

- **`storage.py`** — durable authority. `events.jsonl` (hash-chained, sequence-numbered) is the *only* source of truth. Appends run under an OS file lock (`flock`) with a write-ahead journal (`.pending-event.json`) and fsync; recovery finishes or verifies a pending write but never guesses on corruption.
- **`policy.py`** — deterministic scheduler. Next role/mode is a pure function of replayed state. Directive IDs hash the policy version + event head, so a directive goes stale the moment another writer lands an event. Contains the escalation ladder (failure streak 3 → contradiction_search, 4 → decompose, 5 → fresh_replan, 7 → blocker_audit) and the six strategy dimensions used for novelty checks.
- **`engine.py`** — transition and proof gates (the bulk of the code). Validates submissions against the directive, enforces strategy novelty (fingerprints can never be reused), assigns evidence IDs, and gates terminal states: completion requires direct per-criterion evidence, an independent verifier pass (different context ID *and* provenance), no open critical contradictions, and a structured report hashed into the completion event. `blocked` / `unsafe` / `budget_exhausted` each have distinct gates and never masquerade as `completed`.
- **`driver.py`** — provider-neutral compute loop. Spawns any adapter executable (no shell); JSON envelope in via stdin, one attempt-submission JSON out via stdout, with bounded correction retries. Adapter failure leaves the task `active`, never terminal; on 4.0 it also records a `process_fault_recorded` observation whose repeats schedule the surgeon.
- **`intake.py` / `runner.py` / `sandbox.py` / `validators.py` / `pathsafe.py`** — the 4.0 supervisor plane: drop sorting into isolated workspaces, the fleet scheduler above the stale-directive protocol, declared-argv sandboxes, checker hooks with attested verdicts, and the single write-containment choke point. Field-neutral like everything else in `src/` — the neutrality lint scans these files too (nb: "portfolio" is denylisted; the scheduler is called `fleet`).
- **`cli.py`** — thin argparse front end over the above.
- **`schemas/`** — JSON Schemas for events, attempts, evidence, state, and reports; `protocols/loop.md` and `docs/architecture.md` document the full contract; `docs/policy-4.0.md` is the 4.0 design note.

## Key invariants

- Never hand-edit projections (`state.json`, `attempts.jsonl`, `evidence.jsonl`, `task.md`, `decision-log.md`, `report.md`) to change task state — they are deterministic, byte-audited projections of `events.jsonl`. Rebuild them with `adv-loop audit --repair`.
- Replay deep-copies attempts/evidence from events before attaching derived fields, so replay can never mutate the stored event envelope.
- A researcher's self-reported outcome does not change the failure streak; only a fresh-context critic review does.
- Any research after verification invalidates prior verification and the report.
- Never delete `.pending-event.json` manually; recovery handles it.
- Policy versions are pinned in `task_created` and inherit forward: 2.0 is frozen forever, 3.0 adds governed stopping and scheduled research thinking, 4.0 (the default for new inits; `--policy 3.0` opts down) adds formalization ranks, per-workspace overlays, and harness diagnosis. Every 4.0 state key, event, and gate initializes only under `is_v4`; the freeze rails in `tests/test_persistence.py` pin 2.0/3.0 byte-identity.
- Overlays only ever add or tighten (nine-op allowlist, validated at proposal/review/adoption/replay); `payload/` and the sandbox root are evidence sources, never projections.

AGENTS.md defines the contract for agents *operating* ADV Loop task workspaces (as opposed to developing the codebase) — follow it whenever working inside `workspaces/<task-id>/`.
