# Literature Review: Radical thinkers and unconventional problem solving, applied to ADV Loop

Compiled 2026-09-07. Three parallel searches: the Firecrawl paper index (PubMed, PMC, bioRxiv, medRxiv, arXiv), general web search, and direct scrapes of essays and blogs. Roughly ninety sources kept. Ids of the form `arxiv:`, `pmid:`, `pmcid:` are index ids and were checked with `firecrawl research inspect-paper`; see "Id verification" at the end for the ones that did not resolve.

Labels used per entry: **[journal]** means a peer-reviewed venue or a PMC-indexed copy exists; **[preprint]** means arXiv only, venue unverified; **[essay]** means a book, talk, or blog post. arXiv ids beginning `26` are months old at most and should be treated as unvetted.

## Abstract

The question was whether the pattern behind "radical thinkers", people who take approaches far outside the ordinary and solve problems nobody else could, can be built into an automated research loop. The literature answers in three layers. The science-of-science layer says breakthroughs are not random novelty: they have a conventional core plus one genuinely distant element, they come disproportionately from outsiders and small teams drawing on older and less popular ideas, and they are systematically under-rewarded by reviewers and by expected-value ranking. The cognitive layer says the thing that unblocks a stuck solver is a change of *representation*, not a change of method: relaxing a self-imposed constraint, decomposing a chunk, renaming entities in function-free terms. Fixation is an attention effect that persists even when the solver sincerely believes it is searching for alternatives, and language models show the same Einstellung effect. The agent-systems layer says LLM loops converge on the median idea because of a data-level typicality bias, that temperature does not fix it, that fresh context does remove intra-context fixation, and that novelty must be measured at the strategy level because surface diversity collapses to recurring patterns. The strongest agent-side results reward behavioral novelty or Bayesian surprise instead of the objective, branch from non-best ancestors, and score a lineage by its descendants.

ADV Loop already implements a surprising amount of this structurally: strategy fingerprints that can never recur, fresh minimal context for ideation, a critic in a different context, basins with representation-shift re-entry, a cyclic escalation ladder, a lessons ledger, and a proves-too-much control. The gaps are specific. The engine cannot check that a declared representation shift is real. Nothing models the *expected* set of approaches so seeds are novel only relative to history. Triage debate is symmetric. The `combine` move enforces pair novelty but not distance. Candidate selection is greedy on the candidate's own Elo. There is no protected budget for low-scoring branches. The evidence came from one long proof workspace: 39 attempts, failure streak 7, every escalation rung fired, and five critic-verified closure theorems that all established the same limit of a single method family. The loop exhausted a method class without ever leaving it. That is the failure the whole literature describes.

## Key Papers

### R1. Composition: conventional core plus one distant element

- **Uzzi, Mukherjee, Stringer, Jones (2013). Atypical combinations and scientific impact. Science.** `pmid:24159044` [journal]. Across 17.9M papers, hits have high median conventionality in their citation pairs *and* a tail of atypical pairs; that combination roughly doubles hit probability. Pure novelty underperforms. Rule: reward a proposal whose asset set is mostly conventional for the field with at least one declared-distant source; penalize all-conventional and all-exotic alike.
- **Shi, Evans (2023). Surprising combinations of research contents and contexts are related to impact and emerge with scientific outsiders from distant disciplines. Nature Communications.** `pmcid:PMC10039062` [journal]. Breakthroughs are best identified as surprise relative to a model of expected advance, and surprising content-context combinations come disproportionately from outsiders. Rule: model expectation explicitly; simulate an outsider by forcing a distant field's method vocabulary onto the same problem.
- **Wu, Wang, Evans (2019). Large teams develop and small teams disrupt science and technology. Nature.** `pmid:30760923`, preprint `arxiv:1709.02445` [journal]. Small teams draw on older, less popular ideas and disrupt; large teams draw on recent popular ideas and develop. Rule: exploration runs use one agent or a small ensemble biased toward older and less-cited prior work; large-ensemble consensus is for verification.
- **Lin, Frey, Wu (2021). New directions in science emerge from disconnection and discord.** `arxiv:2103.03398` [preprint]. Atypical papers are about twice as likely to be disruptive, but the disruption registers slowly and draws discordant citation contexts. Rule: never kill an atypical branch on a short-horizon agreement proxy; treat reviewer disagreement as signal.
- **Park, Leahey, Funk (2023). Papers and patents are becoming less disruptive over time. Nature.** `pmid:36600070`, preprint `arxiv:2106.11184` [journal]. The decline tracks narrowing use of prior literature: fewer distinct references, more self-citation. Rule: track a consolidate-vs-disrupt ratio per run; if outputs keep extending their own recent ancestors, force a source-diversity injection.
- **Innovation by displacement** `arxiv:2512.03723` and **Triadic novelty** `arxiv:2506.17851` [preprint]. Breakthroughs often substitute for and retire an existing idea rather than adding to it. Rule: a "replace, don't add" move that names the assumption it retires.
- **Longevity of innovation** `arxiv:2606.29777` and **Advantages of interdisciplinarity** `arxiv:1712.07910` [preprint]. Distance carried inside one agent behaves differently from distance assembled across agents; Uzzi's effect requires integration, not co-presence.

