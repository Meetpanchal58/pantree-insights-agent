# Pantree Insights v2: verified answers by construction

Status: proposed (Nov 17, 2026). Replaces the "LLM writes SQL, many checks veto it" pipeline.

## 1. Why v1 cannot reach the accuracy this project needs

The Nov 17 production run on v1 (the architecture with understanding, planner, SQL writer, safety checks, result
checks, number check, LLM reviewer and claim checker) answered 47 of 60 SQL questions correctly (78%) at the time
of the analysis. Re-scoring every SQL attempt in the 13 failures against the gold answers showed:

| Cause | Failures |
|---|---|
| The model wrote a correct query; a check or the reviewer rejected it, or a retry replaced it with a wrong one | 5 |
| The understanding step stopped to ask when it should not have | 2 |
| The test itself was wrong (scorer, question wording) | 2 |
| The model read the question wrongly (left out a share, wrong split, wrong filter) | 4 |

Two lessons:

1. **Free-form SQL from a model has a failure floor.** Even when it is usually right, each question is a new roll:
   a plausible query that returns a plausible wrong number. Industry results agree: text-to-SQL that "fails
   silently" is the core risk ([dbt Labs, 2026](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)).
2. **More checks did not fix it.** Eight places could veto an answer; their false alarms compounded and some
   contradicted each other. Adding a ninth would not have helped.

For decisions that cost money, 95% is not good enough. The design goal is therefore not "a better guess" but:

> **Every number shown as Verified is correct by construction. A question the system cannot answer that way is
> asked back or answered as clearly labelled exploratory work, never as a confident wrong number.**

## 2. What the industry does (2026)

