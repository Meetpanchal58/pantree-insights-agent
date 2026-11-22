# Current evidence

This directory is the only published evaluation set. It replaces previous generated reports; the reproducible evaluator code and independent reference suites remain under evals/.

The paid run selects 100 cases before inference, with seed 20261122: 56 answer cases and 44 behavior cases. It uses the configured OpenAI model through the production managed adapter, autonomous blind plan confirmations, isolated conversational memory, and a cumulative $4 ceiling. All 100 selected cases completed. Known token-based cost: $0.637558; uncertain holds: $0.000000. The all-pass evaluation gate failed.

| Artifact | Meaning |
| --- | --- |
| [results.md](results.md) | Canonical table with confidence intervals and observed failure types |
| [paid.json](paid.json) | All 100 raw scored attempts and traces, including failures |
| [paid-budget.json](paid-budget.json) | Known cost, uncertain holds, configured rates, and run settings |
| [failure-audit.md](failure-audit.md) | Review of strict-reference mismatches; scores unchanged |
| [source-map.json](source-map.json) | Evaluated local commit mapped to an identical published Git tree |
| [selection.json](selection.json) | Exact selected case IDs, categories, and questions |
| [compiler.json](compiler.json) | Intended specifications versus independent SQL, three passes, with provenance |
| [compiler.md](compiler.md) | Free compiler timings and limitations |
| [spec-check.txt](spec-check.txt) | All 98 compiler/template references match |
| [gold-check.txt](gold-check.txt) | All 129 reference SQL queries validate |
| [fastpath.json](fastpath.json) | Five reviewed deterministic questions, three passes; no model calls |
| [context-retrieval.json](context-retrieval.json) | Required-metric recall for keyword/BM25 top-k retrieval; no embeddings measured |
| [examples.json](examples.json) | README answer excerpts and the independent SQL behind them |

Free checks use the small simulator snapshot through 2026-11-22. Latency is local, shares process/OS caches, and excludes user confirmation time. Free checks were run concurrently on the same host; their timings are descriptive, not a controlled performance comparison. Paid calls run sequentially; provider caching is uncontrolled. Local regression tests overlapped part of the paid run, so timings also include host contention.

Raw reports retain hashes of the source, selected cases, and dataset. A local commit ID records the evaluated checkout; source and data hashes are the reproduction identity. Later documentation edits do not change the evaluated analytical behavior.

No result here establishes narrative faithfulness, causal accuracy, Cloud Run response time, or concurrent-user capacity. Model and compiler correctness have different denominators. Keep failures, unresolved cases, and uncertain cost in the report.

## Software checks

Local regression checks passed: 1,711 tests with one existing skip; 548 no-data tests; desktop/mobile frontend and workspace suites; Ruff and local-link checks. Four final budget/reporting tests also passed after adding overwrite protection. These software checks do not override the paid evaluation gate failure.
