# Profile: literature / biomedical research

An analyst answering a question from the primary literature, where the failure mode is
citing a claim that the sources do not actually support. The loop forces source-grounding
and disconfirmation.

| Concept | Instantiation |
|---|---|
| **criterion** | "The conclusion is supported by primary sources cited to the passage, and the single most disconfirming source has been read and addressed." |
| **seed / kill test** | Seed: "the association reported in the 2019 cohort is causal." Kill test: find the one confounder the cohort did not adjust for. |
| **control** | A known-false analogue claim (e.g. a retracted or debunked result phrased the same way). A method of reading that "confirms" the known-false claim is over-reading and dies under the `proves_too_much` lens. |
| **basin** | The line of evidence — "observational cohorts," "mechanistic in-vitro," "randomized trials." Two confirmed dead ends in "observational cohorts" close it; re-entry needs a different evidentiary basis. |
| **asset** | A retrieved paper with a stable identifier, an extracted table, a dataset, a systematic-review protocol — each with a locator (DOI, accession). |
| **survey move** | Register the source imports with locators: the reviews, the primary studies, the guideline documents. |
| **combine move** | "Cross the exposure definition in asset AS0004 with the outcome ascertainment in asset AS0009" — a synthesis not yet made. |
| **barrier / probe** | Barrier: "no study measured the exposure directly." Probe by `bound` (how large could the measurement error be?) or `shift_representation` (a validated proxy). |
| **wish** | "If the trial's individual-patient data are released, the subgroup route reopens" — rechecked on a date. |
| **replicate move** | Re-derive a key number from the primary source rather than the review that quoted it. |
| **lens** | `correctness` (does the passage say what is claimed?), `novelty` (is this already established?), `proves_too_much` (does the reading also endorse the known-false analogue?), `simplification` (is a single authoritative source enough?). |
| **lesson** | At a stuck streak: cause A "the sources conflict on the endpoint definition," test = compare the two definitions head to head; cause B "the effect is confounded," test = find a study that adjusted for it. |

The domain supplies what a "source," a "confounder," or a "primary study" is. The engine only
enforces that a fact is tied to a direct, criterion-linked citation and that the most
disconfirming source was actually confronted — the same structure it enforces everywhere.

## Honest evidence ranks (4.0)

Biology has no proof kernel, and this profile does not pretend otherwise. The ranks this
field can honestly reach:

- **`sourced_claim`** — a passage-cited claim with a locator, its most disconfirming
  source confronted. The floor for any literature conclusion.
- **`executable_spec`** — a mechanism reduced to a runnable check: a pinned ODE or
  rule-based simulation registered as a validator hook, whose accepted run (artifact
  hash and all) is the evidence. This is the honest ceiling for "the proposed mechanism
  is at least coherent."
- **`replicated_experiment`** — an empirical assertion independently re-derived: a
  second context, a different capture path, a re-extraction from the primary source
  rather than the review that quoted it.

`kernel_proof` does not appear here, ever. A criterion demanding it in this field should
be rejected at intake or narrowed by overlay review — the loop refuses to let a rank
claim more than the field's strongest checker can actually deliver. A "novel mechanism"
criterion should demand at least `executable_spec`; if only `sourced_claim` is possible,
the overlay recording that judgement is itself critic-reviewed.
