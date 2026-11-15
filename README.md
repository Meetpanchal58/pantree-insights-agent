# Pantree Insights Agent

**An AI data analyst for Pantree, a simulated Indian fashion shop.** Ask a business question in plain
English; a team of agents plans the analysis, writes and checks the SQL, and turns the result into charts
and findings. It remembers what it learns, tracks the shop's KPIs day by day and points out what changed.

Data: 22 months of shop history (catalogue, customers, browsing, orders, returns, marketing, stock,
support) from my [ecommerce-simulator](https://github.com/Meetpanchal58/ecommerce-simulator), refreshed
nightly. Sister project: [pantree-recommendation-system](https://github.com/Meetpanchal58/pantree-recommendation-system).

> **Status: complete.** The app runs locally and is ready for Google Cloud Run
> ([deploy guide](#deploy-on-google-cloud-run)); evaluation methodology and verified results are in [Evaluation](#evaluation).

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
| `semantic/glossary.yaml` | Words people use: "cash on delivery" → `'COD'`, "shoes" → `'Footwear'`, and terms such as GMV, RTO, ROAS, CAC. |
| `src/pantree_insights/db.py` | Downloads the shop and exposes it as DuckDB views. After setup the connection cannot read other files, write, install extensions or change settings. |
| `src/pantree_insights/guardrails.py` | Code checks (no LLM): one read-only SELECT, allowed tables and columns only, no table functions, no `SELECT *`, results must be summarised or limited; question checks (personal data, prompt injection); and a check that every number in an answer comes from the query result. |
| `src/pantree_insights/retrieval.py` | Hybrid search (LlamaIndex: BM25 keywords + local `bge-small` embeddings, fused by reciprocal rank) over four indexes: tables, metrics, examples and ~2,000 real filter values. Returns the tables, join paths, metric definitions, exact values and similar examples a question needs, in ~60 ms. With each example held out, it finds every table the example's SQL needs for 28 of 28. |
| `prompts/agents.yaml` | The agents' instructions, editable without touching code. |
| `src/pantree_insights/agents.py` | Four agents, each one structured LLM call with a typed result: **planner** (turns the question into an analysis plan, or asks up to 3 clarifying questions with a recommended default, or declines), **SQL writer**, **validator** (does the SQL answer what was asked?) and **analyst** (headline, findings, recommendations, caveats, facts to remember). |
| `src/pantree_insights/graph.py` | The LangGraph flow: retries with the exact error when the guardrails, the database or the validator reject a query (max 3 attempts); pauses for clarifying questions and resumes with the answers; checks every number in the answer against the result; picks a chart; saves the insight to memory. Streams a progress event per step. |
| `src/pantree_insights/memory.py` | Saves each answer as an insight (facts with numbers, SQL and data date), finds related insights and open gaps for new questions, and closes gaps an answer explains. |
| `src/pantree_insights/charts.py` | Picks the chart from the result's shape (line, bar, grouped bars, scatter or KPI cards) and builds Plotly JSON for the browser and exports. |
| `src/pantree_insights/web.py`, `static/` | FastAPI app and the HTML/CSS/JS front end (no build step): runs in background threads streamed over Server-Sent Events, clarifying-question pause and resume, daily limits, memory, pulse, demo replays. Exports are made in the browser (Plotly for PNG, PptxGenJS for PowerPoint). |
| `src/pantree_insights/store.py`, `auth.py` | Database (SQLite locally, Neon Postgres in production): users, projects, chats, messages, memory, sign-in codes, usage. Google OAuth and email codes with signed session cookies. |
| `src/pantree_insights/demos.py` | Records real runs and replays them with their original timing. |
| `src/pantree_insights/llm.py` | One interface to the model (LangChain `init_chat_model`, so OpenAI or Anthropic is a setting), cost per call and per question, and a daily budget. Tests use a scripted fake model. |
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
uv run pantree-context "Is cash on delivery causing more RTO in tier 3 cities?"   # what the agent would see
```

```bash
uv run pantree-ask "How did Diwali 2026 do compared with Diwali 2025?"   # needs OPENAI_API_KEY in .env
```

A typical question takes 30-50 seconds and costs about Rs 2-3 (US$0.02-0.04) with `gpt-6.1-terra`.

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

## Deploy on Google Cloud Run

Everything below runs in **Google Cloud Shell** (the terminal button at the top right of the Google Cloud
console): it is already signed in and has `gcloud`, `git` and bash. Settings keep it inside the free tier:
**0 instances when idle, at most 1**, request-based billing, data mounted from Cloud Storage.

**1. Database (Neon Postgres, free).** Create a project at neon.tech, copy the connection string
(`postgresql://...?sslmode=require`) and store it as a secret (Cloud console → Secret Manager → Create secret,
name `database-url`). Tables are created automatically on first start.

**2. Google sign-in (optional but recommended).** Cloud console → APIs & Services → OAuth consent screen
(External; app name "Pantree Insights"; scopes `openid`, `email`, `profile`; publish the app) → Credentials →
Create credentials → OAuth client ID → Web application. Leave the redirect URI for now (step 5).
Store the ID and secret as secrets `google-client-id` and `google-client-secret`.
For email codes instead or as well: a Gmail app password, stored as `smtp-username` and `smtp-password`.

**3. Get the code and upload the data** (once, and again whenever you want fresher data):

```bash
git clone https://github.com/Meetpanchal58/pantree-insights-agent && cd pantree-insights-agent
```

```bash
bash scripts/upload_data.sh
```

**4. Deploy** (the OpenAI key is already in Secret Manager as `openai-api-key`):

```bash
ADMIN_EMAIL=you@gmail.com bash scripts/deploy_cloudrun.sh
```

The script creates a service account that can only read the secrets and the data bucket, builds the image
with Cloud Build, deploys, and prints the live URL.

**5. Finish Google sign-in:** add the printed redirect URI (`<URL>/auth/google/callback`) to the OAuth client's
"Authorized redirect URIs". Redeploy any time with the same command.

The first request after an idle period starts a new instance (about 20-40 seconds: indexes and the KPI pulse
are rebuilt). Live questions are limited to 10 per user per day and the app stops calling the model after
US$0.50 a day; the OpenAI project has its own monthly limit.

## Evaluation

Two suites, kept separate:

| Suite | What | Size |
|---|---|---|
| **Regression** | `semantic/examples.yaml` (each question's own example is hidden from retrieval during evaluation) + `evals/questions.yaml` | 44 questions (38 answer, 3 clarify, 3 decline) |
| **Holdout (unseen)** | `evals/holdout.yaml`: never in examples, prompts or the regression set (`tests/test_eval_leakage.py` enforces it). Each answer question is annotated with intent, required tables, metrics, joins, filter values, result grain, independently written gold SQL, key columns and metric columns | 20 questions (14 answer, 3 clarify, 3 decline) |

**Scoring** (`evals/scoring.py`, unit-tested in `tests/test_scoring.py`): the agent's rows are aligned with the
gold rows on the business dimensions (category, month, region, ...) before comparing metrics; missing, extra
or duplicate rows, values on the wrong category and wrong denominators fail. Numeric tolerance: 1% relative
(a ratio reported as a percentage is accepted). Column names and extra columns do not matter.

### Verified results (free, reproducible)

Retrieval, `uv run python evals/retrieval_eval.py` (full shop, report: `evals/reports/retrieval_2026-11-15_0802.md`):

| Suite | Tables recall / precision | Metrics recall / precision | Joins recall / precision | Filter values recall | All required context retrieved | Median latency |
|---|---|---|---|---|---|---|
| Holdout (14) | 96% / 28% | 100% / 25% | 83% / 25% | 100% | **13/14 (93%)** | 60 ms |
| Regression (28, leave-one-out) | 100% / 28% | - | 100% / 23% | - | **28/28 (100%)** | 60 ms |

The one holdout miss (x01, "region of the delivery address") is reported, not patched. Precision is low by
design: the agent receives up to 6 tables so a needed table is rarely missing; the plan then names the exact
source tables and join path. The table limit was tuned on the regression suite only.

Gold SQL: `uv run python evals/run_eval.py --dry-run` checks that all 52 gold queries run, pass the guardrails
and produce the annotated columns without duplicate keys (52/52).

### Pending: paid end-to-end evaluation

Not yet run with the current code. Earlier end-to-end scores (Nov 14) were withdrawn: they let retrieval show
the agent the evaluated question's own gold SQL and used a lenient comparison. To produce current numbers
(SQL execution success, dimension-aware accuracy, clarification and decline accuracy, grounded-number rate,
retry/validator recovery, median latency, cost per question):

```bash
uv run python evals/run_eval.py --suite holdout --yes        # 20 questions, about US$0.50
```

```bash
uv run python evals/run_eval.py --suite regression --yes     # 44 questions, about US$1.10
```

Reports are written to `evals/reports/e2e_<suite>_<date>.md` with failures listed question by question.

### Limitations

- The data is simulated (realistic, validated), not a real shop.
- The holdout is small (20 questions); treat percentages as indicative, not precise.
- Questions are answered against one data snapshot; the simulator adds a day every night.
- One Cloud Run instance; clarifying questions in progress are lost if it restarts.

## Roadmap

1. ✅ Data layer, metric definitions, guardrails, KPI pulse
2. ✅ Retrieval: hybrid search (BM25 + local embeddings) over tables, metrics, examples and column values
3. ✅ Agents (planner, SQL writer, validator, analyst) as a LangGraph flow, with memory
4. ✅ FastAPI app: sign-in, chats and projects, live agent activity, charts, exports (Copy, PNG, CSV, PPT), 4-level memory, pulse, demo replays
5. ✅ Evals and deployment on Google Cloud Run (Dockerfile, `scripts/deploy_cloudrun.sh`, `evals/`)

## License

[MIT](LICENSE)
