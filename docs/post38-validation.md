# Optimization after PR #38

Baseline: merged PR #38, `86e93ea7c665b657152156a8bf04927c9594c28a`. Validation: 22 November 2026,
small simulator snapshot ending 2026-11-22, seed 42. No production deployment, secret changes, paid model calls,
new runtime dependencies or additional agents. PostgreSQL 17 ran in a disposable local container.

## Implemented behavior

- Complete KPI questions can bypass the model inside known self-contained conversations and after prior insights.
  Standing user/project/business instructions still require interpretation. Explicit edits such as “And by brand?”
  preserve the previous successful QuerySpec; unfamiliar context falls back. Entity filters resolve only to a
  unique stored spelling. Date, grain, ranking, entity and metric checks catch explicit dropped requirements;
  clarification no longer disables original-intent checks wholesale. The full semantic catalog remains available.
- Contrast is skipped for grammar decisions and model plans independently matching that grammar without conflicting
  background or answers. Other readings retain contrast and confirmation. Single-row numeric results use a
  deterministic explanation. General complex questions can still exceed two model calls after repairs.
- `QuerySpec.operation` adds whole-period comparison, daily rolling sum and running sum. Both comparison periods
  use the existing compiler, including safe cross-fact pre-aggregation, distinct counts and original denominators.
  Windows require explicitly time-additive metrics; ratios, averages, distinct counts and snapshots are refused.
  They use calendar days, include lookback, emit observed days only, and permit at most 367 output days.
- Three bounded templates add mature first-purchase cohorts, ordered session funnels and recency/frequency segments.
  Cohorts use first-ever orders and exclude immature buyers. Funnels require strictly later events in the same
  session; conversion uses first-step sessions. Segments aggregate order lines to orders before customers.
- Direction and extremum language needs checked evidence in the same sentence. Causal-language coverage is wider.
  Summaries must mention every requested numeric measure with observed values; unsuccessful repair falls back to
  deterministic cells. These lexical controls do **not** establish arbitrary prose correctness.
- History and the last successful plan now use one bounded read. Checkpoints use a pool capped at four connections.
  Answer, terminal run record and user spend commit together, once. Optional ask idempotency keys reuse the run ID
  and reject changed payloads. Interrupted inference is not automatically restarted. Paused checkpoints resume
  only with matching dataset/semantic/prompt fingerprints. Old unversioned checkpoints require a new question.
- Context time and provider queue/inference time are observable separately. Early result streaming remains intact.
  Dataset, semantic definitions and prompts participate in cache/checkpoint identity; definition caches refresh
  only on version changes. Published join-profile startup remains covered by the no-scan regression test.

## Evidence and acceptance limits

| Measurement | Before | After |
|---|---:|---:|
| Full local tests | 1,528 passed, 1 skipped | 1,569 passed, 1 skipped |
| Independent compiler gold, three passes | 294/294 on PR #38 CI | 294/294; all 98 cases pass all three repeats |
| Gold SQL validation | 129 valid | 129 valid, zero problems |
| Five grammar questions × five passes, independent SQL | 25/25 | 25/25 |
| Same five questions in existing simple history, no model allowed | 0/5 completed | 5/5 completed |
| Repeated question with retained memory | Second run required model | Both runs completed without model |
| Warm no-model request p50 / p95, n=20 | 46.2 / 58.4 ms | 51.2 / 59.2 ms |
| First pass through five questions p50 / p95 | 74.7 / 310.8 ms | 102.9 / 298.0 ms |
| Fresh Agent preparation p50 / p95, n=3 | 76.3 / 516.6 ms | 58.7 / 546.4 ms |
| PostgreSQL history, 2,000 messages, p50 / p95, n=20 | 17.55 / 18.85 ms | 2.23 / 2.94 ms |
| PostgreSQL checkpoint write p50 / p95, n=20 | 1.07 / 1.63 ms | 1.02 / 1.67 ms |
| PostgreSQL checkpoint read p50 / p95, n=20 | 0.46 / 1.03 ms | 0.87 / 2.94 ms |
| Calls / tokens / cost for completed grammar benchmark | 0 / 0 / $0 | 0 / 0 / $0 |
| Live first-turn / SQL / end-to-end model accuracy | Unmeasured | Unmeasured |
| Independent general narrative faithfulness | Unmeasured | Unmeasured |
| Cloud Run cold/warm latency, resource/concurrency matrix | Unmeasured | Unmeasured |

The same harness ran serially against the baseline and changed source on the same workspace. Request timing excludes
Agent preparation; warm requests share SQL and OS caches. Fresh Agents share imports and may share OS/profile caches:
**these are not controlled cold-process or Cloud Run measurements**. Samples are small. Warm local request latency is
roughly unchanged, not a demonstrated overall speedup. Pooling does not improve the local checkpoint-read benchmark;
it adds bounded connection ownership while preserving restart correctness. Remote database latency remains unknown.

