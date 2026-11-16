# Pantree Insights Agent

**An AI data analyst for Pantree, a simulated Indian fashion shop.** Ask a business question in plain
English; a team of agents plans the analysis, writes and checks the SQL, and turns the result into charts
and findings. It remembers what it learns, tracks the shop's KPIs day by day and points out what changed.

Data: 22 months of shop history (catalogue, customers, browsing, orders, returns, marketing, stock,
support) from my [ecommerce-simulator](https://github.com/Meetpanchal58/ecommerce-simulator), refreshed
nightly. Sister project: [pantree-recommendation-system](https://github.com/Meetpanchal58/pantree-recommendation-system).

### [▶ Live demo](https://pantree-insights-um7bylhdra-uc.a.run.app/)

Hosted on Google Cloud Run (scales to zero, so the first visit after a quiet period takes about 30 seconds).
Visitors can replay recorded demo runs without signing in; sign in with Google for 10 live questions a day.

> **Accuracy:** 28 of 31 unseen business questions answered correctly in each of 3 repeated runs
> (90%, 95% CI 77-100%), scored against independently written SQL. Without retrieval, the semantic layer,
> planner and validator the same model gets 48%. [Results](#results)

![A demo answer: findings, chart and exports, with the agents' work in the right panel](docs/screenshots/demo-answer-chart.png)

## Screenshots

| Agents working, live | Answer with plan and SQL |
|---|---|
| ![The planner, SQL writer, safety checks and validator, step by step](docs/screenshots/agents-working.png) | ![Answer in a project, with the analysis plan and SQL in the agent panel](docs/screenshots/answer-plan-sql.png) |
| **Clarifying questions** | **Memory** |
| ![Vague questions get 2-3 clarifying choices with a recommended default](docs/screenshots/clarifying-questions.png) | ![What the analyst remembers: preferences, insights per project, shared business knowledge](docs/screenshots/memory.png) |

## Results

All numbers below come from runs in `evals/reports/` (full shop, `gpt-6.1-terra`, Nov 15 2026). Format:
mean [95% bootstrap CI over questions]; pass^k = correct in all k repeats. Method: [Evaluation](#evaluation).

**Holdout** (questions never used in examples or prompts; `evals/reports/e2e_holdout_full_r*`):

| Measure | Result |
|---|---|
| Correct result tables (dimension-aware, 1% tolerance) | **90% [77-100%]** (n=31, k=3, pass^3 90%) |
| Correct answers (result correct and every number claim holds on the gold result) | 90% [77-100%] (n=31, k=3) |
| SQL execution success | 97% (n=31) |
| Decline accuracy (personal data, off-topic, injection) | 5/5 |
| Adversarial declines (paraphrased injection and personal-data requests) | 11/11 |
| False declines (valid questions that sound sensitive) | 2/16 (12%) |
| Clarifies vague questions | 2/4 |
| Asks when it should not (clear questions) | 1/31 (3%) |
| Median latency · mean cost per question | 20-22 s · US$0.013-0.016 |

The same three questions failed in every repeat (below); the other 28 passed every time.

**Regression** (verified examples, each with its own example hidden; `e2e_regression_full_r0-0_*`):
31/38 correct result tables (82% [68-92%]), clarification 2/3, declines 3/3.

**Ablation** (what each part is worth; `evals/reports/ablation_2026-11-15_1445.md`, paired on question):

| Variant | Correct result tables | Difference vs full [95% CI] |
|---|---|---|
| baseline: full schema dump, no planner, no validator | 48% [32-68%] | **-42 points [-61, -23]** |
| + hybrid retrieval and narrowed context, no planner/validator | 81% [65-94%] | -10 points [-23, +3] |
| full | 90% [77-100%] (k=3) | - |

Not run (budget): semantic-layer-only, planner-only, validator-only, full without memory.

**Failure taxonomy** (holdout full run, `e2e_holdout_full_r0-0_*`): did not clarify 2, wrong metric value 2,
declined wrongly 2, SQL error 1, over-asked 1.

| Question | What went wrong |
|---|---|
| x01 revenue by delivery-address region | empty SQL (run 1), then declined by the planner as "personal data" (runs 2-3) |
| y16 late deliveries by region | counted distinct orders; the metric is defined per delivered line |
| z01 orders in December 2024 (before the data starts) | returned NULL with a coverage flag instead of 0 |
| xc1 / yc1 "How did we do last quarter?", "Are we growing?" | answered with defaults instead of asking |
| f04 / f11 delivery-region revenue, email-campaign revenue | declined by the planner |

**Grounding.** Numbers in answers are structured claims checked against the result table (all answers
in the holdout runs verified). The previous regex check accepted 100% of random percentages, 83% of random
ratios and 32% of random counts on an 8-row table; the claim check accepts 0.10-0.45%
(`evals/reports/grounding_2026-11-15_1318.md`).

**Retrieval** (no LLM; `evals/reports/retrieval_2026-11-15_1527.md`): all required tables, metrics, joins
and values retrieved for 31/31 holdout and 28/28 regression questions, median 60 ms.

**After the failure-analysis fixes** (one more run each, k=1; `e2e_holdout_full_r3-3_*`,
`e2e_regression_full_r1-1_*`). The fixes came from the failures above: glossary phrases that bring their
tables, a `last_click_revenue` metric, "a metric definition wins over the question's wording", aggregate
geography and campaigns are not personal data, top 10 by default, never return empty SQL.

| Measure | Before (k=3) | After (k=1) |
|---|---|---|
| Holdout correct result tables | 90% [77-100%] | **100%** (31/31) |
| Holdout correct answers | 90% | 97% (30/31; y14 reported a gap from a different period) |
| False declines | 2/16 | **0/16** |
| Adversarial + unsafe declines | 16/16 | 16/16 |
| Clarifies vague questions | 2/4 | 3/4 |
| Regression, same 30 questions | 23/30 | **21/30** (paired difference -10 points, 95% CI -26 to +6) |

Read with care: the three holdout questions that started passing (x01, y16, z01) informed the fixes and
are marked `tuned_on`, so the holdout gain shows the fixes work rather than that the system generalises.
On the regression suite two questions were fixed (m11, h05) and four that passed before failed (m08,
m12, h02, h10): the same question got different SQL in the two runs (a dropped column, "this year"
instead of all data, True/False instead of New/Repeat labels, events matched by id instead of date
window). Rules for the last three were added afterwards and are **not yet re-measured**. The regression
run stopped at the daily budget after 31 of 44 questions (one OpenAI error, not scored).


**Understanding architecture, measured Nov 16** (`e2e_holdout_full_r0-2_2026-11-16_1754`,
`e2e_holdout_no_understanding_r0-0_2026-11-16_1807`, `e2e_holdout_full_r0-0_2026-11-16_1821`,
`e2e_regression_full_r0-0_2026-11-16_1837`; rescored). Same day, same 31 holdout answer questions:

| Measure | Without understanding | With understanding (k=3) |
|---|---|---|
| Correct result tables | **31/31** | 85% [72-96%], pass^3 81% (paired -16 points [-29, -6]) |
| Clarifies vague questions | - | 4/4 (was 2/4) |
| False declines | - | 0/16 |
| Unsafe + adversarial declines | - | 10/16 (was 16/16) |
| Over-asks on clear questions | - | 3/31 |
| Regression (23 of 44, budget cap) | - | 70% |

The understanding step made clarification better and answers worse. The failures traced to two causes,
fixed afterwards and **not yet re-measured**: "warehouse" mapped to the warehouse city while the expected
answers use its ID (x05, x12, y15 wrong in every repeat), and unsafe requests were judged after the
question had been resolved (the understanding step now declines first). Prime/non-Prime answers failed on
label wording in both systems; the scorer now accepts readable true/false labels.

**Pulse** (150 injected shocks on the small shop, `evals/reports/pulse_backtest_2026-11-15_1303.md`):
robust z + Benjamini-Hochberg finds 43/150 vs 35/150 for the old mean/SD rule, but raises 2.77 vs 1.17
other alerts a week. Neither catches small changes well (+25%: 5/26 vs 3/26).

## The app

| Left | Middle | Right |
|---|---|---|
| Projects (with their own instructions and memory) and chats; Pulse; Memory | The conversation. Clarifying questions appear as clickable choices with a recommended default | The agents working, step by step (plan, SQL, checks), then the chart, findings, recommendations, result table and exports: **Copy, PNG, CSV, PPT, SQL** |

- **Sign-in:** Google, or a one-time code by email. Visitors who do not sign in can replay recorded demo runs
  (`demo_runs/`) at no cost; signed-in users get 10 live questions a day (admins: unlimited).
- **Memory in four levels:** the chat (follow-up questions work), the project (instructions + insights from
  its chats), the user (preferences, insights outside projects) and shared business knowledge (the business
  profile, the pulse's open gaps, and insights an admin chose to share).
- **Pulse:** this week's unusual KPI changes, each with an "Investigate" button that starts a chat.

## How it works

```mermaid
flowchart LR
    Q[Business question] --> IC[Input check]
    IC --> R[Hybrid retrieval<br/>tables, metrics, joins,<br/>values, examples]
    R --> U[Understanding<br/>subject, measures, mentions,<br/>cuts, period, open points]
    U --> RS[Resolve in code<br/>concept map, values, IDs,<br/>join path]
    RS -->|anything unclear| C[Clarifying questions<br/>round after round] --> U
    RS --> RB[Read-back<br/>'here is what I'll compute'<br/>confirm or correct] -->|correction| U
    RB -->|confirmed| P[Planner<br/>metrics, grain - or declines]
    P --> W[SQL writer]
    W --> G[SQL guardrails<br/>read-only, approved<br/>tables and joins]
    G --> DB[(DuckDB<br/>read-only views)]
    DB --> V[Validator]
    G -.->|error| W
    DB -.->|error / empty| W
    V -.->|wrong query| W
    DB --> RC[Result checks<br/>filters used, no repeated<br/>rows, rates 0-100%]
    RC --> NV[Number check<br/>metric recomputed from<br/>its definition]
    RC -.->|problem| W
    NV -.->|mismatch| W
    V --> A[Analyst<br/>findings, actions,<br/>caveats]
    A --> N[Claim check] --> CH[Chart] --> OUT[Answer + chart + SQL<br/>+ exports]
    S[(semantic/<br/>metrics, schema,<br/>examples)] -.-> R
    M[(Memory<br/>chat, project, user,<br/>business)] -.-> P
    A -.-> M
```

### Safety layers

| Layer | Role |
|---|---|
| Question check (regex) | A cheap first filter for obvious personal-data requests and prompt-injection phrasing. It can be fooled by paraphrases, so it is not relied on alone. |
| Planner | Declines personal-data requests, raw dumps and off-topic questions it recognises (judged by the model). |
| **SQL guardrails** (code) | **The real enforcement:** one read-only SELECT, approved tables and columns only (resolved against the schema), approved join paths (ON, WHERE, USING), no table functions or whole-row arguments, results summarised or limited; grouping by an identifier must be a ranked top-N. |
| **Locked database** | Blocked tables and columns do not exist in the views; the connection cannot read other files, write, install extensions or change settings. |
| Claim check | Every number in the answer is a structured claim (value, column, row, operation) recomputed from the result; free-typed numbers are rejected. |

## What is built

| Part | What it does |
|---|---|
| `semantic/schema.yaml` | The 22 tables the agent may query: grain, keys, column notes, join paths. Blocked tables and columns are listed with the reason. |
| `semantic/metrics.yaml` | **One definition per business number**: realized revenue, AOV, return rate, RTO rate, conversion, ROAS, CAC and 26 more (33 in total). The agent must use these, so "revenue" always means the same thing. |
| `semantic/examples.yaml` | 28 verified question → SQL pairs (easy to hard), used as examples and scored in the regression suite. |
| `semantic/library/` | **Query library**: 128 reference queries in 7 areas (sales, customers, funnel, marketing, inventory, delivery, support), each with other phrasings and the metrics it computes. Together with the examples they use every table, all 40 approved joins, all 33 metrics, every concept column and 25 SQL patterns (CTEs, top-N per group, cohorts, funnels, period comparisons, running totals, basket pairs, ...). Retrieval only, never scored; tests check each query passes the guardrails, runs, has no duplicate rows, computes the metrics it claims, and does not contain the answer to any scored question. `evals/library_coverage.py` reports coverage. |
| `semantic/concepts.yaml` | **What a word means depends on what the question is about.** 14 subjects (orders, sign-ups, browsing, ad spend, attribution, support, reviews, stock, ...) with their table and dates, and 33 concepts (city, region, device, channel, category, sale event, ...) mapped per subject to a column: "Mumbai" about orders is `sales.ship_city`, about sign-ups `customers.city`. Plus ID words ("order 12345" → `order_id = 'OD000012345'`). |
| `semantic/glossary.yaml` | Words people use: "cash on delivery" → `'COD'`, "shoes" → `'Footwear'`; terms such as GMV, RTO, ROAS, CAC; phrases that bring their tables ("delivery region" → `customer_addresses`). |
| `src/pantree_insights/db.py` | Downloads the shop and exposes it as DuckDB views. After setup the connection cannot read other files, write, install extensions or change settings. |
| `src/pantree_insights/guardrails.py` | Code checks (no LLM): one read-only SELECT, allowed tables, columns and joins only, no table functions, no `SELECT *` or whole-row arguments, results summarised or limited, identifier grain only as a ranked top-N; a cheap question pre-filter (explicit personal-data requests, prompt injection). |
| `src/pantree_insights/grounding.py` | Structured claims: the analyst writes `{c1}` placeholders and a claim per number; each is recomputed from the result (value, difference, ratio, share, change, sum, mean) and rendered in the text. |
| `src/pantree_insights/profile.py` | `pantree-profile`: facts measured from the data: ID formats (18 types), null shares, ranges, date coverage, and each approved join's cardinality and unmatched rows. Found that joining sales to promotions on the coupon code alone inflates coupon revenue by 7% (codes are re-issued), now a join rule the guardrail enforces. |
| `src/pantree_insights/joins.py` | Join paths built in code from the question's table to the tables it needs (bridge tables added), avoiding joins that repeat rows and flagging them when unavoidable. |
| `src/pantree_insights/understanding.py` | Turns the understanding agent's reading into exact filters, columns, time column and join path; asks the user when a word fits meanings in different groups ("Mumbai": a customer city, a seller's city, a warehouse or a brand) or a cut does not exist for the subject. |
| `src/pantree_insights/references.py`, `sqlinfo.py` | After the question is understood, picks the reference queries that use the **same tables, columns and metrics** (parsed with sqlglot), so a question worded unlike any reference still gets the right SQL pattern. |
| `src/pantree_insights/recompute.py` | **Numbers checked against the data.** Builds each metric's SQL from its `metrics.yaml` definition with code-built LEFT JOINs, independent of the LLM. Build time: 52 library queries (78 result columns) must match it row by row within 1% on the small and the full shop; the rest are marked pattern-only. Runtime: after the agent's query runs, each confirmed metric is recomputed for the confirmed scope; a mismatch goes back to the SQL writer with the recomputed numbers, a match marks the answer "Numbers verified against the data", and what cannot be recomputed is shown as "not independently verified". No false alarm on the 78 verified library columns. |
| `src/pantree_insights/checks.py` | Result checks in code, before the LLM validator: every filter the understanding resolved must be in the SQL on the right table ("sellers.city", not "customers.city"); no repeated rows; no empty columns; rates within 0-100%. Zero false alarms on the 156 reference queries; on the agent's SQL from earlier paid runs it flags 5 of 38 wrong answers and 1 of 207 correct ones. |
| `src/pantree_insights/retrieval.py` | Hybrid search (LlamaIndex: BM25 keywords + local `bge-small` embeddings, fused by reciprocal rank) over four indexes: tables, metrics, examples and ~2,000 real filter values. Returns the tables, join paths, metric definitions, exact values and similar examples a question needs, in ~60 ms. With each example held out, it finds every table the example's SQL needs for 28 of 28. |
| `prompts/agents.yaml` | The agents' instructions, editable without touching code. |
| `src/pantree_insights/agents.py` | Five agents, each one structured LLM call with a typed result: **understanding** (what exactly is asked, and every open point that would change the numbers: no fixed limit, asked round after round until nothing is unclear; a safety stop after 6 rounds; then the resolved reading is shown back in business words - measure definition, cut, period, filters - and nothing is computed until the user confirms or corrects it), **planner** (turns the understood question into an analysis plan, or declines), **SQL writer**, **validator** (does the SQL answer what was asked?) and **analyst** (headline, findings, recommendations, caveats, facts to remember). |
| `src/pantree_insights/graph.py` | The LangGraph flow: retries with the exact error when the guardrails, the database or the validator reject a query (max 3 attempts); pauses for clarifying questions and resumes with the answers; verifies every claim against the result (one rewrite, then a warning); picks a chart; saves the insight to memory only if it verified. Streams a progress event per step. |
| `src/pantree_insights/memory.py` | Saves verified answers as insights (facts with numbers, SQL and data date), finds related insights (up to 30 days old) and open gaps for new questions, and closes gaps an answer explains. |
| `src/pantree_insights/charts.py` | Picks the chart from the result's shape (line, bar, grouped bars, scatter or KPI cards) and builds Plotly JSON for the browser and exports. |
| `src/pantree_insights/web.py`, `static/` | FastAPI app and the HTML/CSS/JS front end (no build step): runs in background threads streamed over Server-Sent Events, clarifying-question pause and resume, daily limits, memory, pulse, demo replays. Exports are made in the browser (Plotly for PNG, PptxGenJS for PowerPoint). |
| `src/pantree_insights/store.py`, `auth.py` | Database (SQLite locally, Neon Postgres in production): users, projects, chats, messages, memory, sign-in codes, usage. Google OAuth and email codes with signed session cookies. |
| `src/pantree_insights/demos.py` | Records real runs and replays them with their original timing. |
| `src/pantree_insights/llm.py` | One interface to the model (LangChain `init_chat_model`, so OpenAI or Anthropic is a setting), cost per call and per question, and a daily budget stored in the database (survives restarts). Tests use a scripted fake model. |
| `src/pantree_insights/pulse.py` | Builds the daily history of 18 KPIs (143 series), compares the latest week with the 8 before it (robust z-score, Benjamini-Hochberg across series), explains changes caused by sale events and records the rest as open gaps in `memory/gaps/`. |

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
uv run pantree-context "Is cash on delivery causing more RTO in tier 3 cities?"   # what the agent would see
```

```bash
uv run pantree-ask "How did Diwali 2026 do compared with Diwali 2025?"   # needs OPENAI_API_KEY in .env
```

A typical question takes about 20 seconds and costs about US$0.013-0.016 (about Rs 1.1-1.4) with `gpt-6.1-terra` (median and mean of the evaluation runs).

```bash
uv run pantree-web                            # the app at http://127.0.0.1:8000 (sign-in codes print in the log)
```

```bash
uv run pantree-demo record "Why are footwear returns so high?"   # save a run visitors can replay
```

```bash
uv run pytest -q                              # guardrail tests + checks on the small shop
```

Settings come from environment variables or `.env` (see `.env.example`), e.g. `DATA_SIZE=small`.

## Deploy

Google Cloud Run (0 instances when idle, at most 1), Neon Postgres, Google sign-in and email codes, data
mounted from Cloud Storage: step-by-step in [docs/deploy.md](docs/deploy.md). Design decisions and their
known weaknesses: [DESIGN.md](DESIGN.md).

## Evaluation

| Suite | What | Size |
|---|---|---|
| **Regression** | `semantic/examples.yaml` (each question's own example hidden from retrieval) + `evals/questions.yaml` | 44 (38 answer, 3 clarify, 3 decline) |
| **Holdout** | `evals/holdout.yaml`: never in examples, prompts or the regression set (`tests/test_eval_leakage.py`). Each answer question has intent, required tables, metrics, joins, values, grain and independently written gold SQL. Questions whose failures informed a fix are marked `tuned_on` | 40 (31 answer, 4 clarify, 5 decline) |
| **Behaviour** | `evals/behaviour.yaml`: 16 valid questions that sound sensitive (false declines), 11 paraphrased attacks; plus every holdout answer question asked without defaults (over-asking) | 58 |
| **External holdout** | `evals/holdout_external.yaml`: questions written by other people from `evals/EXTERNAL_QUESTIONS.md` (no table names). Reported separately | 0 so far |

**Scoring** (`evals/scoring.py`, `tests/test_scoring.py`): agent rows are aligned with gold rows on the
business dimensions before metrics are compared (1% relative tolerance; a ratio as a percentage is
accepted). Missing, extra or duplicate rows, values on the wrong category and wrong denominators fail.
Column names and extra columns do not matter; a left-out descriptive column is accepted only if the other
keys still identify every row; computed labels match on their words; when the question names no size,
only the agent's top n rows are compared with a gold top n. **Correct answers** additionally re-check
every number claim of the answer against the gold result.

**Statistics** (`evals/stats.py`): 95% CI by bootstrap over questions (2,000 seeded resamples); pass^k;
variants compared with a paired bootstrap on (question, repeat).

```bash
uv run python evals/run_eval.py --dry-run                         # free: suites valid, 100 gold queries run
```

```bash
uv run python evals/retrieval_eval.py                             # free: retrieval recall/precision
```

```bash
uv run python evals/library_coverage.py --run                     # free: query-library coverage + run every query
```

```bash
uv run python evals/run_eval.py --suite holdout --repeats 3 --yes # paid: ~US$0.013-0.016 per question
```

```bash
uv run python evals/run_eval.py --suite holdout --variant baseline --sets answer --yes   # ablation
```

```bash
uv run python evals/ablation_report.py evals/reports/e2e_holdout_*.json                  # free
```

```bash
uv run python evals/run_eval.py --rescore evals/reports/<run>.json                       # free re-score
```

The runner prints the number of LLM calls and the estimated cost and asks for confirmation; it stops when
the daily LLM budget is reached and never scores a budget refusal or a provider error.

## Limitations

- The data is simulated (realistic, validated), not a real shop.
- **Small holdout, written by the author.** 31 answer questions: one question moves accuracy by about 3
  points (CI 77-100%). No external questions yet; 4 holdout and 2 behaviour questions informed fixes and
  are marked `tuned_on`, so the "clean" score excluding them is not used as a headline.
- **Run-to-run variation.** The same question can get different SQL on different runs (regression: 4
  questions passed in one run and failed in the next). One run per change is not enough to call a small
  difference; the paired CI above includes zero.
- **Same-model validator.** The validator is the same model family as the SQL writer, so it shares its
  blind spots; it catches some errors but is not an independent check.
- **Pulse raises false alerts** (about 2.8 a week on the backtest besides real changes) and misses most
  small changes; treat it as a prompt to look, not a detector.
- Answers are against one data snapshot; the simulator adds a day every night.
- **One instance by design.** Runs in progress and paused clarifying questions live in memory
  (LangGraph `InMemorySaver`), so a restart drops them; scaling out needs a Postgres checkpointer (DESIGN.md).

## Roadmap

1. ✅ Data layer, metric definitions, guardrails, KPI pulse
2. ✅ Retrieval: hybrid search (BM25 + local embeddings) over tables, metrics, examples and column values
3. ✅ Agents (planner, SQL writer, validator, analyst) as a LangGraph flow, with memory
4. ✅ FastAPI app: sign-in, chats and projects, live agent activity, charts, exports (Copy, PNG, CSV, PPT), 4-level memory, pulse, demo replays
5. ✅ Evals and deployment on Google Cloud Run (Dockerfile, `scripts/deploy_cloudrun.sh`, `evals/`)

## License

[MIT](LICENSE)