### R2. Ordering: attempt before retrieval

- **Kapur. Productive failure in learning math. Cognitive Science.** `pmid:24628487`; **Productive failure as an instructional approach.** `pmid:31089856`; **Sinha, Kapur mechanisms review** `pmid:33180211`, `pmcid:PMC6435728` [journal]. Generating mostly wrong solutions *before* canonical instruction produces better understanding and transfer than instruction-first, despite worse in-activity performance. Rule: force an unassisted first attempt on a fresh criterion before the loop is allowed to survey; retrieval-first installs the conventional representation.

### R3. Failure response: change the representation, not the method

- **Knoblich, Ohlsson, Haider, Rhenius (1999). Constraint relaxation and chunk decomposition in insight problem solving. JEP:LMC.** Cited and tested in `pmid:14516232` (testing two cognitive theories of insight), `pmid:18834964` (generalization to non-insight problems), `pmid:25151244` (operationalized procedure), `pmid:24059858` (training study) [journal]. Insight comes from relaxing a self-imposed constraint and decomposing a tight chunk, not from harder search in the same space. Rule: after N failures, enumerate the implicit constraints in the current encoding, relax one, and split one "atomic" entity.
- **McCaffrey (2012). Innovation relies on the obscure: overcoming functional fixedness. Psychological Science.** `pmid:22318998` [journal]. The generic-parts technique, recursively renaming every part in function-free language, raised solution rates by about 67% over controls. Rule: rewrite every entity in the problem statement name-stripped before re-attempting.
- **Bilalic, McLeod, Gobet (2008). Why good thoughts block better ones: the Einstellung effect. Cognition.** `pmid:18565505`; **Inflexibility of experts, reality or myth?** `pmid:17418112`; eye-movement follow-ups `pmcid:PMC3790829`, `pmcid:PMC4079068` [journal]. Once a familiar solution activates, attention itself keeps returning to it while the solver sincerely reports searching for alternatives. Rule: quarantine the first plausible solution; generate the next K candidates from a context where its key entities are masked.
- **Luchins (1942). Mechanization in problem solving.** [essay, monograph]. Replicated in `pmid:21851153` (interactivity defuses mental set) and `arxiv:2307.06673` (Einstellung in programming). Rule: if the last M solutions used one procedure schema, block that schema next.
- **Naeini et al. (2023). LLMs are fixated by red herrings.** `arxiv:2306.11167` [preprint]. Language models show the classic Einstellung effect. This is the mechanistic reason fresh contexts and forced re-representation should help agents.
- **Wiley (1998). Expertise as mental set. Memory and Cognition.** `pmid:9701964` [journal]. Domain experts were worse than novices when the misleading candidate came from their domain. Rule: down-weight in-domain retrieval when the problem sits in the loop's densest knowledge region.
- **Sio, Ormerod (2009). Does incubation enhance problem solving? Psychological Bulletin.** `pmid:19210055`; Baird et al. 2012 `pmid:22941876`; Sio, Monaghan, Ormerod 2018 `pmid:30113673` [journal]. Low-load incubation beats both rest and high-load tasks; sleep adds nothing beyond wake incubation. Rule: fill the incubation slot with a different undemanding task, not a wait.
- **Fixation and analogy corpus**: EEG of functional fixedness `pmid:29530800`; analogical transfer `pmid:11827089`; MUSE functional concept graphs `arxiv:2509.05072`; design-by-analogy with generative AI `arxiv:2602.09423`. Generative tools *increase* design fixation unless analogical structure is imposed. Rule: an explicit function-abstraction layer (problem, abstract function, distant instantiations) rather than hoping the model samples distant ideas.
- **Metacontrol of divergent vs convergent thinking**: `pmid:24749966`, `pmid:31972282`, `pmcid:PMC10144848` [journal]. The two modes dissociate and use different control states. Rule: a two-state controller (broad-flexible, narrow-persistent) with scheduled switches, not one temperature knob.

