# Contextual investigations, first-request latency and dataset readability

Baseline: PR #41 merged at `bfaf262bfa89b320a816bc3377ebf85a42ecd9af`.
Branch: `fix/contextual-investigations`. Evidence collected 22 November 2026, Python 3.12,
PostgreSQL 17, small synthetic shop covering 2025-01-01 through 2026-11-22.
No production deployment, provider inference or hosting-cost change was performed.

## Architectural diagnosis and changes

| Failure boundary | Root cause found in code | Implemented behavior |
|---|---|---|
| Conversation → business scope | Generic ambiguity rules ran before resolving the previous ranked measure; summaries retained only six example rows. “These brands” had no exact result-set reference. | Persist complete, snapshot-bound populations separately from prose. A typed query/dimension reference binds actual members on the server, with stable product/seller IDs. Inherit the established performance metric and same-fact reporting period. |
| Semantic plan → SQL | Labels automatically inject their IDs, but an explicit ID injected them again. Cosmetic measure names could collide with group names. | Deduplicate identical group expressions/IDs; disambiguate genuinely different same-named dimensions; normalize cosmetic measure aliases and dependent ratio/ranking/share references. |
| Ranking → breakdown | Ranking could only limit the entire result, so a top-products-per-brand question had no representation. | `Ranking.partition_by` ranks after aggregation, per parent group, on single- and multi-fact results. Its meaning appears in the confirmation. |
| Repair → conversation state | Repair received error text without the failed structured proposal. Removing already-answered questions could leave an empty clarification. | Include the previous proposal in one bounded repair; suppress settled generic follow-up ambiguity; terminate unresolved planner errors explicitly. Real business ambiguities remain questions. |
| Result → verification | A product word could also be another brand: “Red” inside a 2go product title was treated as a claim about the brand Red. | Prefer complete observed entity labels over contained substrings. Explicit wrong-brand statements still fail. |
| Evidence → next turn | An incomplete latest result could leave the older successful population available as “these.” | Clear the active population after displayed partial/unverified results, retaining historical evidence. Refuse stale snapshots and incomplete legacy excerpts. |
| Multi-fact scope | The compiler supported native-grain multi-fact aggregation, but its blanket ban on qualified fields also blocked exact shared product IDs. | Permit catalogue primary keys, retain conformed-expression and join-cardinality checks, and continue rejecting fact attributes with incompatible meanings. |
| Validation latency | High-cardinality filter values triggered a separate scan for every ID. | Validate the whole requested set in one parameterized query. |
| Idle startup → ready analyst | Detached lifespan initialization is unreliable after requests end on request-based, CPU-throttled Cloud Run instances. | An authenticated, nonblocking browser readiness request holds CPU during single-flight data preparation; opening/focusing the app starts it. No model call, question charge, periodic keep-alive or resource change. |

The screenshot is now a regression through actual LangGraph confirmation, SQL execution and grounding with
a scripted model. The supplied follow-up retains exactly the selected brands and reporting period, adds
category/product detail, repairs the cosmetic `product_id` measure collision, and produces a nonempty verified
answer. General cases additionally cover 12-member populations, duplicate IDs, cross-fact metrics, same-named
city dimensions, per-brand ranking, explicit exclusions, restarted conversation storage and partial results.

## Cross-table analysis and root-cause limits

The existing compiler already aggregates each metric at its native fact grain before joining on conformed
business dimensions. Joining raw sales lines directly to review rows would multiply revenue; this patch
preserves the separate aggregation and independently checks sales and review metrics against their source
queries. Catalogue identities connect the exact selected product set across both sources.

For “why did revenue change?”, a governed period comparison can quantify category contributions to the total
change. The regression checks that category deltas sum to the overall delta and contributions sum to one.
This is descriptive attribution for an additive revenue metric. A review score correlated with a revenue
decline is evidence to investigate, not proof that reviews caused the decline. The verifier still rejects
unsupported causal assertions.

