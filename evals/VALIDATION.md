# Validate stakeholder analysis changes

Use three separate checks: deterministic regressions, retrieval coverage, and end-to-end live-model accuracy.
Passing the first two does not establish that live answers are correct. Keep DuckDB, the metric definitions,
and the hybrid retriever; assess changes against business outcomes before changing the architecture.

## 1. Free checks on the proposed commit

From the repository folder:

```sh
uv sync --locked
uv run pantree-data download --size small
DATA_SIZE=small uv run pytest -q
uv run ruff check .
npm install --no-save --package-lock=false playwright@1.62.1
npx playwright install chromium
node tests/frontend.cjs
DATA_SIZE=small uv run python evals/run_eval.py --suite all --dry-run
```

These tests use fake model responses. They exercise the real DuckDB execution, guards, orchestration,
verification, memory gating, and UI with synthetic API errors. A passing test suite is not a paid-model score.
The dry run checks each suite's independently written expected SQL without asking a model.

The six cases in `flow_regression.yaml` cover exact brand spelling, an unknown city, complete category
coverage, a history-dependent follow-up, multiple selected cities and shipment versus destination scope.
The original `flow-values` gold SQL incorrectly counted unshipped orders for a shipment question; its gold
now requires a shipment event, and `flow-destinations` separately checks all orders by destination. Preserve
old reports: scores using the old five-case suite are not directly comparable with this corrected suite.
These cases were written during these fixes:
report them as **regressions**, never as unseen holdout accuracy. The history case injects a fixed previous
exchange; it does not claim to measure arbitrary multi-turn conversations.

For a free retrieval-only report on your chosen data size:

```sh
DATA_SIZE=small uv run python evals/retrieval_eval.py
```

Check missing tables, metrics, joins and exact values, alongside precision and latency. Do not optimize only
for recall: irrelevant examples and definitions can confuse the SQL writer. This report tests initial
retrieval; end-to-end and graph tests exercise the refresh after understanding and clarification.

## 2. Configure live evaluation securely

Use your local terminal or Cloud Shell with the model provider key supplied through your environment or a
secret manager. `OPENAI_API_KEY` is required for the default model. Never paste it into a chat or commit it.
The model remains configurable through `MODEL_MAIN` and `MODEL_LIGHT`.

First print an estimate; this command makes no model calls:

```sh
DATA_SIZE=small uv run python evals/run_eval.py --suite flow
```

Run the small review-derived regression first:

```sh
DATA_SIZE=small DAILY_BUDGET_USD=5 uv run python evals/run_eval.py \
  --suite flow --repeats 1 --max-cost-usd 1 --minimum-pass-rate 1 --yes
```

Both the existing daily budget and the run budget apply. They stop starting calls once recorded spend
reaches the limit; an already executing provider call can exceed it. Estimates are approximate and depend
on the configured pricing, model, retries, and response sizes. An exhausted budget yields an incomplete
run, not a successful evaluation.

Live runs use temporary, per-question memory, seeded only with the business profile. Evaluation answers
cannot become production insights, close real Pulse gaps, or teach subsequent evaluation questions their
answers. The reports record the commit, dirty-tree flag, manifest, model, usage, latency and isolation mode.
For comparable before/after runs, use the same **immutable data snapshot**, model, suite, parameters and
question order; use separate checkouts for the base and proposed revisions. Avoid the nightly moving data
feed between the two runs. Do not infer that a quality change came from code if the data also changed.

## 3. Full live evaluation before rollout

Download or pin the production-sized snapshot, then print the full estimate:

```sh
uv run pantree-data download --size full
DATA_SIZE=full uv run python evals/run_eval.py --suite all --repeats 3
```

After reviewing the estimate and choosing a budget you accept, run it. For example, with a USD 10 run budget:

```sh
DATA_SIZE=full DAILY_BUDGET_USD=15 uv run python evals/run_eval.py \
  --suite all --repeats 3 --max-cost-usd 10 --minimum-pass-rate 0.90 --yes
```

A 90% gate is a suggested initial release target, not a measured result or a universal threshold. Also review:

- correct result tables and answer claims, scored separately;
- unsafe requests declined, valid requests not wrongly declined;
- necessary clarification and unnecessary questions;
- all groups, dates, comparison baselines, metric denominators and chart units;
- repeated-run consistency, query retries, cost and latency;
- provider errors, incomplete runs and failures that previously received a green verification badge.

Provider errors and incomplete runs fail the exit-code gate even if the accuracy table excludes provider
errors. Correct SQL with incorrect narrative claims no longer prints PASS. Reports are saved under
`evals/reports/`, or the directory selected by `--output-dir`. Keep the raw JSON and Markdown with your
review. Do not silently rerun only failures and present that as a fresh holdout score.

Collect 10–20 new questions from real stakeholders using `EXTERNAL_QUESTIONS.md`. Keep them out of prompts,
examples and tuning. Independently review their expected SQL before scoring the model. Include follow-ups,
ambiguous terms and questions requiring comparison rather than filtering out a baseline.

## 4. Merge, deploy, and smoke-test

After the PR's CI and live evaluation meet your agreed targets, merge and deploy the saved source revision.
Merging a PR alone does not deploy Cloud Run. Check the deployed revision and repeat: a recorded demo,
a live question, a correction/confirmation, switching chats, exports and a simulated interrupted request.

Use the existing `scripts/deploy_cloudrun.sh` with your configured Google Cloud account and secrets. This
validation change does not change production configuration or run a deployment automatically.

## Data refreshes and limitations

The agent/retriever now notices a changed published manifest and rebuilds indexes for new questions. A
paused interpretation tied to an old manifest must be restarted before SQL runs. Profile caches include
the snapshot signature. Publishers must update `manifest.json` after refreshing data, including same-day
corrections. For atomic refreshes, publish immutable/versioned directories and switch `DATA_DIR` to a finished
snapshot; a manifest guard cannot make files overwritten in place an atomic database snapshot.

Runtime recomputation verifies exact metric and dimension aliases. Unsupported aliases, derived comparison
groupings, custom metrics and ambiguous periods remain **unverified** instead of borrowing another numeric
column's values. Confirmed top/bottom limits are checked as rankings; an unrequested subset cannot pass.
Only independently verified, grounded, non-truncated insights enter durable business memory.
Review legacy insights saved before this change before treating them as trusted knowledge; this PR does
not delete or retroactively certify existing production memory.

## Question markers, rate limits and blank pages

The activity panel's waiting symbol means an answer/confirmation is needed; it is not a confidence score.
Answered steps are marked complete. Earlier retry entries remain an audit trail of a corrected SQL attempt.

API timeouts, HTTP 429/502/503/504 and dropped streams now leave the app shell and conversation available
with a recovery action. Reopen the chat after a timed-out POST before submitting again: the backend may
still have finished the original request. A dropped connection is not proof that the analysis failed.

A completely blank browser page can also originate before JavaScript starts (Cloud Run response, browser
failure or a crashed container). The screenshots do not prove which happened. Inspect Network/Console
and Cloud Run logs at the error time; do not diagnose a quota or memory failure from a white page alone.
For the default service, an operator with Google Cloud access can inspect:

```sh
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="pantree-insights" AND severity>=ERROR' \
  --project=pantree-insights --limit=30 --format=json
```

Review provider 429s, request duration, instance restarts and memory exhaustion separately. Client recovery
does not increase a provider quota or change Cloud Run capacity.