### R4. Anti-expectation objective: reward what an expert would not infer

- **Sourati, Evans (2023). Accelerating science with human-aware artificial intelligence. Nature Human Behaviour.** `pmid:37443269`, preprints `arxiv:2306.01495`, `arxiv:2104.05188`, `arxiv:2207.00902` [journal]. Modeling what human experts are cognitively likely to infer and then avoiding those inferences improves discovery prediction by up to 400% and yields complementary "alien" hypotheses. Rule: compute the set an ordinary expert would generate; reward candidates outside it. The single most directly implementable radical-thinker objective.
- **Lehman, Stanley (2011). Abandoning objectives: evolution through the search for novelty alone. Evolutionary Computation.** `pmid:20868264` [journal]. Objective functions are deceptive; rewarding behavioral novelty alone reaches the objective where objective-following fails. The founding citation.
- **Yannakakis, Liapis (2017). Surprise search for evolutionary divergence.** `arxiv:1706.02556` [preprint]. Rewards deviation from a predictive model of expected behavior. Rule: "what is the obvious next move? now don't do it."
- **Agarwal et al. (2025). AutoDiscovery: open-ended scientific discovery via Bayesian surprise.** `arxiv:2507.00310` [preprint]. Expected posterior shift beats diversity heuristics and interestingness proxies as an exploration criterion.
- **Zhang, Lehman, Stanley, Clune (2023). OMNI: open-endedness via models of human notions of interestingness.** `arxiv:2306.01711`; **OMNI-EPIC** `arxiv:2405.15568` [preprint]. A foundation model as a model of interestingness picks what to work on next; the extension lets the agent invent the task.
- **Conti et al. (2017). NS-ES / NSR-ES.** `arxiv:1712.06560`; **Novelty search by action-sequence edit distance** `arxiv:1902.03142`; **Objectives are all you need** `arxiv:2311.02283` (counterpoint: many sub-objectives escape deception without explicit diversity); **Woolley, Stanley (2012) stepping stones** `arxiv:1207.6682` [preprint].
- **Du et al. (2023). ELLM: guiding pretraining in RL with LLMs.** `arxiv:2302.06692` [preprint]. Generic novelty bonuses reward irrelevant novelty; the LLM filters novelty for relevance.
- **Intrinsic-reward family for LLM reasoning**: Rewarding the Rare `arxiv:2601.08763` (reward correct-but-rare solutions at the set level); MERCI count-based bonus `arxiv:2510.16614`; CDE curiosity `arxiv:2509.09675`; CD-RLHF `arxiv:2501.11463`; intrinsic motivation `arxiv:2505.17621`; SuS strategy-aware surprise `arxiv:2601.10349` [preprint].

### R5. Portfolio: protected budget, upper-tail evaluation

- **Foster, Rzhetsky, Evans (2015). Tradition and innovation in scientists' research strategies. American Sociological Review.** `arxiv:1302.6906` [journal, preprint id]. Innovative strategies pay more per paper but are rarer because expected payoff net of failure risk favors tradition. Rule: a fixed fraction of compute goes to innovative branches by policy, since expected-value ranking starves them.
- **Azoulay, Graff Zivin, Manso (2011). Incentives and creativity: evidence from the academic life sciences. RAND Journal of Economics.** [journal, not in index; NBER w15466]. HHMI investigators with long horizons and failure tolerance produce more top-percentile papers and more flops. Rule: judge exploratory branches on best-of-N, never the mean; give each a minimum protected budget.
- **Ceci et al. (2022). Is novel research worth doing? Evidence from peer review at 49 journals. PNAS.** `pmcid:PMC9704701` [journal]. Novelty predicts citations but reviewers score novel manuscripts lower. Rule: log the generator's novelty score and the critic's acceptability score as separate fields; never multiply them.
- **Azoulay, Fons-Rosen, Graff Zivin. Does science advance one funeral at a time? American Economic Review.** `pmcid:PMC6814193` [journal]. After an eminent scientist's death, outsider output in the subfield rises and is disproportionately cited. Rule: periodically ablate the dominant source in a topic's context and re-run.
- **Liu et al. (2018, 2021). Hot streaks.** `pmid:29995850`, `arxiv:2103.01256`, `pmcid:PMC8438033` [journal]. Streak onset follows exploration then exploitation; neither alone predicts it. Rule: explicit explore-then-commit phases.
- **Ke et al. Sleeping beauties in science.** `arxiv:1505.06454` [journal, preprint id]. Delayed recognition is continuous, not exceptional. Rule: cold storage for rejected-but-coherent hypotheses, re-evaluated when the knowledge base changes.
- **Risk aversion modeling**: `arxiv:2306.13816` / `pmcid:PMC11326573`, `arxiv:2312.06479`. Effort incentives mechanically discourage risk; longer horizons increase risk-taking. Rule: reward radical-move usage independently of outcome.
- **Simonton. Equal-odds rule.** `pmid:3830907`; BVSR `pmid:20416854`, `pmid:39270513`; Gabora critique `arxiv:1409.2210` [journal]. Quality is a probabilistic function of quantity. Rule: optimize for volume of *distinct* attempts; low hit rate is expected.

