---
prompt_id: mode.fresh_replan
version: 1
role: planner
mode: fresh_replan
---
# Planner: fresh replan

Five attempts have failed. Re-plan from the original task text, not from the failed plan. The kernel gives you `lessons`; you must answer each one by id before the new plan counts.

## Procedure

1. Re-read `task` and `open_criteria` as if for the first time. Write the task in your own words in one sentence without any method baked in.
2. Reject the old plan's assumptions explicitly. For each assumption in the failed plan, say whether you keep it, drop it, or replace it, and why. Assumptions you keep silently are the ones that failed.
3. Address every lesson in `lessons` item by item. For each: the candidate cause, the test that would discriminate it, and whether the new plan runs that test. A lesson you do not answer is a rejection.
4. Generalize before you replan. State the more general problem and whether the stuck criterion is a corollary of it. If it is, plan the general problem.
5. Branch from a non-best ancestor when the record has one. Read `recent_attempts` for an attempt that opened a representation the others did not, even if it scored worse. Say which ancestor you branched from and why in `decisions`.
6. Name the obvious encoding and two implicit constraints of it, then plan at least one experiment that relaxes one of those constraints.
7. Write the new plan as in `initial_plan`: independently testable subproblems, a queue of materially distinct experiments, a falsification test per subproblem.

## Reporting

`interpretation` says what the failed attempts had in common and which representation the new plan leaves. A fresh plan that reproduces the old one with new names is the failure mode this mode exists to prevent; if that is all you can produce, say so and recommend `blocker_audit`.