| System | Approach | Relevant lesson |
|---|---|---|
| dbt Semantic Layer (MetricFlow) | The LLM chooses metrics, dimensions and filters; MetricFlow compiles the SQL | 98-100% on questions the layer covers vs 84-90% for text-to-SQL on the same data; outside its scope it **errors instead of guessing** ([dbt 2026 benchmark](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)) |
| Cube AI API | "Text-to-semantic-layer": the model emits an API request, not SQL | Constrained output leaves no room for a "technically valid hallucination" ([Cube](https://cube.dev/blog/introducing-the-ai-api-and-chart-prototyping-in-cube-cloud)) |
| Snowflake Cortex Analyst | Semantic model + Verified Query Repository; evaluations against verified queries | Vendor claim 90%+; verified queries are reused for similar questions; usage suggests new questions to verify ([VQR docs](https://docs.snowflake.com/user-guide/snowflake-cortex/cortex-analyst/verified-query-repository), [evaluations](https://docs.snowflake.com/user-guide/snowflake-cortex/cortex-analyst-evaluations)) |
| Databricks Genie | "Trusted assets": parameterized queries and SQL functions; answers built from them are labelled Trusted | A visible trust label separates certified answers from generated ones ([trusted assets](https://docs.databricks.com/aws/en/genie/trusted-assets)) |
| Benchmarks | Best BIRD systems about 82% vs 93% human; enterprise schemas much lower | Free-form text-to-SQL is not at 99% anywhere ([count.co summary](https://count.co/ai/text-to-sql), [Atlan](https://atlan.com/know/ai-agent/data-for-ai/text-to-sql-with-ai/)) |

The common pattern, and the one this design adopts: **the model translates the question into a structured request;
code turns the request into SQL; answers built that way carry a trust label; everything else is labelled as such.**

## 3. The v2 flow

```
question
  -> 1. guard (code)                personal data, injection, length        -> decline
  -> 2. interpret (1 LLM call)      question -> QuerySpec (JSON, schema-constrained)
  -> 3. validate (code)             every metric, dimension, value, date exists in the catalog
                                    unknown value / unsupported request     -> ask back, with options
  -> 4. confirm (UI)                read-back rendered FROM THE SPEC; user confirms or corrects
  -> 5. compile (code)              spec -> SQL from metrics.yaml + join graph   (no LLM)
  -> 6. run (DuckDB, read-only)     result table, cached by (spec, data snapshot)
  -> 7. narrate (1 LLM call, streamed)  text with {c1} placeholders; numbers filled from the result by code
                                        a claim that cannot be checked is dropped, the answer is not failed
answer label: VERIFIED
```

Questions the spec cannot express (multi-step analyses such as event lift, cohorts, funnels, attribution) take one
of two other routes, in this order:

- **Verified analysis templates** (Tier 2): reviewed, parameterized SQL from the query library ("lift of each sale
  event vs the N days before"). The LLM only picks the template and fills parameters, which code validates. Label:
  VERIFIED.
- **Exploratory SQL** (Tier 3): the current SQL writer with execution feedback, one retry, no veto chain. Label:
  EXPLORATORY - NOT VERIFIED, SQL shown. Stakeholder mode can switch Tier 3 off entirely. Repeated exploratory
  questions are reviewed and promoted to templates (the Cortex "optimization" loop).

### The QuerySpec

```json
{
  "metrics": ["return_rate"],                      // names from metrics.yaml only
  "dimensions": ["product_gender"],                // concepts, incl. derived (payment_type, day_type)
  "filters": [{"dimension": "product_gender", "op": "in", "values": ["Women", "Men"]}],
  "time": {"column_meaning": "placed", "from": "2026-01-01", "to": "2026-11-14", "grain": null},
  "ranking": null,                                 // {"by": metric, "order": "desc", "limit": 10}
  "with_share_of_total": false,
  "compare": null                                  // {"to": "previous_period"} | {"members": [...]}
}
```

Every field is a closed choice from the catalog except dates and numbers, which code range-checks. The model cannot
invent a column, a join or a formula, because it never writes any.

### Why this reaches the bar

- **Compile step: 100% by construction and by test.** The compiler is deterministic; every metric x dimension x filter
  combination it emits is tested against hand-written SQL (the query library already has 128 reference queries and
  recomputation checks). A compiler bug is a test failure, not a production surprise.
- **Interpretation step: made checkable.** The only model judgement left is "which metric, which split, which
  filter". That judgement is (a) constrained to valid choices, (b) shown back to the user in business words before
  anything runs, (c) checked by agreement: two independent interpretations must produce the same spec, otherwise
  the differences become a question to the user. A disagreement is never resolved by guessing.
- **No veto chain.** Hard stops remain only where they are certain: safety, an invalid spec, SQL that fails to run.
  Nothing can reject a compiled verified answer, so the 5 + 2 failures of section 1 cannot happen.

How the 8 v1 failures that were not test errors map onto v2:

| v1 failure | v2 |
|---|---|
| x03, x06, x08, y06, m08: correct SQL rejected | no reviewer veto, no SQL to reject; claims dropped instead of failing |
| h07, x14: needless questions | answers are settled choices; values validated against the catalog with aliases |
| m05: share left out | `with_share_of_total` is a spec field; "most common" sets it |
| y04: did not limit to Women and Men | the comparison members become an `in` filter in the spec, compiled exactly |
| n01: wrong split | `day_type` is a derived dimension in the catalog |
| h10: "wrong size" read as a return reason | ambiguous wording produces two different specs -> asked back |

## 4. Accuracy is measured as two numbers, never one

- **Verified correctness** (target 100%, release-blocking): share of VERIFIED answers whose result equals the gold
  answer. Any miss is a bug with a root cause in the spec mapping, the catalog or the compiler.
- **Coverage**: share of questions answered as VERIFIED (target grows over time, 85%+ of real questions).
- **Silent-wrong rate** (target 0): exploratory answers presented without their label - must be impossible by
  construction (UI renders the label from the route).

Evaluation also becomes cheaper and sharper: each question carries a gold spec, so most of the suite scores the
interpretation by exact spec comparison (one cheap LLM call per question) and the compiler by free unit tests.

## 5. Latency

| Step | v1 | v2 |
|---|---|---|
| LLM calls per answer | 5-7 (understand, plan, write, review, analyse, rewrites, retries) | 2 (interpret, narrate); 3 with the agreement check |
| Typical time to first result | 25-60 s | table shown after the interpret call (~3-5 s); narrative streams after |
| Retrieval | hybrid BM25 + embeddings on every question | none on the verified path: the metric and dimension catalog fits in the cached prompt prefix; value lookup is an in-memory index |

The result table is streamed to the browser as soon as the query finishes; the narrative streams token by token.

## 6. Many users at once

- **Bounded work, not a thread per question**: a worker pool sized to the instance (e.g. 8 concurrent runs per vCPU
  pair), a queue with a visible position ("2 questions ahead of you"), and a per-user limit of 2 runs in flight.
- **Provider protection**: one semaphore around LLM calls, exponential backoff with jitter on 429/5xx, and a
  circuit breaker that switches to "verified templates only" when the provider is degraded.
- **Exact result cache**: the key is (spec hash, data snapshot id). Because the spec is deterministic, a cached
  answer is exactly the answer, unlike semantic caches over question text, which can return a wrong neighbour.
  Identical in-flight specs are coalesced into one query.
- **Data access**: DuckDB read-only views; one cursor per request; snapshot id on every result so a cache never
  crosses a data refresh.
- **Serving**: Cloud Run with min-instances 1 (no cold start), request concurrency matched to the worker pool, SSE
  for streaming (already used), sticky sessions not required because runs are checkpointed in Postgres.

## 7. Cost per request

- 2-3 LLM calls instead of 5-7: roughly 60-70% fewer tokens per answer.
- Stable prompt prefix (rules, catalog, metric list) first, question last, so the provider's prompt cache discounts
  most input tokens on every call.
- Narration may use a smaller model than interpretation (numbers are filled in by code, so the narrator cannot get
  them wrong); interpretation keeps the strongest model, because that is where accuracy is decided.
- Cached results cost nothing; per-request and daily budget caps stay.
- Target: under US$0.01 per verified answer (v1: about US$0.025-0.05).

## 8. Tech stack

| Layer | v1 | v2 | Why |
|---|---|---|---|
| Semantic layer | metrics.yaml, concepts.yaml, catalog.yaml, own compiler (off) | same files, **compiler is the primary path**; formalized as entities, measures, dimensions (MetricFlow-shaped) | already built and tested; the shape keeps a later move to dbt MetricFlow or Cube possible without a rewrite |
| Interpretation | understand + plan agents, free text | one structured-output call returning a QuerySpec (JSON schema), plus an optional second sample for agreement | constrained output, checkable |
| SQL | LLM writer + 8 checks | deterministic compiler; templates; exploratory writer only as labelled fallback | correctness by construction |
| Orchestration | LangGraph with many nodes | LangGraph kept, 6 nodes (guard, interpret, confirm, compile/run, narrate, finish) | keeps interrupt/resume and Postgres checkpoints, drops the veto loop |
| Retrieval | LlamaIndex BM25 + fastembed on every question | only for Tier 2/3 (template and example search) | latency and fewer moving parts |
| Database | DuckDB in memory, read-only views | same, plus result cache and snapshot id | fits the data size; fast |
| Web | FastAPI, thread per run, SSE | FastAPI, bounded worker pool + queue, SSE streaming of table then narrative | smooth under load |
| Models | one model for every agent | strongest model for interpretation; smaller model for narration | accuracy where it matters, cost elsewhere |

Not adopted now: an external semantic-layer server (Cube, dbt Cloud). One DuckDB file and a working compiler do not
justify another service; the spec format is kept compatible so this can change later.

## 9. Status

**Phase 1 done (Nov 18, no paid calls):** `src/pantree_insights/spec.py` (QuerySpec, catalog validation,
deterministic compiler), `evals/gold_specs.yaml` (a gold spec for every one of the 98 SQL questions),
`evals/spec_eval.py` (free check, also in CI) and `tests/test_spec.py`.

| Free measurement | Result |
|---|---|
| Questions the compiler can answer (coverage) | 94 of 98 (96%); the other 4 are the multi-step analyses planned as templates (h03, h04, h07, h08) |
| Compiled answers equal to the gold answer | 94 of 94, on the full and the small shop |
| Every named metric x every dimension of its table vs the independent recompute | all pairs agree, or are refused because the join would repeat rows |
| Compiled SQL passing the existing safety checks | 94 of 94 |

Semantic-layer fixes found on the way: last-click revenue is a compilable metric that only counts rows with a
campaign (an empty "campaign" worth Rs 3.4 crore was ranked first before); new vs repeat customers is a labelled
split. These numbers measure the compiler and the catalog; how well the model fills specs is measured in phase 2.

**Phase 2 built (Nov 18, free checks only):** `src/pantree_insights/interpret.py` and the `v2` variant of the
agent (`--variant v2`): one structured interpreter call over the whole catalog (about 15k tokens, the same for
every question, so the provider caches it); values normalised to their stored spelling; one repair round with the
validator's exact problems; a second independent reading that must agree (a third breaks a tie; no majority = the
readings are offered to the user, even when defaults are allowed); the read-back rendered from the spec; compiled
execution labelled `trust: verified`; multi-step questions go to the v1 pipeline labelled `trust: exploratory`;
a summary sentence whose number cannot be checked is removed instead of failing the answer. Ten scripted end-to-end
tests cover every route (`tests/test_v2_flow.py`). The evaluation reports verified correctness, coverage,
exploratory correctness and spec equality with the gold spec.

## 10. Delivery plan

1. **Spec and compiler (accuracy core)**: QuerySpec schema and validator; compiler extended to several metrics,
   `in` filters, time grain, ranking, share of total, period comparison, derived dimensions, window joins; tests
   comparing compiled SQL with the 128 library queries and every gold answer the spec can express.
2. **Interpretation**: one structured call, agreement check, read-back rendered from the spec, routing to
   templates and the exploratory fallback; trust label in the API and UI.
3. **Templates**: event lift, funnel, cohort and attribution analyses as parameterized verified queries.
4. **Serving**: worker pool, queue position, per-user limits, provider semaphore and backoff, result cache,
   streaming table then narrative.
5. **Evaluation**: gold specs; verified correctness, coverage and silent-wrong rate reported separately; one paid
   run on the full suite when the free checks are green.