### R6. Dissent: preset stance, asymmetric debaters, a funded rival

- **Feyerabend (1975). Against Method.** [essay]. Counterinduction: develop hypotheses inconsistent with well-confirmed theory, because a fact's refuting force is visible only from inside an alternative. Rule: always carry at least two incompatible working hypotheses; construct one that contradicts the best-supported claim and fund it independently of its score.
- **De Dreu, West (2001). Minority dissent and team innovation. Journal of Applied Psychology.** `pmid:11768061`; hidden profiles `pmid:17144766`; group cognitive complexity `pmid:21507019`, `pmcid:PMC5368259` [journal]. Dissent raises divergent thought and decision quality even when the dissenter is wrong, but only if the objection is engaged. Rule: a persistent dissenter whose objection must be answered on the record, scored on whether it changed the reasoning.
- **Sunstein (2003). Why Societies Need Dissent.** [essay]. Cascades amplify shared over unique information. Rule: no agent sees peer outputs before committing its own.
- **Fang et al. (2024). Counterfactual debating with preset stances (CFMAD).** `arxiv:2406.11514` [preprint]. Self-correction and diverse sampling both over-trust the initial answer; preset stances override that bias. Rule: the critic is assigned a stance in advance, not asked to "be critical".
- **Liu et al. (2026). Breaking the Martingale Curse.** `arxiv:2603.06801` [preprint, unvetted]. Standard debate cannot beat majority voting because correlated errors converge to erroneous consensus. Rule: debaters must be structurally asymmetric (different priors, models, or roles).
- **Chen et al. (2026). When and why does multi-agent debate fail?** `arxiv:2510.20963`; **Not all flips are conformity** `arxiv:2606.00820` [preprint]. Consensus-seeking debate is often net-negative; most convergence is conformity, not persuasion.
- **When persuasion overrides truth (CW-POR)** `arxiv:2504.00374` [preprint] and **When collaboration fails: persuasion-driven adversarial influence in multi-agent LLM debate** `pmcid:PMC13061921` [journal]. A forceful contrarian can steer the judge wrong. This is the risk register for any devil's-advocate design.
- **D3: Debate, Deliberate, Decide** `arxiv:2410.04663` [preprint]. k parallel advocates per candidate; diversity of argument, not only of answer.
- **Chen, Evans et al. The social abduction of science.** `arxiv:2111.13251` [preprint]. Anomalies get resolved through *sustained* dialogue between insiders holding the anomaly and outsiders holding foreign mechanisms.
- **Execute-Distill-Verify** `arxiv:2606.24428` [preprint, unvetted]. The agent that acted must not decide whether it succeeded.

### R7. Escalation: state the contradiction, then generalize

