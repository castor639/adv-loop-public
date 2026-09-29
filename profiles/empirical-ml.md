# Profile: empirical ML research

A researcher trying to move a metric on a held-out task. The engine schedules exactly the
discipline a careful experimentalist already imposes on themselves.

| Concept | Instantiation |
|---|---|
| **criterion** | "Held-out metric improves by ≥ the pre-registered delta, with released seeds and configs." |
| **seed / kill test** | Seed: "the gain comes from the new sampler, not the extra compute." Kill test: a 10-minute ablation that removes the sampler and holds compute fixed. |
| **control** | A shuffled-label copy of the training data. A method that "improves" on shuffled labels is fitting noise and dies under the `proves_too_much` lens. |
| **basin** | The family of the approach — e.g. "reweight the loss," "change the architecture," "change the data mix." Two confirmed failures in "reweight the loss" close it; re-entry needs a real reframing, not a new coefficient. |
| **asset** | A trained checkpoint, an evaluation harness, a cleaned dataset, a profiling script — each with a locator (run id, artifact path). |
| **survey move** | Read the prior work and register what is reusable: a baseline implementation, a public benchmark split, a metric definition. |
| **combine move** | "Apply the augmentation from asset AS0003 to the curriculum schedule from asset AS0007" — a pairing no run has tried. |
| **barrier / probe** | Barrier: "the eval is compute-bound at 8 GPUs, so full sweeps are infeasible." Probe by `bound` (how large could the untested region's gain be?) or `shift_representation` (proxy metric that is cheap to sweep). |
| **wish** | "If the larger pretraining checkpoint is released, the low-data route reopens" — rechecked on a date. |
| **replicate move** | Re-run the winning configuration with fresh seeds to confirm the gain is not seed luck. |
| **lens** | `correctness` (is the eval leak-free?), `novelty` (is this just a known trick?), `proves_too_much` (does it beat the shuffled-label control too?), `simplification` (does a one-line baseline match it?). |
| **lesson** | At a stuck streak: cause A "the gain is a data leak," test = deduplicate train/test; cause B "the gain is variance," test = five-seed CI. Both enter the lessons ledger and the next replan must answer them. |

The one math-free reason this works: an "improvement" that also improves on shuffled labels,
or that a trivial baseline matches, is not an improvement — and the engine makes the researcher
confront that before it will let the criterion be called satisfied.
