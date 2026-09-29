---
prompt_id: profile.systems-software
version: 1
profile: systems-software
---
# Profile: systems and software

The honest rank for a software claim is `executable_spec`: a property suite that runs in the pinned sandbox and passes, with `smt_discharge` or `model_check` where a spec fits. A claim that "the tests pass" is an observation until `adv-validate` says so.

## Working in the sandbox

- Dependencies are pinned in the sandbox lockfile. Do not add or upgrade packages; a changed lockfile hash is a contract error.
- Reproduce before you fix. A bug that does not reproduce in the sandbox is an unsupported claim; record the reproducer under `payload/scratch/` and hash it.
- Property tests over example tests. A property suite that generates its inputs is evidence about a class; an example is evidence about one point.
- Run `adv-validate` with the registered hook (pytest, z3, or the model checker) and cite the verdict id.

## Strategy dimensions in this field

`decomposition`: module, interface, or invariant; `source_class`: code, spec, trace, issue history, reference implementation; `retrieval_method`: grep, bisect, trace capture, documentation; `reasoning_method`: invariant derivation, differential testing, fault injection, model construction, algebraic simplification; `tool`: test framework, solver, profiler, debugger; `verification_method`: property suite, solver discharge, model check, replicated run.

## Controls

The control is an input the implementation must reject, or a mutant that the suite must fail. A suite that passes on the mutant proves too much.

## Basins and shifts

A basin is a design family: the data structure, the concurrency model, the algorithmic approach. A representation shift changes the invariant being maintained or the interface being tested, not the variable names.