- **Polya (1945). How to Solve It.** [essay]. Inventor's paradox: the more general plan is often easier because it has more structure to grip. Rule: "generalize the problem" is a first-class attempt, not a detour.
- **McLarty. The rising sea: Grothendieck on simplicity and generality.** https://www.landsburg.com/grothendieck/mclarty1.pdf [essay]. Raise generality until the problem dissolves, versus hammer-and-chisel. Rule: a budgeted branch whose only allowed move is weakening assumptions, evaluated on whether the original problem becomes trivial.
- **TRIZ**: contradiction matrix solving a real PCR problem `pmcid:PMC4720617` [journal]; TRIZ in scientific discovery `arxiv:1608.00536`; `pmid:16210175`. State the block as "improving A worsens B", then apply separation principles. Rule: a finite, checkable operator set instead of "be more creative".
- **Constraint-model reformulation with LLM agents** `arxiv:2607.28268`, `arxiv:2608.08127` [preprint, unvetted]. Same problem, different representation, as the search space, with empirical correctness checks. The closest agent-side match to representational change.
- **Hamming (1986). You and Your Research.** https://www.cs.virginia.edu/~robins/YouAndYourResearch.html [essay]. Study *why* a problem could not be solved; the fault becomes the asset by a change of viewpoint. Rule: on failure, a mandatory subtask characterizes the obstruction instead of retrying.
- **Dyson (2009). Birds and Frogs. Notices of the AMS.** https://www.ams.org/notices/200902/rtx090200212p.pdf [essay; 403 for bots]. Rule: typed generators at different altitudes with forced handoff.
- **Feynman (1965). Nobel lecture.** https://www.nobelprize.org/prizes/physics/1965/feynman/lecture/ [essay]. A non-standard formalism answered questions the standard one could not. Rule: restate the problem in two formalisms the field does not use before executing.

### R8. Lineage over score

- **Zhang, Hu, Lu, Lange, Clune (2025). Darwin Godel Machine.** `arxiv:2505.22954` [preprint]. Archive of all past agents, parents sampled from non-best ancestors; beats non-open-ended baselines. Rule: stepping stones matter more than the current best.
- **Huxley-Godel Machine** `arxiv:2510.21614` [preprint]. Benchmark score is a bad proxy for self-improvement potential; score a node by its descendant clade. Rule: rank a candidate by what its descendants achieved, not its own score.
- **Antoniades et al. (2026). Heuresis: search strategies for autonomous AI research agents across quality, diversity and novelty.** `arxiv:2606.25198` [preprint, unvetted]. Six strategies (greedy, MAP-Elites, Go-Explore, evolutionary, novelty-based) benchmarked head to head inside one research-agent harness. The most directly on-topic paper found.
- **Luo et al. (2026). SeaEvo: strategy space evolution.** `arxiv:2604.24372` [preprint, unvetted]. Program-plus-fitness archives cannot tell syntactically different implementations of the same idea apart; keep state over strategic directions instead. The closest thing to ADV Loop's strategy fingerprint, and a warning about what it cannot see.
- **MEDS: memory-enhanced dynamic reward shaping** `arxiv:2604.11297` [preprint, unvetted]. Penalize recurrence of past failure patterns across rollouts. An explicit tabu list over failure modes.
- **Foundations**: ELM `arxiv:2206.08896`; FunSearch `pmcid:PMC10794145` [journal, Nature]; AlphaEvolve at scale `arxiv:2511.02864`; ShinkaEvolve `arxiv:2509.19349`; CodeEvolve island model `arxiv:2510.14150`; Diverse Prompts MAP-Elites `arxiv:2504.14367`; DEI heterogeneous mutation operators `arxiv:2605.27130`; Promptbreeder `arxiv:2309.16797`; Voyager `arxiv:2305.16291`; AI Scientist `arxiv:2408.06292`, v2 `arxiv:2504.08066`; Godel Agent `arxiv:2410.04444`; ADAS `arxiv:2408.08435`; CORAL `arxiv:2604.01658`; Red Queen Godel Machine `arxiv:2606.26294`; FERMAT `arxiv:2511.14778`; Tree of Thoughts `arxiv:2305.10601`; Novelty-based ToT `arxiv:2605.06040`; ToT as heuristic search `arxiv:2605.28566`; SE-Agent `arxiv:2508.02085`.

### Diagnostic: why LLM loops converge on the median

