# Evaluation

Only [reports/current](reports/current/README.md) is the published evidence set. Previous generated reports and benchmark snapshots were removed in PR #46. Evaluator code, question suites, reference SQL, and gold specifications remain so the results can be reproduced.

## What the numbers mean

- **Correct answer tables:** result tables matching reference SQL / all attempted answer cases, including failures and unresolved cases.
- **Verified = gold:** correct tables / completed answer cases carrying a verified label. This is a conditional rate, not overall accuracy.
- **Silent wrong:** a completed, verified-labelled answer whose table does not match the reference.
- **Correctly clarified:** expected-clarification cases that ask a real clarification, excluding confirmation prompts.
- **Correctly declined:** unsafe or unsupported cases that return a decline.
- **Clarifying questions:** prompts asking for missing information, including unanswered prompts; plan approvals are excluded.
- **Latency:** local per-question time, excluding initial preparation, browser/network time, and human response delays. p95 is the nearest-rank 95th percentile.
- **Cost:** reported token usage priced at configured rates. Uncertain calls retain reservations; the ledger total includes calls missing from terminal row costs.
- **95% intervals:** bootstrap whole questions; Wilson intervals avoid a misleading zero-width interval when every case passes or fails. Small samples do not establish broad reliability.
- **Free compiler match:** intended specifications compiled by code versus reference SQL. No model interprets the questions.

## Suites and scoring

| Inputs | Purpose |
| --- | --- |
| holdout.yaml, behaviour.yaml | Answers, clarification, declines, valid-but-sensitive requests, and attacks |
| regression.yaml, questions.yaml | Regression cases with reference SQL |
| coverage.yaml | Additional tables and columns |
| flow_regression.yaml | Follow-ups with supplied chat history |
| gold_specs.yaml | Intended structured plans; never supplied to the autonomous model |

The sample is fixed before inference: 100 questions, seed 20261122, proportional allocation by suite and category with at least one case per group. It includes 56 answer cases and 44 behavior cases. The suites are development benchmarks, not a claim of a newly unseen external test set.

Autonomous mode blindly confirms proposed plans. It never uses reference answers to correct the model. Real unresolved clarification remains incomplete. Assisted mode is a separate experiment and must not be mixed into this report.

The scorer aligns dimension keys before comparing every numeric reference column. Integer counts match exactly; default float tolerances are 1e-9 relative and 1e-6 absolute. Different tolerances and percentage scaling require explicit case annotations. Truncated results cannot pass. See [scoring.py](scoring.py) for top-N and descriptive-key rules.

## Reproduce the free checks

```bash
DATA_SIZE=small uv run python evals/spec_eval.py
DATA_SIZE=small uv run python evals/run_eval.py --suite all --dry-run
DATA_SIZE=small uv run python evals/baseline.py --repeats 3 --output-dir /tmp/pantree-free
DATA_SIZE=small uv run python evals/fastpath_baseline.py
DATA_SIZE=small uv run python evals/context_retrieval.py
```

## Reproduce the paid run

Configure OPENAI_API_KEY securely in the environment. Preview selection without model calls first:

```bash
DATA_SIZE=small uv run python scripts/run_fresh_eval.py --questions 100 --seed 20261122 --mode autonomous --max-cost-usd 4 --output-dir /tmp/pantree-new-eval
```

Then explicitly authorize inference:

```bash
DATA_SIZE=small uv run python scripts/run_fresh_eval.py --questions 100 --seed 20261122 --mode autonomous --max-cost-usd 4 --output-dir /tmp/pantree-new-eval --ledger /tmp/pantree-new-eval.db --yes
```

The command writes paid.json, paid-budget.json, and the canonical results.md. A fresh ledger and output directory are required; the command refuses to overwrite an existing paid run. It uses the production managed provider adapter, one attempt per call, and conservative input/output reservations. The ceiling is cumulative across the run, including UTC day changes and uncertain outcomes. Pricing depends on configured model rates. Budget exhaustion can leave fewer than 100 attempted cases; it never substitutes cheaper questions to improve the score.

The $4 allowance is at most four cents per selected question on average, not a prediction that all 100 will finish. Actual cost depends on token counts, cache hits, clarification, and explanation calls. The legacy run_eval.py paid CLI checks spend after calls; use the command above for this stricter bounded run.

## Build the results table

```bash
uv run python scripts/make_results.py evals/reports/current/paid.json --compiler evals/reports/current/compiler.json --budget evals/reports/current/paid-budget.json
```

Every failed attempt stays in the appropriate denominator. The summary lists up to three observed failure types with real examples. Rescoring is free but requires matching case and data fingerprints; it is not another model run.

These results use simulated data, shared process/OS/provider caches, and one question at a time. They do not measure narrative faithfulness, causal correctness, Cloud Run cold starts, or concurrent-user capacity.
