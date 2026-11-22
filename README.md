# Pantree Insights Agent

An AI data analyst that answers business questions with verified numbers. The LLM never writes SQL.
A large language model (LLM) interprets the question; Python builds the query and checks its numeric claims.

[Open the live demo](https://pantree-insights-um7bylhdra-uc.a.run.app/) · [Dataset and AI settings](docs/ai-settings.md)

<!-- Demo recording placeholder: docs/demo.gif. Add an actual recording before embedding it. -->

| Example question | Answer excerpt | Local median time |
| --- | --- | --- |
| What was yesterday's GMV? | Gross sales: ₹75,757.00 | 0.199 s |
| What is the monthly return rate? | Latest observed month (2026-11): 18.01% | 0.406 s |
| Show revenue by category. | Highest: Apparel — ₹12,622,125.87 | 0.530 s |
| Which brands generated the highest sales? | Highest: Puma — ₹1,756,918.27 | 0.509 s |

Examples use the small snapshot through 2026-11-22. Times are three-pass local medians for model-free paths, excluding startup and human confirmation; they are not live-demo response times.

## Why it matters

- Generated SQL can run successfully while answering the wrong business question.
- Shared metric definitions and join checks catch unsupported calculations before execution.
- A visible plan and checked numeric evidence make the answer easier to inspect and challenge.

## How it works

```mermaid
flowchart LR
  Q[Question] --> I[Interpret to QuerySpec]
  I --> C[Clarify / Confirm]
  C --> S[Compile SQL from semantic layer]
  S --> D[Execute DuckDB]
  D --> V[Verify numbers]
  V --> E[Explain + chart]
```

A QuerySpec is a structured request describing measures, filters, groups, and dates.
The semantic layer is a collection of YAML files defining those business terms.
Every query needs confirmation. Follow-up questions retain the approved context.

## Results

The fresh autonomous run completed **100 questions**: 56 answer cases and 44 behavior cases.
Token-based cost: **$0.637558**, with **$0.00** in uncertain holds, below the $4 ceiling.

| Measure | Fresh result |
| --- | --- |
| Correct answer tables | 27/56 (48.2%) |
| Verified-labelled answers matching gold | 27/34 (79.4%) |
| Verified-labelled reference mismatches | 7 |
| All-case local latency, p50 / p95 | 6.851 / 27.283 seconds |
| Free compiler check | 98/98 cases; 294/294 matches across three passes |

**The evaluation release gate failed.** [Full results, confidence intervals, and failure analysis](evals/reports/current/results.md) retain failed and unresolved cases.
Some strict reference mismatches involve extra expected columns; the [failure audit](evals/reports/current/failure-audit.md) distinguishes them from interpretation errors without changing scores.
Simulated data, one repeat, and overlapping host work limit these measurements. Narrative faithfulness is not measured.
Only the [current evidence set](evals/reports/current/README.md) is published; earlier result artifacts were removed.

## Tech stack

Python · LangGraph · Pydantic · DuckDB · SQLAlchemy/PostgreSQL · FastAPI · JavaScript ES modules · Cloud Run

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). These three commands open the app and its model-free demos.

```bash
uv sync --locked
uv run pantree-data download --size small
DATA_SIZE=small uv run pantree-web
```

For live model questions, configure the provider securely as described in [the settings guide](docs/ai-settings.md).
Never commit a provider key. [Deployment guide](docs/deploy.md) · [Evaluation commands](evals/README.md)

## Limitations

- Data is simulated; results do not describe a real business.
- Verification checks numeric evidence, not every interpretation, narrative claim, or causal explanation.
- Investigations and retrieved history have explicit bounds; unsupported scope needs clarification or a new query.
- Active jobs run on one instance; distributed workers and a concurrent-user benchmark are not implemented.

[Architecture](docs/ARCHITECTURE.md) · [Design decisions](docs/DESIGN_DECISIONS.md) · [Implementation history](docs/history/README.md)

MIT license.
