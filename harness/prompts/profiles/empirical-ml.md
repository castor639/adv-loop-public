---
prompt_id: profile.empirical-ml
version: 1
profile: empirical-ml
---
# Profile: empirical machine learning

The honest rank for an empirical claim is `replicated_experiment`: a clean-sandbox re-run reproduces the result within the stated tolerance, with controls. One run is an observation.

## Working in the sandbox

- Seeds, data splits, and package versions are pinned. Record the seed in every run's arguments and in the artifact name.
- Every result artifact carries its config alongside it under `payload/scratch/`; a number without its config is unsupported.
- Controls are part of the experiment, not an afterthought: a shuffled-label run, a baseline, an ablation. A method that beats the baseline on shuffled labels proves too much.
- Replication is a second run from a fresh process with the recorded config and a different seed; the verifier does this, and so should you before claiming `progress`.

## Strategy dimensions in this field

`decomposition`: data, model, objective, or evaluation; `source_class`: dataset, prior result, reference implementation, paper; `retrieval_method`: reproduction of a reference, literature, ablation grid; `reasoning_method`: hypothesis on mechanism, scaling argument, error analysis, ablation; `tool`: training harness, evaluation script, statistical test; `verification_method`: replication across seeds, held-out evaluation, control comparison.

## Basins and shifts

A basin is an approach family: the architecture class, the objective family, the data regime. A representation shift changes what the model sees or what it is asked to predict, not the hyperparameters.
