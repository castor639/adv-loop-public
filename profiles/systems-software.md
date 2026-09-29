# Profile: systems / software engineering

An engineer landing a change in a dropped codebase, where the failure mode is "works on
my machine" and green-by-assertion. The loop pins the environment, runs checks through
registered hooks, and refuses completion until the property actually held under a
mechanical check.

| Concept | Instantiation |
|---|---|
| **criterion** | "The invariant holds on the declared cases and the checker run is recorded" — at `executable_spec` for a property-test suite, `smt_discharge` for a solver-closed goal, `model_check` for an exhaustively checked spec. |
| **seed / kill test** | Seed: "the race is in the retry path." Kill test: a stress case that forces the interleaving in under a minute. |
| **control** | A known-buggy build or a deliberately violated invariant. A test suite that passes the control is not testing anything and dies under `proves_too_much`. |
| **basin** | The mechanism family — "locking discipline," "idempotent redesign," "queue re-architecture." Two confirmed dead ends close it. |
| **asset** | A minimal reproducer, a pinned dependency set, a profiling trace, a bisection result — each with a locator. |
| **survey move** | Register the existing test harnesses, the upstream issue threads, the design docs as located assets. |
| **combine move** | Run the reproducer from AS0002 under the tracing harness from AS0005 — never joined before. |
| **barrier / probe** | Barrier: "the failure needs production traffic shape." Probe by `bound` (how small a trace still reproduces?), `dual` (what would make it *never* reproduce?), or `shift_representation` (a simulated load model). |
| **wish** | "If the upstream fix ships, the workaround criterion can retire" — rechecked on a date. |
| **replicate move** | Re-run the suite from a clean sandbox (`uv sync --frozen` then the hook) — the second run is the verification evidence. |
| **lens** | `correctness` (does the test check the invariant or the implementation?), `novelty` (is this already covered?), `proves_too_much` (does it pass the known-buggy control?), `simplification` (is a smaller fix sufficient?). |
| **lesson** | Cause A "the flake is timing-dependent," test = 100 repeats under load; cause B "the fixture masks the bug," test = run against the real store. |

## Sandbox and hooks

```json
{
  "sandbox": {
    "kind": "uv",
    "root": "sandbox",
    "command": ["uv", "run", "--frozen"],
    "setup": ["uv", "sync", "--frozen"],
    "lockfile_hashes": {"sandbox/uv.lock": "<sha256>"}
  },
  "validators": {
    "property-suite": {"command": ["python3", "checkers/run_props.py"], "in_sandbox": true, "rank": "executable_spec"},
    "static-analysis": {"command": ["python3", "checkers/run_lint.py"], "in_sandbox": true, "rank": "sourced_claim"}
  }
}
```

`adv-loop validate <ws> property-suite` returns an attested verdict; the agent embeds it
as direct evidence. `accepted: false` is a successful observation — it feeds the critic,
the streaks, and the ladder like any failed experiment. When the harness itself is the
recurring problem (a missing hook, an unpinned tool), the repeating fault signature
schedules `surgeon/overlay_diagnosis`, and a reviewed overlay registers the missing
piece — the operator is asked only for authorization, credentials, and real external
dependencies.
