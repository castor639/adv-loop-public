# ADV Loop Agent Contract

This repository is a persistence harness for research agents. Every agent working here must follow this contract.

## Working on the harness itself

Two distinct roles exist here: *operating* task workspaces under `workspaces/<task-id>/` (this contract) and *developing* the engine (`src/adv_loop/`). When developing:

- The CLI is not preinstalled. Run `python3 -m pip install -e .` first or `adv-loop` will not be found.
- Tests: `python3 -m unittest discover -s tests -v`; single test: `python3 -m unittest tests.test_engine.ClassName.test_name -v`. There is no lint/typecheck/CI config — tests are the only gate.
- Pure-stdlib Python (>=3.9). Do not add runtime dependencies; POSIX serialization relies on `flock`.
- `tests/test_neutrality.py` fails if any domain vocabulary (math, biomedicine, finance, security, ML terms — see its denylist) appears in `src/adv_loop/*.py`. Worked field examples belong in `profiles/`, never in `src/`. Note the denylist includes the word "portfolio": the multi-workspace scheduler is named `fleet` for that reason.
- Engine internals and invariants are documented in `CLAUDE.md` and `docs/architecture.md`; the full wire contract is `protocols/loop.md` plus JSON Schemas in `schemas/`; the 4.0 design note is `docs/policy-4.0.md`.
- Test suites map to concerns: `test_engine` (2.0 spine), `test_exploration`/`test_moves` (3.0), `test_persistence` (governed stopping + version freeze rails), `test_formalization`/`test_overlays` (4.0 kernel), `test_intake`/`test_runner`/`test_sandbox`/`test_validators` (4.0 supervisor plane).

## Enforcement boundary

For a v2 task workspace, `events.jsonl` is authoritative. Never edit `state.json`, `attempts.jsonl`, `evidence.jsonl`, `task.md`, `decision-log.md`, or `report.md` to change task state. They are deterministic projections.

Use the controller for every transition:

1. `adv-loop next workspaces/<task-id>`
2. perform exactly the returned role and mode;
3. `adv-loop scaffold workspaces/<task-id>` if you need the submission shape;
4. `adv-loop record workspaces/<task-id> <attempt.json>`;
5. if rejected, correct the cited contract error without discarding the failed work;
6. `adv-loop audit workspaces/<task-id>` before any terminal claim.

A directive is bound to the event head. If it becomes stale, fetch `next` again; do not force the old result into state.

On 3.0 workspaces the loop also schedules exploration: when `next` returns `explorer/ideation`, generate the seed batch from a fresh minimal context (do not paste in your accumulated history); when it returns `critic/triage`, judge every seed from another fresh context. `next --explore` offers a voluntary ideation alternate whenever the free researcher slot is up.

On 4.0 workspaces the loop also schedules harness surgery: when `next` returns `surgeon/overlay_diagnosis`, classify the repeating condition honestly (a harness gap, a research failure, or a true human dependency) and, for a gap, propose a minimal tighten-only overlay delta; when it returns `critic/overlay_review`, judge that proposal from a fresh context — adopt only what you would be willing to be governed by. `payload/` and the sandbox root are evidence source material, never projections: reference them from evidence, never hand-edit them to change state. The only honest way to claim a checker accepted an artifact is the `adv-loop validate` verdict embedded as evidence with its artifact hash as the fingerprint.

## Which stop is which (4.0)

| The condition | The action |
|---|---|
| The loop's own contract lacks a rung, kind, hook, or tool registration | `surgeon/overlay_diagnosis` → reviewed overlay → continue. Never `ask-human`. |
| Authorization, credentials, private data, or a real external dependency | `ask-human` with the honest classification, or the blocked gate with a retest premise. |
| A safety, legal, privacy, or authorization boundary | `unsafe`, immediately; it is never gated behind diagnosis. |
| Ordinary scientific failure | The escalation ladder. The surgeon is not an escape hatch from a hard problem. |

## Session-end contract

A session working an ADV Loop workspace may end only in one of three ways:

1. the workspace is **terminal** (`completed`, `blocked`, `unsafe`, `budget_exhausted`);
2. the workspace is **awaiting_human** — you recorded the question with `adv-loop ask-human`, so the pause is honest, visible, and resumable;
3. a **supervisor is armed** — the obligation is handed to something that will resume it (a driver, a scheduled runner, or an operator who acknowledged the pending directive).