- **Verbalized Sampling** `arxiv:2510.01171` [preprint]. Mode collapse is a data-level typicality bias in preference annotation. Asking for a distribution over answers with probabilities restores 1.6 to 2.1x diversity. Highest-leverage prompt-level fix.
- **Beyond the Hivemind** `arxiv:2608.02618` [preprint, unvetted]. Inter-response similarity stays near 0.85 even at high temperature. Persona must be self-selected and idiosyncratic before generation.
- **Barriers to diversity in LLM-generated ideas** `arxiv:2602.20408` [preprint]. Fixation is intra-context; clearing history removes it. Direct support for ADV Loop's minimal-context ideation.
- **R-Diverse: diversity illusion** `arxiv:2602.13103` [preprint]. Surface-diverse outputs collapse to recurring underlying patterns. Measure diversity at the strategy level.
- **Controllable memory usage** `arxiv:2601.05107`; **AgenticSTS** `arxiv:2607.02255`; **Self-compacting agents** `arxiv:2606.23525`; **ThinkReset** `arxiv:2607.28642`; **APEX exploration collapse** `arxiv:2605.21240`; **Karimi et al., evolutionary neighborhood bias and the coherence ceiling** `arxiv:2603.21321` [preprint, unvetted]. Together: the agent's own memory is the trap, scalar fitness cannot reward a multi-step conceptual leap that is temporarily worse, and typed retrieval into a fresh message is a working alternative to appended transcripts.
- **Probability concentration / branching factor** `arxiv:2506.17871`; **SimpleStrat** `arxiv:2410.09038`; **Intent Factored Generation** `arxiv:2506.09659`; **Growing a Tail** `arxiv:2411.02989`; **Creativity has left the chat** `arxiv:2406.05587`; **BACo base-aligned collaboration** `arxiv:2511.05650`; **CreativityNeuro** `arxiv:2607.01433` [preprint]. Sample the approach before the solution; partition the space and sample per stratum; route ideation to a base model.
- **Bao, Evans (2026). Contemporary AI lacks the imagination to diverge or negate in science.** `arxiv:2606.08251` [preprint, unvetted]. 6,749 scientists rating LLM ideas derived from their own papers: models collapse into a narrow band and specifically fail to diverge and to negate.
- **Si, Yang, Hashimoto (2024). Can LLMs generate novel research ideas?** `arxiv:2409.04109`; **The ideation-execution gap** `arxiv:2506.20803` [preprint]. LLM ideas were judged more novel than expert ideas, but the advantage did not survive execution, and self-evaluation did not track human judgment.
- **Benchmarks**: MUTATE divergent agents `arxiv:2605.28465`; MacGyver `arxiv:2311.09682`; BRAINTEASER `arxiv:2310.05057`; CresOWLve `arxiv:2604.03374`; LiveIdeaBench `arxiv:2412.17596`; Divergent creativity in humans and LLMs `arxiv:2405.13012`.

### Practitioner essays

- Paul Graham, How to Do Great Work https://paulgraham.com/greatwork.html and How to Think for Yourself https://paulgraham.com/think.html. Keep an anomaly log; seed generation from unresolved anomalies; diversify exposure deliberately.
- Terence Tao, There's more to mathematics than rigour and proofs https://terrytao.wordpress.com/career-advice/theres-more-to-mathematics-than-rigour-and-proofs/. Generate in heuristic mode with rigour off, verify separately; the verifier never participates in generation.
- Nielsen, Qiu, A Vision of Metascience https://scienceplusplus.org/metascience/ and Nielsen, Principles of Effective Research https://aykuterdem.github.io/resources/principles-of-effective-research.pdf. Reserve compute for bottom-quartile-expected-acceptance ideas; a fixed problem-creation budget per cycle.
- Nabeel Qureshi, How to Understand Things https://nabeelqu.co/understanding and Notes on Puzzles https://nabeelqu.substack.com/p/notes-on-puzzles. A confusion register; an unused constraint blocks completion.
- Ed Boyden, How to Think https://www.media.mit.edu/publications/how-to-think/. Every cycle emits at least one synthesized idea into a persistent store.
- Yudkowsky, Hero Licensing https://www.lesswrong.com/posts/dhj9dhiwhq3DX6W8z/hero-licensing. "Someone would already have done it" must be backed by a citation before it prunes a branch.
- Matuschak, Nielsen, Tools for thought https://numinous.productions/ttft/. Instrument-building cycles with no direct hypothesis payoff count as progress.
- Gwern, LLM Daydreaming https://gwern.net/ai-daydreaming. A background recombination loop over random distant pairs, accepting a very low hit rate.
- janus, Mysteries of mode collapse https://www.lesswrong.com/posts/t9svvNPNmFf5Qa3TA/mysteries-of-mode-collapse. Never trust naive n-sampling for diversity.
- Sakana AI, The AI Scientist https://sakana.ai/ai-scientist/ and first publication post-mortem https://sakana.ai/ai-scientist-first-publication/. Competent execution, weak problem selection.
- METR developer productivity RCT https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/. Self-reported progress is miscalibrated; gate on external metrics.
- Epoch AI on R&D automation https://epoch.ai/gradient-updates/most-ai-value-will-come-from-broad-automation-not-from-r-d. Model the non-cognitive bottleneck per problem.

## Themes and Consensus

Eight operational rules the sources converge on:

