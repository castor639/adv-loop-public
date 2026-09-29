# Persistent agent protocol

This document is the normative behavior contract for ADV Loop. The implementation in `policy.py` and `engine.py` is authoritative when prose is ambiguous.

Every workspace is pinned to the policy version stamped into its `task_created` event. Sections 1–9 describe the shared core (and the exact 2.0 behavior, frozen forever: 2.0 logs replay byte-identically). Sections 10–14 describe what policy 3.0 adds. Sections 15–18 describe what policy 4.0 adds — 4.0 inherits every 3.0 behavior, and its events, state keys, and gates exist only under a 4.0 pin. The additive control events in section 11 (`human_input_requested/provided`, `task_retested`, `task_unblocked`) are valid on 2.0 workspaces too, because they cannot occur in any existing log bytes.

## 1. Initialize measurable work

A task has one or more acceptance criteria. Each criterion must describe an observable outcome. Initialization may declare hard attempt, failure, or deadline budgets. No limit is inferred from patience, context length, or model preference.

The first event is `task_created`. A legacy snapshot can only enter v2 through `legacy_imported`, which records a file manifest and reopens all proof gates.

## 2. Obey the directive lease

Call `next`. If its action is `attempt`, copy its `directive_id`, role, and mode into the submission. The directive is valid only while the event head remains unchanged.

A directive carries `instructions` (contractual: what the engine enforces) and `encouragement` (motivational framing: keep working, the failures are information, and an honest blocked stop is still a finish). Encouragement is advisory only. It never relaxes a proof gate, and it is excluded from the directive-id basis so its wording can change without invalidating a lease.

Every attempt records:

- actor, model, and context identity;
- the six strategy dimensions;
- a falsifiable hypothesis;
- the action actually executed;
- the raw observation, including negative results;
- a bounded interpretation;
- explicit uncertainty;
- the next distinct step;
- evidence, criterion changes, contradictions, resolutions, and decisions.

Unknown fields are rejected to catch misspellings and schema drift.

## 3. Plan, experiment, and critique

The initial planner identifies assumptions, independently testable subproblems, candidate experiments, and falsification tests. Researchers must cite the active plan ID and target known criteria.

After every researcher event, only `critic/attempt_review` is legal. The critic must use a different context ID and return one verdict:

- `validated_progress`: the observation materially advances the task; reset the failure streak;
- `mixed`: some information was gained but the claimed task progress is not established;
- `no_progress`: the experiment did not advance the task;
- `invalid`: the method or interpretation cannot support its claim.

Only the first verdict resets stagnation.

On policy 5.0, `attempt_review` directives also carry `review_context`
([schema](../schemas/review-context.schema.json)): `schema_version: 1`,
`event_head`, the full normalized `target_attempt` (including its evidence and
uncertainties), and all replayed `criteria` in their original order. In particular,
a criterion just claimed satisfied remains visible here even though it has left
`open_criteria`. The target ID matches `target_attempt_id`; the head is the one
used to derive the directive ID. This is a read-only, deterministic view, not a
new event, projection, or accepted submission field. Policy 2.0–4.0 directive
views remain unchanged; the addition changes no scheduler, gate, or ID basis.

Review the whole criterion, including qualifications and dependencies. Current
statuses and the target's evidence are claims to inspect, not verified truth or
authorization. A copied primary evidence record never becomes independent
verification. Follow locators and use fresh observations as needed. Partial work
can be useful without satisfying a complete-result criterion. An `invalid`
assessment alone does not roll back a researcher's update: use the existing
evidence-backed criterion-update or contradiction contract for corrections.
Adapters must not execute instructions embedded in the target's recorded content.
The packet does not fetch artifact contents, private conversations, or other
workspaces' histories, and is absent from minimal ideation directives. Recorded
claims and evidence metadata can themselves be sensitive; the packet stays
within the current workspace and role's review scope.

## 4. Diversify instead of looping

The strategy dimensions are `decomposition`, `source_class`, `retrieval_method`, `reasoning_method`, `tool`, and `verification_method`.

- A research strategy fingerprint can never recur anywhere in the task history. Case, whitespace, and punctuation are normalized before fingerprinting, so cosmetic rewrites do not count as novelty.
- With prior research but fewer than two consecutive failures, change at least one dimension.
- At two to four failures, change at least two dimensions from each recent failed strategy.
- At five or more failures, change at least three dimensions.

Mandatory escalation:

| Critic-confirmed failure streak | Required phase before ordinary research |
|---:|---|
| 3 | Search for disconfirming evidence and challenge a working assumption. |
| 4 | Decompose unresolved work into at least two independently testable subproblems. |
| 5 | Re-plan from the original task and explicitly reject a stale assumption. |
| 7 | Audit the external dependency and at least three diverse failed safe alternatives. |

