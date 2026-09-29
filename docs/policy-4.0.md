# Policy 4.0 design note

Three capabilities stacked on the existing proof spine, so the same loop can run
hundreds of unrelated projects at once, adapt its project-local contract when the
harness itself is the bottleneck, and treat "we discovered X" as incomplete until a
domain-appropriate checker accepted X. Nothing here rewrites the kernel: every addition
initializes only under a `4.0` pin, and `2.0`/`3.0` logs replay byte-identically
(pinned by the freeze tests in `tests/test_persistence.py`).

## Version plumbing

- `SUPPORTED_POLICY_VERSIONS = ("2.0", "3.0", "4.0")`; `POLICY_VERSION = "4.0"` is the
  default for new inits, with `adv-loop init --policy 3.0` to opt down. New `2.0`
  workspaces cannot be created (the version is frozen).
- `is_v4(state)` is `policy_version not in ("2.0", "3.0")` — the same
  inherit-forward idiom as `is_v3`, so 4.0 is a superset of 3.0 and a future 5.0
  inherits 4.0.
- Twelve state keys initialize only under the 4.0 pin: `overlays`, `overlay_revision`,
  `evidence_kind_registry`, `validator_hooks`, `sandboxes`, `stall_classes`,
  `role_instruction_overlays`, `toolchain_pins`, `process_faults`,
  `pending_overlay_review`, `pending_overlay_adoption`, `surgeon_marks` — plus an
  always-present `min_formalization_rank` on every 4.0 criterion.

## New event types (4.0-only; older logs fail closed on them)

- **`process_fault_recorded`** `{decision_hash, signature, source, detail}` — a
  non-terminal observation of one exhausted retry burst. `signature` is an opaque
  SHA-256 the submitter computed over its normalized error class (the driver collapses
  digit runs so counters do not split one condition into many); the kernel only counts
  repeats. Appended by the driver on adapter exhaustion (`AdapterError` is still
  raised; the task stays active) or via `adv-loop record-fault`.
- **`contract_overlay_adopted`** `{overlay_id "OV%04d", content_hash, from_revision,
  to_revision, schema_version, delta, justification_attempt_id, critic_attempt_id,
  verdict, fingerprint}` — engine-authored (request id `system:overlay:<head>`, the
  budget-gate precedent), appended in the same lock as the adopting review. The
  adoption gate also runs at every writer entry, so a crash between the two appends is
  finished before any other event can interleave; an `unsafe` stop may leave an
  adoption pending forever, which is correct — safety outranks amendment. Replay
  re-verifies the review binding, monotonic numbering, the content hash, and the
  tighten-only rules against the state the event landed on.

## The overlay delta language (allowlist; add or tighten only)

`{"schema_version": 1, "ops": [...]}` with exactly nine operations:

| op | tighten rule |
|---|---|
| `register_evidence_kind` | new kind only (redefinition could loosen); may require `checker` / `theory_base_hash` / `formalization_rank` and set a floor rank |
| `register_validator_hook` | new `hook_id` only; declares rank, argv command, timeout |
| `register_sandbox` | new id only; a pure record for the supervisor plane |
| `add_stall_class` | threshold 2..3 — may only demand diagnosis sooner |
| `add_role_instructions` | append-only contractual lines per role/mode |
| `add_criterion` | additive `C{n+1}` numbering; resets verification and report |
| `require_min_rank` | only when the criterion currently has no rank |
| `raise_criterion_rank` | strictly greater than the current rank |
| `pin_toolchain_hash` | new toolchain id only; checker records for that id must then match |

Never-below-creation is automatic: only set-on-null and strict-raise exist. Anything
outside the allowlist — skip a review, waive direct evidence, reuse a fingerprint,
reopen a terminal, lower a rank, declare human input unnecessary, touch another
workspace, patch kernel source — is rejected by name, because no such operation exists
to fold. The delta is validated at proposal (surgeon submission), at review, at
adoption, and again at replay (`IntegrityError` on tamper).

## Scheduler predicates for `surgeon/overlay_diagnosis`

`surgeon_demand(state)` is a pure function, priority fault > gap flag > rank backend,
iteration deterministic (fault insertion order, attempt order, criterion order):

1. **fault**: a signature's count reaches its threshold (`SURGEON_FAULT_THRESHOLD = 3`;
   an overlay stall class may tighten to 2). Marks are cyclic —
   `fault:<signature>#<k>` in `surgeon_marks` — so a diagnosed signature stays silent
   until a full threshold of new faults lands.
2. **gap flag**: an `attempt_review` carrying the structural tag `harness_gap: true`;
   mark `gap:<critic_attempt_id>`, one diagnosis per flag.
3. **rank backend**: an unsatisfied criterion demanding `smt_discharge` or above with
   no registered hook of sufficient rank; mark `backend:<criterion>:<rank>`, re-demanded
   only if the rank is raised again.

