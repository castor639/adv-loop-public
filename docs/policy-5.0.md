# Policy 5.0 — the autonomy plane

**Status: ALL phases (0–F) are IMPLEMENTED; the tests are the record.**
Policy 4.0 (`docs/policy-4.0.md`) made the loop an all-given architecture that still
expected an operator to attach compute, press enter, and answer questions. 5.0
removes the operator from the *run* by moving every human decision into the
**charter** — a front-loaded grant of budgets, tools, credentials, boundaries, and
escalation policy compiled from a one-sentence goal (`prompts/compile-charter.md`).
The target interaction, working end to end now:

> one sentence + the charter compiler → a charter → an autonomous run → an honest
> terminal state — with zero human turns unless the run would exceed its envelope.

Same discipline as 4.0: the kernel stays pure-stdlib and field-neutral; almost
everything below is adapters and supervisors outside the proof spine; the few kernel
touches are additive, version-gated, and freeze-railed. Tests are the only gate.

## Phase 0 — The charter (standing authorization) — IMPLEMENTED

The design shift: **the human is in the charter, not in the loop.** Budgets already
work this way (declared at init, enforced mechanically, never re-asked); 5.0
generalizes the pattern to authorization, credentials, data, and escalation.

- Charter document lives at `payload/charter.md` (`adv-loop init --charter <file>`
  copies it there); machine fields (`steward`, `spend_ceiling_usd`, `retest_probe`,
  `wish_probe`, `adapter`) live in `loop-config.json`. A drop arrives with its
  charter attached: `charter.md` at the drop root lands in every candidate's
  payload, and `charter.json` merges into every candidate's loop-config.
- Kernel touch (5.0 pin, additive): `task_created` may carry `charter_hash` —
  the SHA-256 of the charter file — surfaced in state so the contract the run was
  granted is audited into the chain. New inits default to 5.0 (`--policy 4.0/3.0`
  opt down); 2.0/3.0/4.0 freeze rails extended exactly as before.
- The driver puts `charter` in every adapter envelope, so the model knows what is
  pre-authorized and never wastes a turn asking for it.
- `unsafe` stays completely ungated — the one deliberately non-autonomous act. A
  precise §6 boundary makes it fire rarely and correctly; it is never removed.

## Phase A — The brain (reference adapter) — IMPLEMENTED

`adapters/claude_adapter.py` (official Anthropic SDK; its dependency never enters
`src/`; unit tests inject a fake transport and run offline):

- Reads the drive envelope, calls Claude (default `claude-opus-5`, adaptive
  thinking, effort/model/max-tokens via `ADV_LOOP_*` env) with role-tuned prompting
  per mode — planner, researcher, critic, verifier, synthesizer, explorer, surgeon,
  overlay reviewer — plus the charter on every call.
- Makes context independence **true by construction**: every invocation is a fresh
  subprocess and a fresh model conversation; the adapter mints `context_id` and
  overwrites `directive_id`/`role`/`mode`/`actor` mechanically, so the model cannot
  mislabel its identity even if it tries (pinned by test).
- Executes experiments with real tools: `run_in_sandbox` (argv, workspace-contained
  cwd) and `hash_artifact` (fingerprints computed from real files, never asserted);
  all parallel tool results return in one message; refusals surface as adapter
  failures, never fake submissions. Every call's token usage lands in the
  workspace's `.spend.jsonl` with an estimated cost.

Highest-leverage phase: it converts the entire 4.0 machine from "governed" to
"running".

## Phase B — Always on (zero-touch operation) — IMPLEMENTED

- **`adv-loop fleet --watch`**: a daemon pass loop (`--interval`, `--max-passes`)
  that stops honestly on the kill file (`<root>/.fleet-stop`), the global
  `--spend-ceiling-usd` (summed from the spend ledgers; per-workspace
  `spend_ceiling_usd` in loop-config also gates each drive), or quiescence (a pass
  where nothing owes work).
- **Watched inbox**: `--inbox <dir>` auto-intakes every waiting drop before each
  pass (processed drops move to `processed/`, broken ones to `failed/` with an
  error note); a drop's `charter.md`/`charter.json` arrive attached. Drop a folder
  in a directory; discovery starts.