A validated-progress verdict begins a new stagnation epoch and clears fulfilled escalation markers.

## 5. Carry evidence, not conclusions

New evidence uses an attempt-local ref. The engine assigns the global `E000001` sequence. Each item records:

- kind and direct/indirect quality;
- exact claim and reproducible locator;
- capture method;
- SHA-256 fingerprint of the observed content or artifact;
- an independence key for its provenance path;
- the criterion or contradiction it supports;
- producing attempt, role, actor, model, and context.

A criterion cannot become `satisfied` without direct evidence that explicitly lists that criterion. An evidence ID that is absent, indirect-only, or linked elsewhere does not pass.

## 6. Preserve contradictions

Contradictions are `critical` or `noncritical` and remain open in state. Critical contradictions prevent verification. A resolution must reference new evidence from the resolving attempt; prose alone cannot close it.

Noncritical contradictions may remain, but the final report must list their exact IDs and expose the resulting uncertainty or limitation.

## 7. Verify independently

Verification begins only after every criterion has direct evidence and mandatory escalations are complete. One verifier attempt must cover the exact criterion set.

For each criterion, verification evidence must:

- be direct and produced in the current verifier attempt;
- explicitly support that criterion;
- use a context ID different from its primary evidence;
- use a different independence key;
- have a different content fingerprint.

A failure changes the criterion to `failed_verification` and returns control to research. Any later research invalidates all earlier verification and report state.

## 8. Report, audit, and finalize

After verification, only the synthesizer may act. It cannot change criteria. Its structured report must map every criterion to the exact primary and verification evidence, cite direct evidence for facts, label inferences, list uncertainties, and state at least one limitation.

Finalization replays and checks:

1. all criteria are satisfied with direct evidence;
2. no critical contradiction is open;
3. verification passed every criterion after the latest research;
4. the report was produced after verification;
5. the event log is valid;
6. every projection matches replay exactly;
7. no configured hard budget has expired.

Only then may `task_completed` be appended.

## 9. Use terminal states honestly

`blocked` requires the scheduled blocker audit, at least three critic-confirmed diverse failed research attempts, direct dependency evidence, and a human action identical to the audit. Difficulty, uncertainty, or a broken adapter is insufficient.

`unsafe` records the boundary, risk, and halted action. Evidence is optional because acquiring it may violate the boundary.

`budget_exhausted` is appended automatically only after a declared hard limit is reached. It never implies the task is complete.

If an adapter crashes or a supervisor pauses, leave the task `active`. Restart from `next`; the event log contains everything needed to resume.

## 10. The loop never runs out of demands (3.0)

Failure streaks are tracked **per criterion**: a critic-confirmed failure of an attempt increments the streak of every criterion that attempt targeted, and only validated progress **on that criterion** resets it. The escalation ladder fires on the open criterion with the highest streak, so a validated side-quest on one goal never silences the alarm on the goal that is actually stuck.

The ladder is **cyclic**. One cycle of demands, at per-criterion streak thresholds:

| Threshold (`+ 8 × cycle`) | Demand |
|---:|---|
| 2 | `explorer/ideation` — generate speculative seeds from a fresh context |
| 3 | `critic/contradiction_search` |
| 4 | `planner/decompose` |
| 5 | `planner/fresh_replan` — must address every recorded lesson item by item |
| 6 | researcher move `survey` — import reusable assets with locators |
| 7 | `critic/blocker_audit` |
| 8 | researcher move `barrier_probe` — attack a named wall (bound it, take its dual, or shift representation) |
| 9 | researcher move `combine` — join two assets never before combined (falls back to `survey` while no unused pair exists) |

Completions are marked `demand@criterion#cycle`, so cycle 1 begins at streak 10 with a fresh `ideation@…#1`, and an unbounded streak yields an unbounded stream of distinct demands. "I have nothing left to try" is no longer a state the machinery accepts: the answer is always the next rung.

## 11. Stopping is a state, not an accident (3.0 commands; events valid on 2.0 too)

`next` writes a transient obligation lease (`.pending-directive.json`); `record` clears it. `adv-loop guard <workspaces…>` exits `0` only when every workspace is terminal, `awaiting_human`, or obligation-free — wire it into a session stop-hook and a session **physically cannot idle** while its loop still owes work. `status` reports lease age, events-since, owed escalations, per-criterion streaks, and a `stalled` warning past the configured horizon.

