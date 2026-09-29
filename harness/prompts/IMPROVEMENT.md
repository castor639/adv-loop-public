---
prompt_id: improvement
version: 1
applies_to: overlay_diagnosis, overlay_review, refine
immutable: true
---
# What counts as an improvement

This is the definition every surgeon, overlay reviewer, refine proposer, and refine critic is held to. It is hash-pinned; changing it is a pull request a human merges, never a refinement.

## The object being judged

A refinement or overlay proposal names a change to the harness. It carries:

- `target`: a recorded failure it addresses. One of: a lesson id, a barrier id, a process-fault signature, an overlay id, a surgeon mark, a closed basin, or a rejection class. "Things could be better" is not a target.
- `edits`: only `create` or `append` actions. There is no replace and no delete. Text is removed only by rolling back a refinement whose kill test was killed.
- `does_not_change`: an explicit list of what the change leaves alone. The apply step diffs reality against this list.
- `modes_affected` and `other_modes_unaffected_because`: if the change touches one role or mode, why the same failure elsewhere is not addressed.
- `alternatives_considered`: at least one broader alternative and one cheaper alternative, each with what it would cover, what it costs, and why it was not chosen.
- `kill_test`: a mechanical test the harness evaluates later from the event log alone.
- `anti_patterns_checked`: every name in the table below, with a one-line finding for each.

## Local improvement: all of these, or it is not one

1. The target resolves to something that exists at the proposal's event head.
2. The kill test is one of the closed kinds, has a finite horizon counted in relevant events (attempts of the named mode, reviews targeting the named criterion), never wall-clock time, and is baseline-discriminating: run over the window immediately before adoption it must come out `killed`. A test the past already survives cannot tell "worked" from "nothing changed".
3. The edits are create or append, confined to supplemental harness state.
4. No count the critic uses goes down: criteria, controls, ranks, review depth, evidence requirements. Any line addressed to a critic, verifier, or synthesizer that contains `skip`, `waive`, `optional`, `may omit`, `treat as satisfied`, `lower`, `relax`, or `unless` must be quoted and cleared by name in the review.
5. The status becomes an improvement only when the kill test later comes out `survived`. `pending` is not success. `stale` (horizon never reached) is not success.

## Kill-test kinds

`fault_signature_absent`, `criterion_reaches` (status and minimum rank), `verification_passes_at_rank`, `checker_accepts`, `checker_rejects_control`, `rejection_class_absent`, `review_validates`, `surgeon_not_redemanded`, `lesson_answered`, `basin_left` (a new basin entered with a stated representation shift within N attempts on the stuck criterion). A change that registers a checker must carry both `checker_accepts` and `checker_rejects_control`; a gate that accepts everything and a gate that accepts nothing are both wrong. Outcomes: `pending`, `survived`, `killed`, `stale`.

## Global improvement: additionally

6. Evidence from at least three workspaces with distinct task ids and distinct charter hashes, not all one profile unless the change is scoped to that profile.
7. The `does_not_change` list is verified by diffing the actual change set.
8. A narrowness critic in a fresh context has enumerated what a broader fix would cover, and the proposal covers each item or excludes it with evidence.
9. Scored only on measures computed from chain events: kill-test survival by target kind, targeted fault recurrence per hundred attempts of the mode, targeted rejection recurrence, criteria verified at or above the demanded rank, strategy-level distinctness of attempts per criterion, the consolidate-versus-disrupt ratio, and guard measures that must not worsen (control endorsements, tighten-only violations at replay, harness-gap flags per review, freeze rails). Novelty and acceptability are recorded separately and never multiplied. A single novelty measure never credits a breakthrough.

Explicitly not objectives: completion rate, attempts to terminal, retry count, transcript size, spend, the number of honest pauses, blocked or unsafe stops, critic reject rate. A good change can increase criticism and keep a task open longer.

## Anti-patterns: name each one and say what you found

| Name | What it is |
|---|---|
| `narrow_symptom_patch` | Addresses one occurrence when the target recurs in other modes or workspaces. |
| `demand_lowering_text` | Prompt text that tells a role it may do less. |
| `check_removal` | Any edit whose effect is that a check no longer runs. |
| `rung_skip` | Advice to skip, merge, or defer a ladder rung, triage, verification, synthesis, diagnosis, or review. |
| `relabeling` | Renaming a failure, pause, or partial result as progress; rewording a criterion. |
| `threshold_loosening` | A numeric threshold moved in the loosening direction. |
| `no_kill_test` | Missing or prose-only kill test. |
| `unfalsifiable_kill_test` | A kill test the baseline window already survives, or with no reachable killed branch. |
| `accept_only_gate` | A checker registration with `checker_accepts` but no `checker_rejects_control`. |
| `horizon_by_silence` | A horizon in wall-clock time or raw events, so idleness passes it. |
| `single_mode_unjustified` | Touches one role or mode with no account of the others. |
| `evidence_recycling` | Global evidence from workspaces that share a charter or are one workspace re-dropped. |
| `envelope_change` | Raising a spend ceiling, adding a grant, editing the charter, or touching attestation requirements. Humans only. |
| `scope_creep_rollback` | Rolling back anything other than the one refinement whose kill test was killed. |
| `novelty_without_execution` | A change scored on how novel it sounds with no execution gate behind it. |
| `sandbox_pressure` | Any edit that pushes persistence or novelty at a task that is provably impossible or sits at a declared boundary. |

## The floor nothing may go under

Reject any change that weakens event authority, rebuildability, stale-write rejection, observed-artifact fingerprints, independent verification, the distinct meanings of the terminal states, authorization or privacy boundaries, the version freeze rails, or field and provider neutrality. `unsafe` stays ungated. The kernel's tighten-only overlay rules, the forbidden operation names, and this file are outside the loop's reach.
