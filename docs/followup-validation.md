# Validate brand follow-ups and preparation latency

A saved top-five brand result is already a selected cohort. Its category breakdown can use one governed
query with a five-brand filter and the original dates. It does not require discovering the top five again.
Chat history now includes the saved spec and complete small aggregate result (at most 20 rows and 12,000
characters per answer). Failed, unverified, raw-row and partial results are not supplied as complete cohorts.
The interpreter must preserve all selected brands and avoid applying the old ranking limit to category rows.

This does not add an arbitrary multi-step query planner. It supplies missing evidence for an already
supported follow-up. Model interpretation still needs a live regression after deployment.

## Evaluation

The `flow-brand-followup` case uses a fixed saved five-brand cohort from 16 October–14 November 2026.
Its gold answer independently recomputes revenue for every brand/category/subcategory combination in that
scope. The cohort is a saved selection, not a claim that those brands rank first on every dataset size or date.

Free checks:

```bash
DATA_SIZE=small uv run python evals/spec_eval.py --show flow-brand-followup
DATA_SIZE=small uv run python evals/run_eval.py --suite flow --dry-run
```

With the provider key configured privately in the environment, this command makes paid model calls:

```bash
DATA_SIZE=small uv run python evals/run_eval.py --suite flow --ids flow-brand-followup \
  --repeats 3 --max-cost-usd 1 --minimum-pass-rate 1 --yes
```

Also test the actual conversation in one chat: ask for the top five brands by revenue, orders and units
over the last 30 days, then request their category/subcategory revenue breakdown. Check that all five
selected brands and the original dates carry forward, with no new top-five limit on the breakdown.
Repeat with a second chat and check that its cohort does not leak into the first.

## Latency

Plain equality join profiling now materializes each side's grouped key counts once, using those counts
for both cardinality and row-weighted unmatched fractions. Joins with extra conditions keep their existing
measurement. No join checks are skipped. The profile cache invalidates when this code changes, so the first
request after deployment still prepares the data.

For the reported 71-second preparation, inspect the existing `agent_prepare` logs to separate DuckDB view
creation, join profiling and PostgreSQL checkpoint setup:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="pantree-insights" AND textPayload:"agent_prepare"' \
  --project=pantree-insights --freshness=1h --limit=30 \
  --format='table(timestamp,resource.labels.revision_name,textPayload)'
```

Compare cold and warm questions on the same revision and data snapshot. The local join benchmark is not
an end-to-end Cloud Run latency measurement. This change does not alter instance count, billing, model
timeouts or provider concurrency. Keeping an instance warm and moving initialization before readiness
can be evaluated separately once the slow preparation phase is known.