`expected_step` order (frozen for 4.0): awaiting_human → terminal stop → report-ready
`finalize` → **pending overlay review** → **surgeon demand** → verification-passed →
pending critique → pending triage → planner → escalation ladder → verifier →
researcher. Diagnosis outranks every adapter-issued step because a fault storm can
strike any of them; it never outranks `finalize`, which needs no adapter; the review
outranks new demands so one diagnosis is judged before another is owed. The predicates
never read failure streaks — ordinary scientific failure stays on the 3.0 ladder.

The surgeon records the raw condition, a classification (`harness_gap` /
`research_failure` / `human_dependency` — the review attacks this), a kill test, the
next experiment, and (gap only) the proposed delta with an attested fingerprint. It is
proof-inert, like ideation. A fresh-context `critic/overlay_review` then adopts,
rejects, or narrows (an exact-subset of the proposed ops); the effective delta is
re-validated at review time so adoption can never fail afterward. Reject completes the
obligation without adopting; the diagnosis remains on the record.

## Directive hashing × overlay adoption

The directive-id basis stays exactly `{policy_version, event_head, revision, role,
mode, target_attempt_id}`. Adoption is an append, the head moves, every in-flight
directive stales, and the refetched directive carries the amended contract — overlay
instruction text rides `instructions` outside the id basis (the encouragement
precedent), fully deterministic from replayed state.

## What still requires a human

`ask-human` on 4.0 must classify what the human is for: `external_dependency`,
`authorization`, `private_data`, `safety_boundary`, or `other_human_judgment`.
`harness_gap` is refused by name with the surgeon named as the legal next step, and
asking is refused outright while any diagnosis or review is owed. The blocked gate
refuses identically. `mark_unsafe` is never gated — a safety or authorization boundary
always stops the loop. Approving `intake --apply`, answering classified questions,
retesting blocked premises, and granting credentials remain operator work.

## Formalization ranks

`sourced_claim < replicated_experiment < executable_spec < smt_discharge <
model_check < kernel_proof` (frozen). A criterion's `min_formalization_rank` is set at
init (structured `--criteria-file` items or `create()` dicts), by `add-criterion`, or
raised (only) by overlay. Gates, all same-item: a satisfied rank-gated criterion needs
one direct supporting evidence item at or above the rank; `criteria_evidence_ready`
re-checks it (so a raised rank reopens the work without touching status); every cited
verification item must meet the rank too. Combined with the existing independence
rules (context, provenance key, fingerprint disjointness) and the fingerprint ==
checker-artifact-hash rule, a `kernel_proof` verification is structurally a second
accepted checker run.

Evidence fields (4.0-only, optional): `formalization_rank`, `checker {checker_id,
checker_version, accepted, artifact_hash, log_hash?, toolchain_hash?}`,
`theory_base_hash`. Rules: a rank at/above `smt_discharge` requires `accepted: true`;
a non-accepting checker record forbids any rank (the failed check is still recordable
as an unranked observation); the fingerprint must equal the artifact hash; registered
evidence kinds enforce their required fields and floor; pinned toolchains require a
matching `toolchain_hash`.

## Supervisor plane contracts

- **Intake proposal** (heuristic or adapter, validated identically): candidates with
  stable filesystem-safe ids (`slug + content-hash prefix`), non-empty criteria, payload
  paths covering the whole manifest, structural suggestions only. Envelope
  `adv-loop-intake/1` carries the manifest, the heuristic proposal, and utf-8 head
  samples; the kernel never interprets content. Apply is per-candidate isolated, with
  post-copy hash verification and no rollback (a created workspace is valid).
- **Checker verdict** (`adv-loop-validate/1`): `{accepted, checker_id,
  checker_version, artifact_hash, log_hash, toolchain_hash?, details?}` — schema-checked;
  `accepted: false` is exit 0 (a successful observation); contract violations are
  exit 1. The printed `evidence_hint` uses the engine's exact evidence field names.
  Overlay-registered hooks (audited, command included) win over machine-local
  `loop-config.json` entries.
- **loop-config.json 4.0 keys** (all optional, ignored by older readers): `adapter`,
  `adapter_timeout_seconds`, `adapter_retries` (per-workspace drive command — wins over
  the fleet CLI fallback), `profile`, `formalization`, `sandbox {kind, root, command,
  setup, setup_timeout_seconds, lockfile_hashes}`, `validators {name: {command,
  timeout_seconds, in_sandbox, rank}}`.
- **Isolation layers** per workspace: event log + lock + journal + lease (2.0),
  projections (2.0), `payload/` + sandbox + config (4.0, supervisor), overlay contract
  (4.0, kernel). The kernel source is shared and immutable from tasks. Concurrent fleet
  passes over the same root serialize per workspace on `flock`; the loser's directives
  go stale — worst case duplicated adapter compute, never corruption.

## Honest limits (also in architecture.md)

The kernel cannot see whether a checker was actually a proof kernel or a stub; it
trusts the attested artifact exactly as it trusts evidence fingerprints (signed checker
attestations are the extension point). Path containment is a contract against accidents
and hostile archives, not an OS jail. Hundreds of projects is scheduling plus
isolation, not magic throughput — bound `--max-parallel`. And a field without a proof
kernel never gets one by declaration: profiles state each field's honest ceiling, and
overlays can only tighten.