The storage experiment materialized only three columns of 25,796 sales rows. After-change local Parquet query
p50/p95 was 4.04/5.19 ms versus 2.33/2.90 ms materialized; materialization took 16.54 ms. This narrow benefit does not
justify shipping a second runtime storage path. GCS-mounted data was unavailable; local Parquet remains the default.

Raw measurements: [before](validation/post38-before.json), [after](validation/post38-after.json),
[source/dependencies](validation/post38-size.json), [compiler cases](validation/post38-compiler.json). The files include source-content hashes and snapshot metadata.
No production latency or paid-model cost reduction is inferred from these measurements.

## Architectural scorecard

The “before” numbers below are the user's qualitative assessment, **not measured accuracy**. Local tests support
specific improvements, not calibrated numerical rescoring or an automatic overall score of 94.

| Component | User's before | After: supported assessment |
|---|---:|---|
| Core LangGraph | 93 | More deterministic conversation routes; bounded repair and confirmation retained |
| Semantic layer/compiler | 94 | Explicit time-additivity policy; independently checked period/window populations |
| Question understanding | 81 | Stronger explicit-scope checks and plan edits; live interpretation gains unmeasured |
| Multi-table flexibility | 87 | Composable period comparison plus bounded cohort/funnel/segment stages |
| SQL safety | 96 | New generated SQL passes unchanged safety controls; no free model SQL |
| Insight grounding | 91 | Evidence-linked direction/rank checks and completeness fallback; general prose unmeasured |
| Backend latency | 80 | History read improves; local warm latency similar; remote bottlenecks unmeasured |
| Production scalability | 76 | Real PostgreSQL retry/restart tests and bounded pool; still one instance |
| Evaluation methodology | 91 | Autonomous default; assisted results explicitly separate; narrative limits visible |
| Overall | 87 | Locally validated improvements; no defensible numerical reassignment |

The requested 95% first-turn, 95% end-to-end, 99% governed KPI and numerical, 95% narrative, 90% coverage, and
3–8s/8–25s model-response targets remain **unvalidated** on representative live-model traffic. Selected deterministic
fixtures are not an estimate of those percentages. No live-model failure is claimed resolved without a live run.

## Simplification, source size and reliability

Runtime Python: **23 → 24 files, 5,791 → 6,245 lines, 289,249 → 316,464 bytes**. Including HTML/CSS/JavaScript:
27 → 28 files, 7,068 → 7,522 lines, 408,876 → 436,129 bytes. Counts include comments and blank lines.
Direct runtime dependencies remain **20 → 20**; `psycopg_pool` was already a checkpoint dependency.

Removed the unused separate user-cost update and duplicate channel-parameter model. Consolidated terminal writes,
customer-order pre-aggregation and bounded history/previous-plan retrieval. New period/window stages reuse the
existing compiler rather than duplicating metric SQL. Legacy frontend step labels remain because saved demos use
them. Dependency audit found existing uses, including PyArrow for saved pulse Parquet and LangChain's provider
factory; none were removed merely to reduce counts. The net growth supplies six new analytical primitives,
validation and reliability controls, rather than a new service or framework.

Real PostgreSQL tests verify 40 concurrent quota attempts admit seven, 40 spend increments are retained, 30 terminal
retries commit one answer/charge, a mid-transaction error leaves no partial completion, and a paused LangGraph
checkpoint survives Agent restart. CI now runs these tests against PostgreSQL 17. Existing bounded queue, completed
history retention, snapshot refresh, authorization, timeout and SQL injection tests remain enabled. The one skipped
local test is the existing offline-demo snapshot fixture mismatch against the nightly simulator.

## Reproduction and remaining work

```bash
uv sync --locked
PANTREE_TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres uv run pytest -q
DATA_SIZE=small uv run python evals/run_eval.py --suite all --dry-run
DATA_SIZE=small uv run python evals/baseline.py --repeats 3
DATA_SIZE=small uv run python evals/post38_benchmark.py
node tests/frontend.cjs  # Playwright Chromium must be installed
```

The PostgreSQL tests create and drop isolated databases; use a disposable server and a role allowed to create them.
The benchmark's PostgreSQL section is enabled by the same environment variable. `PYTHONPATH=<baseline>/src` and
`DATA_DIR=<shared-data>` run the same benchmark harness against PR #38 without changing its source.

With securely supplied credentials and a separately authorized spend cap, run `evals/run_eval.py --mode autonomous`
and `--mode assisted` independently. Evaluate the new question families with independent held-out gold SQL and human
narrative review; the current 98-case gold corpus is preserved, not expanded into a claimed language benchmark.
Cloud Run/GCS cold starts, CPU/RAM/concurrency configurations, remote PostgreSQL failover and production-scale memory
still need measurement. Keep single-instance deployment. Human review of the new analytical definitions is pending.