These are deliberately bounded capabilities:

- A plan contains at most four approved queries, not an autonomous, adaptive causal investigation loop.
- Each query's explanation is grounded in its own result. General cross-query causal synthesis would need
  typed evidence references across query IDs, explicit hypotheses and validation of combined claims.
- Shared dimensions and approved joins are required. Incompatible dates/grains and unsupported template
  population bindings must be clarified; arbitrary raw table joins remain prohibited.
- Exact references require complete results of at most 500 rows and a matching snapshot. SQL truncation can
  impose a smaller practical bound. Six-row legacy excerpts cannot establish a complete population.
- Language interpretation remains model-dependent. The new regressions use scripted outputs and real data;
  they do not demonstrate arbitrary live-model question accuracy.

## First question after idle

Thirty to sixty seconds is plausible with scale-to-zero plus several serial model calls, but should be measured
and reduced. The supplied screenshot separately shows repeated interpretation stages of roughly 13 seconds
each. A single “cold start” label would hide that work.

The deployment defaults are one CPU, request-based CPU allocation and minimum instances zero. Preparation
includes DuckDB, join/profile metadata and checkpoints. Published profile artifacts avoid rebuilding joins;
missing/stale artifacts deliberately fall back to measured profiling. Starting this work while the signed-in
user browses helps the first analytical question, but cannot remove image startup or provider latency.

Reproduce the free local measurement:

```bash
DATA_SIZE=small uv run python evals/startup_baseline.py
```

| Fresh process, local small data | Imports | Analyst preparation | First proposal | Next proposal |
|---|---:|---:|---:|---:|
| Published profile | 1.258 s | 0.527 s | 0.051 s | 0.018 s |
| Forced profile rebuild | 1.092 s | 4.056 s | 0.056 s | 0.018 s |

These are individual samples, memory checkpoints and deterministic proposals; no full answer, provider,
GCS-mounted data, platform startup or controlled cold filesystem cache. They establish the local preparation
cost difference, not a production speedup percentage. The current interpreter also sends the full governed
catalogue (about 57 KB of prompt text in this snapshot); provider prefill and cache misses remain a potential
cost. Schema retrieval/pruning would require separate recall checks before removing relevant join context.

Production verification should correlate platform startup, `agent_prepare` phase logs, authenticated
`/api/ready` duration and streamed interpretation/repair/contrast/query/analysis times on idle versus warm
requests. No production logs or live provider credentials were available here. Minimum instances or
always-allocated CPU are separate paid options, not prerequisites for this code change and not guarantees
of fast model responses.

## Dataset Preview

The entry button is larger and explains its purpose. The full-screen overview uses larger body/table text,
roomier cards, a three-step first-visit guide, and business labels for row grain, identifiers and dates.
Desktop light and mobile dark screenshots were captured from the actual local FastAPI app using synthetic
Parquet data and a local test account. New screenshots remain local; historical PR #41 screenshots are unchanged.
Navigation still preserves the chat, pending confirmation and stream.

## Validation

- Full local pytest with real PostgreSQL: 1,678 passed, one existing snapshot-fixture skip. Subsequent
  checks also cover exact populations in alternative readings and explicit reporting-period changes.
  The final submitted commit is checked by the full GitHub CI suite.
- All 129 evaluation gold queries valid; 294/294 compiler comparisons matched across 98 cases and three passes.
- Grounding/context/period regression suite: 80 passed before the final three additions.
- Existing frontend and workbook browser suites passed, including desktop/mobile and light/dark navigation.
- Ruff and whitespace checks passed; no dependencies changed.

Tests enforce confirmation before execution, complete result references, snapshot checks, user isolation,
native-grain aggregation, no fan-out inflation, continued genuine clarification and explicit nonempty failure.
They do not substitute for a live provider evaluation or deployed idle/warm latency measurements.
