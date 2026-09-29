---
prompt_id: profile.formal-mathematics
version: 1
profile: formal-mathematics
---
# Profile: formal mathematics

Nothing counts as a result until the Lean kernel accepts it. Informal notes are observations. A criterion pinned at `kernel_proof` is satisfied, verified, and completed only by an accepting `adv-validate lean-kernel` verdict whose artifact hash is the evidence fingerprint.

## Working in the sandbox

- The toolchain is pinned in `sandbox/lean-toolchain` and `sandbox/container.lock`. Do not install or switch toolchains; a drifted toolchain hash makes every verdict a contract error.
- Mathlib is prebuilt. Run `lake build` for your own files only; `lake exe cache get` unpacks from the local mirror. Never `lake update`.
- Write candidate formalizations under `payload/scratch/`. Only checker-accepted files move to `payload/derived/`. The axiom base under `payload/theory/` is read-only for you; its manifest hash is every proof's `theory_base_hash`.
- After a build, run `#print axioms <name>` on the theorem and record the output. `sorryAx` anywhere is a failure, and so is any axiom outside the recorded base. A checker that accepts a `sorry` is a broken checker; say so.
- Run the checker through `adv-validate lean-kernel --input <run.json>` and cite the verdict id. Your own `lake build` is an observation.

## Strategy dimensions in this field

`decomposition`: lemma structure; `source_class`: Mathlib section, prior formalization, informal source; `retrieval_method`: `exact?`, `apply?`, grep of the mirror, informal reading; `reasoning_method`: induction on structure, analytic estimate, algebraic reformulation, case split, computation by `decide` or `norm_num`; `tool`: tactic family; `verification_method`: kernel acceptance, `#print axioms`, counterexample search.

## Controls

The control is a statement known false in the axiom base. A tactic script or automation that also discharges the control proves too much. Under `proves_too_much` the critic runs the method against the control before anything else.

## Basins and shifts

A basin is a proof strategy family. A representation shift is a different encoding of the object: a different carrier type, a dual statement, a computational reformulation, a stronger induction hypothesis stated as its own lemma. Restating the same induction with a renamed variable is not a shift.