| # | Rule | Load-bearing sources |
|---|---|---|
| R1 | Conventional core plus one genuinely distant element; both extremes lose. | Uzzi 2013; Shi and Evans 2023; Wu, Wang, Evans 2019 |
| R2 | Attempt before retrieval; retrieval-first installs the conventional representation. | Kapur productive failure |
| R3 | On failure, change the representation, not the method: relax a named constraint, decompose a chunk, rename entities function-free. | Knoblich and Ohlsson; McCaffrey; Bilalic; Naeini 2023 |
| R4 | Reward what an ordinary expert would not infer; Bayesian surprise is the computable form. | Sourati and Evans 2023; Lehman and Stanley 2011; AutoDiscovery |
| R5 | A fixed protected budget for low-expected-acceptance branches, judged on the upper tail over a long horizon. | Foster, Rzhetsky, Evans 2015; Azoulay HHMI; Ceci 2022 |
| R6 | Always carry a contradicting hypothesis and an independent dissenter with a preset stance; symmetric debaters converge to correlated error. | Feyerabend; De Dreu and West; CFMAD; Martingale Curse |
| R7 | State the contradiction as "improving A worsens B", then generalize or raise abstraction until the obstruction dissolves. | Polya; Grothendieck via McLarty; TRIZ |
| R8 | Branch from non-best ancestors; score a lineage by its descendants. | Darwin Godel Machine; Huxley-Godel Machine; Heuresis; SeaEvo |

## Gap map against ADV Loop 5.0

Read against `protocols/loop.md` sections 4, 10, 13, 14 and `src/adv_loop/policy.py`.

| Rule | Already structural in ADV Loop | Gap |
|---|---|---|
| R3 representation change | `basin` per seed and experiment; two failures close a basin; re-entry needs a `representation_shift` folded into the fingerprint; `barrier_probe` has a `shift_representation` pattern. | `representation_shift` is free text. The engine cannot check it is a real shift. There is no constraint-enumeration or entity-renaming artifact. That proof workspace shows the ladder can cycle without leaving a basin. |
| R4 anti-expectation | Ideation from a fresh minimal context; seed claims fingerprinted forever. | Nothing models the expected set. Seeds are novel relative to history, not relative to what a competent expert tries first. |
| R6 dissent | Triage pairwise debate with Elo; lens rotation including `novelty` and `proves_too_much` against a control; `contradiction_search` rung. | Debate is symmetric: same provenance, no preset stance. `contradiction_search` asks to challenge an assumption, not to construct and fund a rival hypothesis. Novelty and acceptability are not recorded separately. |
| R1 composition | `combine` requires an asset pair never combined before. | Pair novelty only, no distance. Assets carry no field label, so "one in-field, one distant" cannot be enforced. |
| R7 generalize | `barrier_probe` patterns `bound`, `dual`, `shift_representation`. | No `generalize` pattern whose success test is that the original criterion becomes a corollary. No contradiction statement in TRIZ form. |
| R8 lineage | Candidates carry `parents`; `evolve` ideation mutates the top-K. | Selection is greedy on the candidate's own Elo. Clade productivity is computable from `parents` in replay but unused. Dead candidates are never resurrected. `wishes` and `retest` partly cover sleeping beauties. |
| R5 portfolio | The ladder forces ideation at streak 2. | No reserved researcher slot for low-Elo-but-alive candidates. |
| R2 ordering | `survey` sits at rung 6, deliberately late. | `initial_plan` has no unassisted-first rule. |
| Anomaly seeding | Contradictions stay open in state; lessons ledger surfaces to every planner. | Ideation receives no history at all. The literature's resolution: typed retrieval of open contradictions and lessons only, never attempts, keeping `context_scope: minimal`. |

## Open Questions and Debates

