# Pantree Insights Agent

**An AI data analyst for Pantree, a simulated Indian fashion shop.** Ask a business question in plain
English; a team of agents plans the analysis, writes and checks the SQL, and turns the result into charts
and findings. It remembers what it learns, tracks the shop's KPIs day by day and points out what changed.

Data: 22 months of shop history (catalogue, customers, browsing, orders, returns, marketing, stock,
support) from my [ecommerce-simulator](https://github.com/Meetpanchal58/ecommerce-simulator), refreshed
nightly. Sister project: [pantree-recommendation-system](https://github.com/Meetpanchal58/pantree-recommendation-system).

> **Status: step 1 of 5 done.** Data layer, business metric definitions, SQL guardrails and the KPI
> pulse are built and tested. Agents, UI and deployment come next (see [Roadmap](#roadmap)).

## How it will work

```
question
  → input check → planner (asks clarifying questions) → SQL writer → guardrails (code) → validator
  → DuckDB (read-only) → analyst → chart → number check → answer + chart + SQL + exports
                ▲                                  ▲
       semantic/ (tables, metrics, examples)   memory/ (insights, KPI history, open gaps)
```

## What is built

| Part | What it does |
|---|---|
| `semantic/schema.yaml` | The 22 tables the agent may query: grain, keys, column notes, join paths. Blocked tables and columns are listed with the reason. |
| `semantic/metrics.yaml` | **One definition per business number**: realized revenue, AOV, return rate, RTO rate, conversion, ROAS, CAC and 25 more (32 in total). The agent must use these, so "revenue" always means the same thing. |
| `semantic/examples.yaml` | 28 verified question → SQL pairs (easy to hard), used as examples and in evals. |
| `src/pantree_insights/db.py` | Downloads the shop and exposes it as DuckDB views. After setup the connection cannot read other files, write, install extensions or change settings. |
| `src/pantree_insights/guardrails.py` | Code checks (no LLM): one read-only SELECT, allowed tables and columns only, no table functions, no `SELECT *`, results must be summarised or limited; question checks (personal data, prompt injection); and a check that every number in an answer comes from the query result. |
| `src/pantree_insights/pulse.py` | Builds the daily history of 18 KPIs (143 series), compares the latest week with the 8 before it, explains changes caused by sale events and records the rest as open gaps in `memory/gaps/`. |

### Data the agent cannot see

| Hidden | Why |
|---|---|
| `customer_pii`, `customer_addresses.address_line` | Personal data |
| `support_contacts.issue_bucket_true` | Simulator ground truth a real shop would not have |
| `crm_sends` | Control-group rows have no send time, so CRM uplift cannot be measured |
| `purchase_orders` | Initial purchase orders are always recorded 2 days late |

## Quickstart

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run pantree-data download --size full      # ~470 MB; use --size small (~170 MB) for a quick start
```

```bash
uv run pantree-data info                      # table sizes
```

```bash
uv run pantree-pulse                          # KPI history + what changed this week
```

```bash
uv run pytest -q                              # guardrail tests + checks on the small shop
```

Settings come from environment variables or `.env` (see `.env.example`), e.g. `DATA_SIZE=small`.

## Roadmap

1. ✅ Data layer, metric definitions, guardrails, KPI pulse
2. Retrieval: hybrid search (BM25 + local embeddings) over tables, metrics, examples and column values
3. Agents (planner, SQL writer, validator, analyst) as a LangGraph flow, with memory
4. FastAPI app: chats and projects, live agent activity, charts, exports (Copy, PNG, CSV, PPT), demo replays
5. Evals and deployment on Google Cloud Run

## License

[MIT](LICENSE)