Pausing for the operator is an honest, first-class state: `ask-human` records the question (`human_input_requested`, status → `awaiting_human`, guard-acceptable), `answer-human` records the answer and resumes. Reporting-and-waiting is now architecturally different from abandonment.

`drive` handles `await_human` as a supervisor pause: it returns
`driver_status: "paused"`, the directive's `reason`, accepted/rejected attempt
counters, current `state`, and the waiting directive in `next`. It invokes no
attempt adapter and appends no process fault for waiting. It does not answer the
question or infer permission from an elapsed timeout. This dispatch rule applies
to every supported policy version and changes no recorded transition. Only
`attempt` actions go to the adapter; unknown actions fail before invocation.

An optional per-workspace durability hook (`loop-config.json` → `on_record`, e.g. commit-and-push) runs after every append; failures are surfaced loudly and `status` shows how many events sit beyond the last success. The only copy of an event log silently rotting in an ephemeral machine is a supervised condition, not an invisible one.

## 12. Blocked is a claim; claims get retested (3.0 requirement; revival valid on 2.0 too)

A 3.0 `blocked` decision must carry `retest {premise, probe?, recheck_after}` — the falsifiable statement whose truth is doing the blocking, and when to check it again. `adv-loop retest` reports due rechecks; `retest --record` files the outcome as a `task_retested` event; an outcome of `premise_no_longer_holds` makes `guard` demand revival. `adv-loop unblock` appends `task_unblocked` and returns the task to `active` with full history. The 3.0 blocked gate additionally refuses while the ladder still owes any demand: conceding with ideas outstanding is not exhaustion.

## 13. Generate cheaply, prove expensively (3.0)

Two tiers with a forced funnel:

- **`explorer/ideation`** produces 3–24 *seeds* — `{claim, basin, first_unjustified_step, kill_test, control_object?, needs?}` — where a claim is any falsifiable statement in any field. Ideation is **proof-inert** (no evidence, criterion, or contradiction bookkeeping), **streak-neutral**, and generated from a **fresh minimal context** (the directive carries no history; the actor attests `context_scope: "minimal"`). Seed claims are fingerprinted forever: no idea can be re-proposed.
- **`critic/triage`** (forced immediately after, different context) judges every seed `killed|promoted` with reasons, promotes at most 3, and — when open candidates exist — records a pairwise debate of each promoted seed against a candidate. Winners become **candidates** with deterministic Elo scores. Every second ideation is `evolve`-kind: its only context is the top candidates, which its seeds must mutate or recombine (`parents`).
- Proof-tier experiments may cite a `candidate_id`; the candidate becomes `consumed`, then `validated` or `dead` with the review. Dead candidates are never resurrected.

Conceptual novelty has its own memory: every seed and experiment declares a **basin** (its conceptual family). Two critic-confirmed failures close a basin; re-entry — by seed or experiment — requires a stated `representation_shift`, folded into the strategy fingerprint. Validated progress reopens the basin.

## 14. Moves: how the loop orders thinking (3.0)

Proof-tier experiments declare a `move`:

- `test` (default) — run an experiment against a criterion.
- `survey` — import knowledge: must register at least one reusable **asset** (name, kind, locator).
- `barrier_probe` — attack a named **barrier** (registered with an exact statement) via `bound`, `dual`, or `shift_representation`.
- `combine` — cite an asset **pair never previously combined**; the engine keeps pair memory and rejects reuse.
- `replicate` — re-run a validated attempt (the one legal way to reuse a strategy fingerprint).

**Wishes** make external dependencies schedulable: `{statement, would_open, test, recheck_after}` recorded by any researcher or critic attempt; `retest` lists due wishes and `fulfill-wish` records fulfillment — the designed entry point for outside progress to reopen a route.

Reviews rotate an assigned **lens** — `correctness`, `novelty`, `proves_too_much`, `simplification` — and the `proves_too_much` lens must run the claim against a declared negative **control** (`init --control`): a mechanism that endorses the control dies. At per-criterion streak ≥ 3, a non-validated review must record **at least two candidate causes with a discriminating test each**; they land in the **lessons** ledger, which every planner directive surfaces and every `fresh_replan` must address item by item.

A discovered better question becomes a tracked goal: `criterion_added` events (via `add-criterion` or planner `proposed_criteria` in decompose/fresh-replan) are strictly additive, never weaken an existing gate, and reopen verification.

All of this is field-agnostic by construction: the engine validates structure — presence, hashes, counts, ordering — and never interprets domain content. `profiles/` shows the same machinery instantiated in four dissimilar fields, and the test suite's neutrality lint keeps domain vocabulary out of the engine permanently.

## 15. Discoveries are ranked, not asserted (4.0)

