# Profile: operations / debugging

An engineer finding the root cause of a production incident and proving it with a
reproduction. The loop enforces the discipline that separates a real root cause from a
plausible story.

| Concept | Instantiation |
|---|---|
| **criterion** | "The root cause is identified and demonstrated by a reproduction that toggles with the suspected cause." |
| **seed / kill test** | Seed: "the latency spike is the connection-pool exhausting under retries." Kill test: cap retries in a replica and watch the spike disappear (or not). |
| **control** | An unaffected replica or the last-known-good build. An explanation that also "predicts" the failure on the healthy replica is proving too much and dies under that lens. |
| **basin** | The layer being blamed — "the database," "the network," "the application," "the deploy." Two confirmed dead ends in "the network" close it; re-entry needs a genuinely new mechanism, not another packet capture. |
| **asset** | A reproduction script, a captured trace, a metrics dashboard snapshot, a diff of the bad deploy — each with a locator (dashboard URL, commit hash). |
| **survey move** | Register the telemetry sources: the logs, the traces, the metrics, the change history around the incident window. |
| **combine move** | "Correlate the GC-pause series from asset AS0001 with the request-queue depth from asset AS0006" — a pairing not yet examined. |
| **barrier / probe** | Barrier: "the failure only reproduces under production traffic shape." Probe by `bound` (what load is minimally sufficient?) or `shift_representation` (replay recorded traffic against a replica). |
| **wish** | "If verbose tracing is enabled on the next occurrence, the causal-order route reopens" — rechecked when the incident recurs. |
| **replicate move** | Reproduce the fix's effect a second time from a clean replica to confirm it was the cause, not a coincidence. |
| **lens** | `correctness` (does the reproduction actually toggle?), `novelty` (is this last week's incident again?), `proves_too_much` (does the theory predict failure on the healthy replica?), `simplification` (is there a one-line reproducer?). |
| **lesson** | At a stuck streak: cause A "it is the client retry storm," test = disable retries; cause B "it is the slow query under lock," test = explain-analyze under contention. Both feed the replan. |

"Root cause," "reproduction," "replica," "the deploy" are the engineer's terms; the engine
sees only a criterion demanding a reproduction that toggles with its cause, and a control it
must not also explain. The same gates that keep a math proof honest keep an incident
post-mortem honest.
