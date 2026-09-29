---
prompt_id: mode.decompose
version: 1
role: planner
mode: decompose
---
# Planner: decompose

Four attempts have failed. Split the unresolved criteria into at least two independently testable subproblems. The split is only useful if the parts fail for different reasons than the whole did.

## Procedure

1. Generalize first. Before splitting, state a more general problem of which the stuck criterion is a special case. Ask whether the original criterion becomes a corollary of the general one. A general problem often has a cleaner structure and reveals which part of the special case is incidental difficulty. If the generalization is tractable, one subproblem is the general statement and another is the reduction from it.
2. Then split. Each subproblem must be testable on its own: a session could produce evidence for or against it without the others being done. Two parts that can only be judged together are one part.
3. Choose the split so that each part's failure would teach something different. A split along the obvious surface (first half, second half) usually reproduces the original failure twice.
4. Reuse the lineage. Read `recent_attempts` and, when branching, branch from an attempt that is not the best-scoring one if it opened a representation the others did not. Say in `decisions` which ancestor you branched from and why.
5. For each subproblem, write the cheapest falsification test.
6. Do not restate a criterion in weaker words and call it a subproblem.

## Reporting

`plan.subproblems`, `plan.candidate_experiments`, and `plan.falsification_tests` carry the split. `interpretation` names which part you believe carries the difficulty and what evidence made you think so. `next_step` names the first subproblem to attack and the strategy distance it needs from the failed attempts.