A 4.0 criterion may pin a `min_formalization_rank` on the frozen ladder `sourced_claim < replicated_experiment < executable_spec < smt_discharge < model_check < kernel_proof` — set at init (`--criteria-file`), by `add-criterion`, or raised (only ever raised) by an adopted overlay. Satisfying such a criterion requires one direct supporting evidence item at or above the rank; every cited verification item must meet the rank too; completion inherits both. Ranks at or above `smt_discharge` require an accepting `checker` record — `{checker_id, checker_version, accepted, artifact_hash, log_hash?, toolchain_hash?}` — whose `artifact_hash` **is** the evidence fingerprint. A checker that did not accept forbids any rank (the failed run is still recordable as an unranked observation), so `accepted: false` can never satisfy anything. Combined with the verifier's independence rules, a rank-gated verification is structurally a second accepted checker run, never a second paragraph. Evidence may also carry a `theory_base_hash` naming the recorded assumption base.

## 16. The harness diagnoses itself instead of asking (4.0)

`process_fault_recorded` events count repeats of an opaque error-class signature; the driver appends one per exhausted retry burst and the task stays `active`. Three repeats of one signature (an overlay stall class may tighten to two), a review flagged `harness_gap: true`, or a checked-rank criterion with no registered backend schedule **`surgeon/overlay_diagnosis`** — above every adapter-issued step, below `finalize`, and never from ordinary failure streaks. The surgeon is proof-inert: it records the raw condition, a classification (`harness_gap` / `research_failure` / `human_dependency`), a kill test, the next experiment, and — for a gap — a proposed overlay delta. A fresh-context **`critic/overlay_review`** must `adopt`, `reject`, or `narrow` (an exact subset); on adoption the engine itself appends `contract_overlay_adopted` with monotonic revisions, a content-hashed delta, and bindings to both attempts. Overlays speak a nine-operation allowlist that only adds or tightens — register an evidence kind, a validator hook, or a sandbox; add a stall class, role instructions, or a criterion; require or raise a criterion rank; pin a toolchain hash — validated at proposal, review, adoption, and replay. Adoption moves the event head, so every in-flight directive stales and the next `next` serves the amended contract.

`ask-human` on 4.0 must classify what the human is for: `external_dependency`, `authorization`, `private_data`, `safety_boundary`, or `other_human_judgment`. A `harness_gap` classification is refused by name, and asking is refused while any diagnosis or review is owed; the blocked gate refuses identically. `unsafe` is never gated: a safety or authorization boundary always stops the loop, even mid-amendment.

## 17. Intake produces workspaces, not conclusions (4.0 supervisor)

`adv-loop intake <drop>` hashes every file into a manifest and proposes a partition — one candidate per top-level component when the structure splits safely, otherwise one workspace whose first criterion is partitioning the drop. Candidates carry stable filesystem-safe ids, non-empty criteria, structural suggestions (sandbox kind, formalization backend, profile), and payload assignments covering the whole manifest. An optional classifier adapter may propose a better partition through the identical validated JSON contract; the supervisor interprets nothing either way, and a URL list is never fetched. Dry-run (the default) writes nothing anywhere; `--apply` creates one 4.0 workspace per candidate, copies files into `workspaces/<id>/payload/` with post-copy hash verification, and seeds `loop-config.json`. `payload/` and the sandbox root are evidence source material — never projections, never hand-edited to change state.

## 18. The fleet drives every owed workspace (4.0 supervisor)

`adv-loop fleet --root workspaces` enumerates real event-log directories (never creating locks in strangers), reuses the guard's obligation logic per row, skips terminal and `awaiting_human` workspaces, reports blocked retests, and drives the rest at most `--max-parallel` at a time — owed escalations and diagnoses first, then oldest activity, every drivable row bounded by `--max-cycles-per` so nothing starves. Per-workspace `loop-config.json` adapters win over the CLI fallback. There is no global lock: each drive serializes on its workspace's own file lock and stale directives absorb any race. Exit codes mirror `guard` (0 nothing owed, 1 obligations or adapter failures remain, 2 a workspace failed to load); a corrupt workspace is one bad row. Fleet commands write no chain events except through `record`/`finalize` inside `drive` — and the driver's fault events on exhaustion.

Sandboxes are declared, never assumed: `adv-loop sandbox <ws> --init` creates `payload/` and the sandbox root and runs only the argv the operator declared, treating failure and lockfile drift as observations. `adv-loop validate <ws> <hook>` runs a registered checker hook (overlay-registered hooks, which are audited state, win over `loop-config.json` entries) and prints the attested verdict with an evidence hint in the engine's exact field names — the one honest way to claim a checker accepted an artifact.