`adv-loop guard workspaces/*` enforces this mechanically: it exits nonzero while any workspace holds an unmet obligation (pending directive, owed escalation, due retest). Wire it into your harness's stop hook so idling with owed work is impossible, e.g. for Claude Code `settings.json`:

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command",
      "command": "adv-loop guard workspaces/* || { echo 'ADV Loop still owes work — run adv-loop next, or record the pause with adv-loop ask-human.' >&2; exit 2; }"}]}]
  }
}
```

Never end a turn by describing what you would do next on an active workspace: do it, or record the question with `ask-human`. Deferral (scheduling future work, writing a summary) is not work; the guard does not accept it.

If the event log lives only in an ephemeral machine, configure the durability hook (`loop-config.json`: `{"on_record": ["git", "..."]}` or a push script) so every append is persisted, and treat an on_record failure line as an emergency, not noise.

## Prime directive

Do not stop because the first method failed. Stop only when one of these terminal states is proven:

1. `completed`: Every acceptance criterion has direct evidence and an independent verification pass.
2. `blocked`: A specific external dependency requires human action, and all safe alternatives are exhausted.
3. `unsafe`: Continuing would violate a stated safety, legal, privacy, or authorization boundary.
4. `budget_exhausted`: A configured hard resource limit was reached. This is never the same as completion.

## Required loop

1. Restate the task as measurable acceptance criteria.
2. Build an evidence map. Each criterion must point to concrete evidence.
3. Select a strategy that is materially different from failed strategies.
4. Execute the smallest useful experiment.
5. Record the attempt before interpreting it.
6. Critique the result and identify uncertainty.
7. Verify claims through an independent method whenever possible.
8. Update state, memory, and the next strategy.
9. Continue until a terminal-state gate is satisfied.

The structured attempt record must include the raw observation separately from its interpretation and uncertainty. A researcher's self-reported outcome does not reset failure state; only the required fresh-context critic can validate progress.

## Anti-stagnation rules

- Never repeat an unchanged strategy after it fails.
- After two similar failures, change at least two dimensions: source type, tool, decomposition, query framing, model role, or verification method.
- After three failures, perform a contradiction search and ask what assumption may be false.
- After five failures, re-plan from the original task without relying on the current plan.
- Treat partial progress as evidence, not completion.
- A plausible answer without traceable evidence is incomplete.
- Never reuse any earlier research strategy fingerprint, even after intervening attempts (the one exception is a `replicate` move of a validated attempt, which is fingerprinted by its target).
- Never skip a scheduled contradiction search, decomposition, fresh replan, blocker audit, verifier, or synthesizer phase — and on 3.0, never skip a demanded ideation, triage, survey, barrier probe, or combine rung. The 3.0 ladder is cyclic and per-criterion: there is always a next demand, so "nothing left to try" is never a valid conclusion.
- On 3.0, name the basin (conceptual family) of every experiment honestly; when a basin closes after repeated confirmed failures, re-enter it only with a genuine representation shift, not a rewording.
- Adapter failure and a supervisor pause leave the task `active`; they are not proof of blockage.
- A 3.0 `blocked` is a falsifiable claim, not a burial: state the premise and its retest schedule, run `adv-loop retest` when due, record the outcome, and `unblock` the moment the premise fails.

## Workspace discipline

Each task gets `workspaces/<task-id>/` containing:

- `task.md`: original request, scope, constraints, acceptance criteria
- `state.json`: machine-readable loop state
- `attempts.jsonl`: append-only attempt ledger
- `evidence.jsonl`: source and artifact ledger
- `decision-log.md`: important choices and rejected alternatives
- `report.md`: final answer with evidence mapped to criteria

V2 also requires `events.jsonl` as the append-only source of truth. `.pending-event.json` may appear briefly during a crash-safe write. Use `adv-loop audit --repair` to finish that write or rebuild projections; never delete the journal manually.

Legacy v1 workspaces have no trustworthy event history. Continue them with `adv-loop migrate <legacy-workspace>`, which preserves the source and reopens its criteria under v2 proof gates.

Do not delete failed attempts. Failed paths are memory that prevents loops.

## Safety and honesty

Persistence does not override authorization, user intent, resource budgets, or safety. Never claim success to escape the loop. Clearly distinguish facts, inference, uncertainty, and missing evidence.

Independent verification means a different context ID and a different evidence provenance path, not the same reasoning relabeled as `verifier`. Evidence fingerprints and actor identities must describe what the adapter actually observed. Process validation is not permission to fabricate provenance.