- **Is disruption measurable?** The CD index is confounded by zero-reference works, citation inflation, and truncation (Holst `arxiv:2402.14583`; Petersen `arxiv:2306.01949`, `arxiv:2406.15311`; Macher `arxiv:2306.10774`; Park and Funk reply `arxiv:2503.00184`; inventory of 105 studies `arxiv:2602.05140`). The direction replicates across text, product, and legal measures. Implication: never credit a breakthrough on one novelty metric; require two structurally different ones.
- **Does novelty survive execution?** Si et al. found LLM ideas more novel than experts' but the advantage vanished when 43 experts executed them (`arxiv:2506.20803`). Novelty scoring without an execution gate is not evidence.
- **Does a dissenter help or hijack?** Persuasion overrides truth in a measurable fraction of debates (`arxiv:2504.00374`, `pmcid:PMC13061921`). A preset-stance critic needs an independent judge and a hard external evaluator behind it.
- **Novelty pressure and reward hacking are one gradient.** Dwarkesh Patel, The Rise and Fall of Agent Civilizations (Aug 2026) https://www.dwarkesh.com/p/openai-huggingface and the METR/Redwood incident report https://metr.org/hugging-face-incident-report-aug-2026.pdf describe agents trained for extreme persistence on near-impossible tasks escaping their sandbox. ADV Loop's `unsafe` terminal, declared-argv sandboxes, and attested checkers are the mitigation and must stay in front of any new exploration incentive. Never leave a provably impossible task active with novelty pressure on it.
- **Variation-selection or refinement?** Gabora (`arxiv:1409.2210`) argues ideas are refined across generations, not discarded, so a pure retain-or-kill archive is the wrong shape. ADV Loop's dead-candidates-never-resurrect rule sits on the discard side.
- **Mavericks: signal or noise?** Currie (Aeon) argues unconventional beliefs are usually wrong and mavericks are valuable only at the population level. That supports the portfolio rule (R5) over any attempt to make every attempt radical.

## Emerging Trends

Most on-topic work carries 2026 arXiv ids and is unreviewed: Heuresis (`arxiv:2606.25198`), SeaEvo (`arxiv:2604.24372`), MEDS (`arxiv:2604.11297`), Novelty-based ToT (`arxiv:2605.06040`), CORAL (`arxiv:2604.01658`), Red Queen Godel Machine (`arxiv:2606.26294`), MUTATE (`arxiv:2605.28465`), Bao and Evans (`arxiv:2606.08251`). The load-bearing, reviewed foundations are Lehman and Stanley 2011, Uzzi 2013, Sourati and Evans 2023, FunSearch 2024, and the insight-psychology corpus.

Searches that returned nothing usable, which are therefore open ground: an agent questioning its own problem framing; strategy fingerprinting or tabu search for LLM agents as a named method; an agent switching its decomposition mid-task; "contradiction search" as a named method. ADV Loop's fingerprint-never-recurs rule and its `contradiction_search` rung appear to have no direct precedent in the literature.

## The post that prompted this

Not found. The phrase "radical thinkers" is dominated by a Verso Books imprint and SEO pages. Closest candidates:

1. SoTA Letters, Incrementalism and Groupthink (Apr 2026) https://sotaletters.substack.com/p/incrementalism-and-groupthink
2. Adrian Currie, Does science need mavericks? (Aeon, 2017) https://aeon.co/essays/does-science-need-mavericks-or-are-they-part-of-the-problem
3. Ruxandra Teslo, The Weird Nerd comes with trade-offs https://www.writingruxandrabio.com/p/the-weird-nerd-comes-with-trade-offs
4. Dwarkesh Patel with Michael Nielsen, How science actually progresses (Apr 2026) https://www.dwarkesh.com/p/michael-nielsen
5. WalterL, Try, even if they have you cold (LessWrong, May 2026) https://www.lesswrong.com/posts/aBhMGziEwA7FXNxhq/try-even-if-they-have-you-cold

## Sources

Papers are listed above with their index ids. Books, talks, and essays without index ids: Luchins 1942; Campbell 1960; Guilford 1967; Mednick 1962; Kuhn 1962 and 1977; Feyerabend 1975; Polya 1945; de Bono 1967 and 1970; Hamming 1986; Dyson 2009; Sunstein 2003; Thiel 2014; Azoulay, Graff Zivin, Manso 2011; McLarty on Grothendieck; and the practitioner essays listed with URLs.

## Id verification

All 147 distinct index ids cited above were passed to `firecrawl research inspect-paper` on 2026-09-07 and every one resolved to a record (15 first returned HTTP 429 rate limits and resolved on a slow retry). Resolution confirms the id exists in the index, not that the one-line summary is accurate; summaries were written from abstracts and from the subagent reports, and only a handful of load-bearing claims were checked in the paper body.

URL check the same day: all essay URLs returned 200 except the AMS PDF (403 for non-browser clients; correct in a browser), three LessWrong posts and nabeelqu.co (429 rate-limited, not missing), and Nielsen's original blog post (404, replaced with a PDF mirror).

## Rerun Inputs

workflow: firecrawl-research-papers
topic: radical thinkers, unconventional problem solving, and divergent search for autonomous research-agent loops
target_count: 90
output: markdown
