# Profile: formal mathematics (proof-kernel backed)

A researcher constructing theory the way automated theory construction does it in Lean 4:
a pinned toolchain, an explicit axiom base, candidate statements generated cheaply, and
*nothing* counted as theory until the proof kernel accepts it. Informal notes are not
results. The loop enforces exactly that shape: a criterion pinned at `kernel_proof` rank
cannot be satisfied, verified, or completed by prose — only by an accepting checker
record whose artifact hash is the evidence fingerprint.

| Concept | Instantiation |
|---|---|
| **criterion** | "The statement is proved from the recorded axiom base and the proof term is accepted by the Lean kernel" — declared with `min_formalization_rank: kernel_proof`. |
| **seed / kill test** | Seed: "the bound tightens to O(n log n) under convexity." Kill test: a small counterexample search, or the kernel rejecting the sketch's first lemma. |
| **control** | A statement known to be false in the axiom base (e.g. the negation of a proved theorem). A "proof method" that also discharges the control proves too much and dies. |
| **basin** | The proof strategy family — "induction on structure," "analytic estimates," "algebraic reformulation." Two confirmed dead ends close it; re-entry needs a representation shift. |
| **asset** | A checker-accepted lemma in `payload/derived/`, a tactic script, a formalized definition — each with a locator and the artifact hash the checker reported. |
| **survey move** | Import and pin the relevant Mathlib sections and prior formalizations as assets with locators. |
| **combine move** | Join two derived lemmas never used together into a candidate composite proof. |
| **barrier / probe** | Barrier: "the induction hypothesis is not strong enough." Probe by `bound` (weaken the goal), `dual` (contrapositive form), or `shift_representation` (a different encoding of the object). |
| **wish** | "If the pending Mathlib port of X lands, the algebraic route reopens" — rechecked on a date. |
| **replicate move** | Re-run the accepted proof term through the checker in a clean sandbox; the second run is the independent verification evidence. |
| **lens** | `correctness` (does the term prove the stated goal, not a weaker one?), `novelty` (is it already in the base?), `proves_too_much` (does the method also accept the control?), `simplification` (is a shorter derivation available?). |
| **lesson** | Cause A "the definition unfolds differently than assumed," test = check the elaborated form; cause B "the axiom base is missing a closure property," test = state and attempt the missing lemma. |

## Workspace layout

```
workspaces/<id>/loop-config.json     # sandbox + validator hook declarations
workspaces/<id>/sandbox/             # uv venv + lean-toolchain + lakefile; pins in lockfile_hashes
workspaces/<id>/payload/theory/      # the axiom base; its manifest hash is every proof's theory_base_hash
workspaces/<id>/payload/derived/     # ONLY checker-accepted artifacts move here
workspaces/<id>/payload/scratch/     # current attempts; allowed to fail
```

`loop-config.json`:

```json
{
  "sandbox": {
    "kind": "uv",
    "root": "sandbox",
    "command": ["uv", "run", "--frozen"],
    "setup": ["uv", "sync", "--frozen"],
    "lockfile_hashes": {"sandbox/uv.lock": "<sha256>", "sandbox/lean-toolchain": "<sha256>"}
  },
  "validators": {
    "lean-kernel": {"command": ["python3", "checkers/run_lean.py"], "in_sandbox": true, "rank": "kernel_proof"}
  }
}
```

The flow per attempt: write the scratch formalization, run `adv-loop validate <ws>
lean-kernel`, and embed the printed verdict as evidence — `accepted: true` with the
artifact hash as the fingerprint, at `kernel_proof` rank. A failed check is recorded as
an unranked observation and recycled; its strategy fingerprint can never be reused. A
`pin_toolchain_hash` overlay makes toolchain drift a contract error.

Honest limit, stated plainly: the kernel verifies the *shape and hashes* of the checker
attestation, exactly as it verifies every evidence fingerprint. Lean supplies the
mathematics; the loop supplies the discipline that nothing else counts as mathematics.
