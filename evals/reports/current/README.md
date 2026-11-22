# Current evidence

This directory is the only published evaluation set. Earlier runs were removed; the evaluator code, question suites, reference SQL and gold specifications under evals/ reproduce it.

The paid run is autonomous: blind plan confirmations, and no answers to the agent's clarifying questions. It used code `24b3f1e` on 22 November 2026, the configured OpenAI model through the production managed adapter, isolated conversational memory and a cumulative $2 ceiling. It selected 100 cases before inference with seed 20261122: 56 answer cases and 44 behavior cases. All 100 completed. Known token cost was $0.926527, with $0 in uncertain holds. The all-pass evaluation gate failed.

| Result | Value |
| --- | --- |
| Correct answer tables | 51/56 (91.0%) [83.9–98.3%] |
| Holdout answer tables | 18/18 |
| Verified-labelled answers matching the reference | 51/53 (96.2%) |
| Silent wrong answers | 2 (m11: contacts vs tickets; c21: correct counts in rows instead of columns) |
| Correctly declined | 12/12 |
| Correctly clarified | 6/6 |

| Artifact | Meaning |
| --- | --- |
| [results.md](results.md) | Canonical table with confidence intervals and observed failure types |
| [paid.json](paid.json) | All 100 raw scored attempts and traces, including failures |
| [paid-budget.json](paid-budget.json) | Known cost, uncertain holds, configured rates and run settings |
| [selection.json](selection.json) | Exact selected case IDs, categories and questions |
| [compiler.json](compiler.json) / [compiler.md](compiler.md) | Intended specifications versus independent SQL: 294/294 across three passes, no model calls |
| [spec-check.txt](spec-check.txt) | All 98 compiler/template references match |
| [gold-check.txt](gold-check.txt) | All 129 reference SQL queries validate (172 items, 0 problems) |
| [fastpath.json](fastpath.json) | Reviewed deterministic questions, three passes; no model calls |
| [context-retrieval.json](context-retrieval.json) | Required-metric recall for keyword/BM25 top-k retrieval; no embeddings measured |

Scoring notes:
- Regression cases list the columns their question asks for, and supporting counts are optional; holdout and coverage cases already worked this way.
- A singular superlative may be answered by its winner.
- The c24 reference was corrected before this run (paid ad spend excludes CRM and affiliates) and is marked `tuned_on`.
- The suites were used during development, including the holdout, so these are development-benchmark results, not an unseen external test.

After this run, the f05 behaviour failure ("customers who placed orders") was fixed. That fix has not been re-measured.

Free checks were run on the small simulator snapshot through 2026-11-22. Latency is local, shares process/OS caches and excludes user confirmation time. Paid calls run sequentially; provider caching is uncontrolled. Nothing here establishes narrative faithfulness, causal accuracy, Cloud Run response time or concurrent-user capacity.