- **The steward**: configured per workspace (`steward: {auto_answer: [...],
  command?: argv}`), consulted whenever a workspace is `awaiting_human`. Questions
  whose classification the charter covers are auto-answered with `answer-human`
  (builtin grant-citing answer, or the command hook's answer / escalate verdict) —
  ordinary audited chain events. Everything else — and always, hard-coded,
  `safety_boundary` — escalates to the operator.
- **Probe hooks close the last manual filings**: `retest_probe` and `wish_probe`
  argv hooks run when due; the fleet files `task_retested` / `fulfill-wish` itself
  and auto-unblocks on `premise_no_longer_holds`. A blocked task revives itself the
  day its premise dies.

## Phase C — Hands and honest artifacts — IMPLEMENTED (store deferred)

- Experiment execution and artifact hashing shipped with the Phase A adapter tools.
- Reference checkers under `checkers/` (outside `src/`), each speaking the
  `adv-loop validate` verdict contract: `command_checker.py` (accepted iff the
  declared command exits 0; artifact-bound hashes), `lean_kernel.py` (Lean kernel
  elaboration; a broken toolchain is a hook failure, never a verdict),
  `z3_discharge.py` (accepted iff `unsat`). Integration tests probe that the tool
  actually runs and skip honestly otherwise.
- The content-addressed artifact store remains an open extension point.

## Phase D — Compounding (discoveries teach each other) — IMPLEMENTED

- `adv-loop commons --root workspaces --out commons/`: per-workspace manifests
  (assets, lessons, open wishes, barriers, criteria, event-head provenance) plus
  completed reports, in a read-only directory other workspaces cite as survey-asset
  locators. Kernel-neutral — payload and locators only.
- Intake overlap join: a candidate's id embeds its payload content hash, so
  re-dropping the same content joins the existing workspace (`existing: true`)
  instead of forking a twin; dry-run proposals flag `existing` per candidate.

## Phase E — Self-direction (the fleet proposes its own next problems) — IMPLEMENTED

- `adv-loop frontier --root workspaces --out drops/incoming --apply -- <generator>`:
  field-neutral signals (completed summaries and next actions, open wishes, named
  barriers, blocked dependencies and premises) go to a generator adapter
  (`adapters/frontier_generator.py` is the shipped reference), which returns
  validated goal briefs — at most `--max-proposals`, kebab-case names, real
  acceptance-criteria sections, optional attached charters. Accepted proposals
  become inbox drops; the watch loop intakes them; the loop's existing gates handle
  proposal quality and the standing budgets handle proposal appetite. The kernel
  never generates or interprets a proposal.

## Phase F — Trust hardening — IMPLEMENTED

- **Keyed checker attestations.** `adv-loop keygen <ws>` creates a workspace secret
  (`.checker-key`, mode 600); `adv-loop validate` HMAC-signs every verdict it
  obtains with it; and with loop-config `checker_attestation: {"require": true}`
  the engine refuses any 5.0 checker record whose attestation is missing or does
  not verify. An adapter that merely *imagines* an accepting verdict can no longer
  land it; the attested value itself is audited into the chain.
- **Append-only mirrors.** `adv-loop mirror <ws> --to <dir>` (and loop-config
  `mirror_to`, run by every fleet pass) syncs the event log's new tail to a mirror
  file; a mirror that is not an exact prefix of the source is refused loudly as
  divergence evidence, never repaired silently.
- **Honest limits, stated plainly**: the attestation is a shared-key HMAC, not an
  asymmetric signature — a deliberately malicious local adapter with filesystem
  access can read the key; the `require` switch is machine-local config. What the
  mechanism eliminates is the realistic failure (a model inventing acceptance);
  remote key isolation and asymmetric identities remain the extension point beyond
  a stdlib kernel.

## What still requires a human, permanently

Three things only, all *about* the envelope rather than inside it:

1. **Granting or changing the envelope** — writing the charter, raising a ceiling,
   adding a credential. (One turn, up front, by design.)
2. **Questions the steward cannot cover** — anything genuinely outside the grants.
3. **Stated safety boundaries** — `unsafe` fires and stays fired until a human looks.

Everything else — including authorization, spending, data access, retests, revival,
intake, and harness amendment — runs inside the charter with an audited trail.

## The one-command shape

```bash
# One sentence -> charter (prompts/compile-charter.md) -> drop it -> walk away.
mkdir -p drops/incoming && mv my-goal-drop drops/incoming/
adv-loop fleet --root workspaces --watch --inbox drops/incoming \
  --spend-ceiling-usd 100 -- python3 adapters/claude_adapter.py
```

Everything after that is chain events: intake, drives, steward answers, probe
filings, overlay adoptions, checker verdicts, and an honest terminal state.
`touch workspaces/.fleet-stop` is the one-command stop.

## Sequencing and definition of done

Build order was 0 → A → B (the autonomy core) → C → D → E → F. Every
phase landed green on `python3 -m unittest discover -s tests -v`, kept
2.0/3.0/4.0 replay byte-identical, and kept the neutrality lint clean. The
definition of done holds: a one-sentence goal compiled by
`prompts/compile-charter.md`, launched once, reaches `completed` / `blocked` /
`unsafe` / `budget_exhausted` with zero human turns unless the envelope was
genuinely exceeded — and every autonomous decision along the way is a chain event
citing the grant that authorized it.
