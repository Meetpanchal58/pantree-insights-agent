# Design decisions

## 1. Code enforces data safety

Every generated query is parsed with sqlglot before DuckDB sees it: one read-only SELECT, approved tables/columns/joins, no table functions or whole-row access, and bounded identifier-grain results. The DuckDB connection also disables external access and exposes only approved views.

The model can still misunderstand a valid business question; safety and semantic correctness are separate problems.

## 2. Interpret intent before generating SQL

Question understanding resolves the subject, metric, dimensions, filters, IDs and time meaning. Ambiguity is returned to the user, then the resolved interpretation is read back before execution.

Known tradeoff: clarification adds latency, but silently choosing the wrong meaning is worse for analytics.

## 3. Compile standard metrics; generate only when necessary

For one supported metric with a confirmed scope, `compiler.py` builds SQL deterministically from `metrics.yaml` and the measured join graph. Complex comparisons, cohorts, funnels and custom analyses fall back to the guarded LLM SQL writer.

This reduces SQL variance without pretending every business analysis can be represented by a fixed query builder.

## 4. Measure joins instead of trusting names

The profile measures cardinality and unmatched rows for approved joins. Join paths are built in code and additional rules are enforced when a key alone is unsafe. This previously exposed a coupon-code join that inflated revenue because codes were re-issued.

## 5. Verify numbers independently

Supported metrics are recomputed from semantic definitions for the confirmed scope and compared with the agent result. Narrative numbers are structured claims that must also match the result table.

A verified number does not prove the business interpretation was correct, so validation and external evaluation remain necessary.

## 6. Persist analysis state

Production uses a Postgres LangGraph checkpointer plus an `analysis_runs` table. Browser navigation can detach from an analysis, and a later request can reconnect to persisted events/checkpoints.

Development and tests use the in-memory saver when `CHECKPOINT_DATABASE_URL` is empty.

## 7. Queue LLM-heavy work while quota is constrained

The web process allows a bounded number of simultaneous analyses (`MAX_CONCURRENT_RUNS=1` by default). Additional questions are visibly queued. Read requests may retry transient 429/502/503/504 responses; paid POST requests are never automatically replayed.

The deployment intentionally stays at one Cloud Run instance until provider quota justifies broader concurrency.

## 8. Treat evaluation data as source; reports as artifacts

Holdout/regression/behaviour definitions and scoring code are versioned. Generated JSON/Markdown reports are uploaded as workflow artifacts rather than committed. External questions must be authored outside the project and independently reviewed before scoring.

## 9. Production feedback becomes regression evidence

Users can rate completed answers. Negative feedback stores the run ID, failure category and note. Feedback is evidence for review; it is not automatically inserted into prompts or labelled as an unseen holdout.

## 10. Memory is gated

Only verified, non-disputed answers become durable analytical memory, and stale insights age out relative to the data date. A grounded answer can still be semantically wrong, so saved memory is intentionally conservative.
