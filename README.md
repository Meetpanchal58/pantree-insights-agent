# Pantree Insights Agent

AI data analyst for a simulated Indian fashion retailer. A user asks a business question in plain English; Pantree resolves the intended metric, dimensions, filters and period, executes safe analytics SQL, verifies supported numbers against metric definitions, and returns findings, a chart and exports.

**Live app:** https://pantree-insights-um7bylhdra-uc.a.run.app/

## Why this is more than text-to-SQL

```text
question
  -> hybrid retrieval
  -> structured understanding + clarification/read-back
  -> semantic metric / concept / join resolution
  -> deterministic SQL compiler for supported standard metrics
     OR guarded LLM SQL fallback for complex analyses
  -> read-only SQL guardrails + locked DuckDB
  -> result checks + independent metric recomputation
  -> validator
  -> grounded findings + chart + memory
```

The LLM interprets business intent. Standard metric computation is increasingly deterministic: definitions live in `semantic/metrics.yaml`, joins are measured and constrained, and simple confirmed metric requests are compiled rather than freely generated.

## Reliability

- **Persistent runs:** production LangGraph checkpoints use Postgres. A restart or scale-to-zero no longer requires the conversation state to live only in process memory.
- **Rate-limit protection:** LLM-heavy runs are queued per instance (`MAX_CONCURRENT_RUNS=1` by default), reads use bounded transient retries, and paid POST requests are never automatically replayed.
- **Navigation safety:** chat requests and streams are view-scoped; switching chats/tabs cancels stale browser work while the server-side analysis continues.
- **SQL safety:** one read-only SELECT, approved tables/columns/joins, no raw dumps, locked DuckDB external access.
- **Number verification:** supported metrics are independently recomputed from their semantic definitions for the confirmed scope.
- **Feedback loop:** completed live answers can be rated; negative feedback captures the failure category/note for later regression review.
- **Observability:** request IDs plus per-run status, duration, attempts, cost and verification status are emitted to structured Cloud Run logs.

## Evaluation

Evaluation definitions and scoring code are versioned; generated run reports are not.

Historical evidence before this reliability PR:

| Variant / suite | Result |
|---|---:|
| baseline | 48% |
| retrieval only | 81% |
| full internal holdout | 90% (28/31, 3 repeated runs) |
| initial regression | 82% (31/38) |

Those numbers are historical, not a claim about this branch. The current revision must be re-run through the manual **Live model evaluation** workflow before a new accuracy claim is made.

External questions are collected verbatim with `evals/collect_external.py`; model-written questions are never labelled external. See [evaluation methodology](docs/EVALUATION.md) and [results summary](docs/RESULTS.md).

## Offline demos

The home page contains four model-free walkthroughs bundled with the frontend. They work without live API/model calls and show:

1. interpreted question,
2. analysis plan,
3. guardrail-validated SQL,
4. query result shape,
5. findings,
6. visualization and result table.

The brand demo first finds the five highest-revenue brands, then within each top brand finds the leading product in each of its five highest-revenue categories. This avoids presenting five globally dominant products as if they represented category breadth.

Rebuild from a downloaded small snapshot:

```bash
DATA_SIZE=small uv run python scripts/build_offline_demos.py
```

## Repository map

```text
src/pantree_insights/
  agents.py          structured LLM roles
  graph.py           LangGraph orchestration
  compiler.py        deterministic standard-metric SQL
  checkpoint.py      memory/Postgres checkpoint backend
  understanding.py   intent -> columns / filters / periods
  retrieval.py       BM25 + embedding retrieval
  joins.py            measured join graph
  guardrails.py       deterministic SQL safety
  recompute.py        independent metric recomputation
  grounding.py        numerical claim verification
  db.py               read-only DuckDB data layer
  store.py            users, chats, runs, feedback, memory
  web.py              FastAPI application
  static/             dependency-light web UI + offline demos

semantic/
  schema.yaml
  metrics.yaml
  concepts.yaml
  glossary.yaml
  examples.yaml
  library/

evals/
  run_eval.py
  scoring.py
  stats.py
  holdout.yaml
  holdout_external.yaml
  external_intake.yaml
  behaviour.yaml
  flow_regression.yaml

tests/
docs/
```

## Local setup

Python 3.11+ and `uv`.

```bash
uv sync --locked
uv run pantree-data download --size small
DATA_SIZE=small uv run pytest -q
uv run ruff check .
uv run pantree-web
```

Frontend check:

```bash
npm install --no-save --package-lock=false playwright@1.62.1
npx playwright install chromium
node tests/frontend.cjs
```

## Production

Google Cloud Run + Neon Postgres + Cloud Storage. The deploy script maps the same Postgres secret to application storage and durable LangGraph checkpoints.

```bash
ADMIN_EMAIL=you@example.com bash scripts/deploy_cloudrun.sh
```

See [deployment notes](docs/deploy.md) and [design decisions](DESIGN.md).

## Known limitations

- The data is simulated; business impact is not real-company impact.
- External holdout is intentionally empty until questions are supplied by people outside the project.
- Complex analyses still use LLM-written SQL after deterministic interpretation and guardrails.
- `MAX_CONCURRENT_RUNS=1` favors stability over throughput while provider quota is constrained.
- Pulse is an investigation aid rather than a high-recall anomaly detector.
