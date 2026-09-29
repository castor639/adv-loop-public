# Profile: security audit

An auditor establishing whether a system is exploitable, and proving each finding with a
reproducing artifact. The loop's gates match responsible-disclosure discipline.

| Concept | Instantiation |
|---|---|
| **criterion** | "Each in-scope vulnerability class is either shown exploitable with a reproducing proof-of-concept, or shown infeasible with a stated reason." |
| **seed / kill test** | Seed: "the upload endpoint trusts the client-supplied content type." Kill test: send a mismatched type in the sandbox and observe whether it is honored. |
| **control** | A known-clean build of the same system. A scanner or heuristic that flags the clean build is producing false positives and dies under the `proves_too_much` lens. |
| **basin** | The attack family — "authz bypass," "injection," "deserialization," "race." Two confirmed dead ends in "injection" close it; re-entry needs a genuinely different vector, not another payload. |
| **asset** | A working request harness, a decompiled component, a credential for the test account, a traffic capture — each with a locator. |
| **survey move** | Enumerate the attack surface and register it: endpoints, versions, dependencies with known advisories. |
| **combine move** | "Chain the SSRF from asset AS0002 with the metadata endpoint from asset AS0005" — a pairing not yet attempted. |
| **barrier / probe** | Barrier: "the WAF blocks the obvious payload shape." Probe by `dual` (what must the WAF *allow* to keep the app working?) or `shift_representation` (encode the payload differently). |
| **wish** | "If the vendor ships debug symbols for version X, the memory-corruption route reopens" — rechecked on a date, or fulfilled when they do. |
| **replicate move** | Re-run a confirmed exploit from a clean environment to prove it is not an artifact of the tester's box. |
| **lens** | `correctness` (does the PoC actually trigger?), `novelty` (is this the same finding twice?), `proves_too_much` (does the check also fire on the clean build?), `simplification` (is there a smaller reproducer?). |
| **lesson** | At a stuck streak: cause A "the endpoint is actually filtered upstream," test = hit it directly; cause B "the payload is right but the sink is unreachable," test = trace the data flow. |

Nothing here is security-specific to the *engine*: "a reproducing artifact," "a known-clean
control," "a named barrier and its dual" are research-method concepts. The auditor supplies the
domain; the loop supplies the discipline that stops a plausible-looking finding from being
called proven before a PoC exists.
