# Handoff prompt: finish Pantree Insights Agent (step 5)

Copy everything below into another AI coding tool, with this repository checked out.

---

You are finishing **pantree-insights-agent** (GitHub: Meetpanchal58/pantree-insights-agent), an AI data analyst
for Pantree, a simulated Indian fashion shop. Steps 1–4 are done, tested (160 pytest tests) and must keep working.
Your job is **step 5: test set + deployment on Google Cloud Run**, then the final README. Read `README.md`,
`semantic/metrics.yaml`, `src/pantree_insights/graph.py` and `src/pantree_insights/web.py` first.

## What exists (do not rewrite; extend)

- Python 3.11+, managed with **uv** (`uv sync`, `uv run ...`). Lint: `uv run ruff check .` (line length 120). Tests: `uv run pytest -q`
  (tests marked `data` need `uv run pantree-data download --size small`).
- **Data**: ecommerce-simulator shop from Hugging Face `Meet58/ecommerce-simulator` (`full` = ~470 MB, default; `small` = ~170 MB)
  downloaded to `data/<size>/` by `pantree-data download`. DuckDB views over Parquet, locked down after creation
  (`db.connect`): read-only, only the data folder, no extensions/settings changes.
- **semantic/**: `schema.yaml` (22 allowed tables; `crm_sends`, `purchase_orders`, `customer_pii`, `customer_addresses.address_line`,
  `support_contacts.issue_bucket_true` are blocked), `metrics.yaml` (32 metrics, validated against the simulator — "revenue" means
  realized revenue), `examples.yaml` (28 verified question→SQL pairs), `glossary.yaml`.
- **guardrails.py**: one read-only analytical SELECT, allowed tables/columns, no table functions, no SELECT *, aggregated or LIMIT ≤ 500;
  question checks (personal data, prompt injection); `unsupported_numbers()` checks numbers in answers against the result.
- **retrieval.py**: LlamaIndex hybrid (BM25 + local fastembed `BAAI/bge-small-en-v1.5`) over tables, metrics, examples, ~2,000 values.
  Embeddings cached in `.cache/embeddings.npz`; model in `.cache/models`.
- **agents.py + prompts/agents.yaml**: planner (proceed / clarify ≤3 questions with defaults / decline), SQL writer, validator, analyst.
- **graph.py**: LangGraph flow with retries (max 3), clarify interrupt/resume, number check, chart, memory; streams step events.
  `Agent.start(question, chat_id, history, use_defaults, memory)` / `Agent.resume(run_id, answers)`. CLI: `pantree-ask`.
- **llm.py**: LangChain `init_chat_model`; models from settings `MODEL_MAIN=openai:gpt-6.1-terra`, `MODEL_LIGHT=openai:gpt-6-luna`;
  cost tracking, `DAILY_BUDGET_USD`; `FakeLLM` for tests. Real runs: ~$0.02–0.04 and 30–50 s per question.
- **web.py + static/**: FastAPI app (`pantree-web`, port from `$PORT`, host from `$HOST`), SSE streaming, Google OAuth + email codes
  (`auth.py`), SQLite/Postgres via SQLAlchemy (`store.py`; tables auto-created), 4-level memory (chat, project, user, shared business),
  pulse (`pulse.py`, runs at startup), demo replays from `demo_runs/` (`pantree-demo record "..."`). Exports are made in the browser.
- Settings: `src/pantree_insights/config.py` (pydantic-settings, env vars or `.env`; see `.env.example`).
- `memory/` in git holds `kpi_definitions.yaml`, `business_profile.md`, empty `gaps/*.json`; `daily_kpis.parquet` and
  `latest_pulse.json` are rebuilt at startup and git-ignored.

## Constraints

- Budget: the OpenAI key has ~$7.7 left (prepaid, auto-reload OFF; separate OpenAI project `pantree-insights-demo` with a $5 limit).
  Never loop real LLM calls; use `FakeLLM` in tests. Eval runs must print their cost and ask before running.
- Never commit secrets. Never put keys in code, Dockerfile, or GitHub. Secrets live in Google Secret Manager.
- Keep the existing tests passing and ruff clean. Add tests for new code.

## Google Cloud (already set up by the owner)

- Project ID `pantree-insights`, region `us-central1`, gcloud installed on the owner's Windows PC (Command Prompt).
- APIs enabled: Cloud Run, Artifact Registry, Cloud Build, Secret Manager, Cloud Storage. Budget alert ₹100.
- Artifact Registry repo `pantree` (docker, us-central1) with cleanup policy keeping the latest 2 images.
- Bucket `gs://pantree-insights-data` (us-central1) — empty; the data goes here.
- Secrets already created: `openai-api-key`, `demo-access-code` (the access code is no longer needed: sign-in + daily limit replace it).

## Step 5 tasks (in order)

### 5.1 Test set and scoring (`evals/`)
- `evals/questions.yaml`: ~40–60 questions = the 28 in `semantic/examples.yaml` (gold SQL) + new ones covering inventory, pricing,
  reviews, support, customers, marketing, time windows, vague questions (should clarify), and must-decline questions
  (personal data, raw dumps, off-topic). Each: `id, question, gold_sql (optional), expect: answer|clarify|decline, difficulty, tags`.
- `evals/run_eval.py`: runs the agent with `use_defaults=True` (except clarify cases), compares results by **execution accuracy**
  (run gold SQL and agent SQL; equal if the key numeric values match within 1% after sorting, ignoring column names/order),
  checks expected behaviour (clarify/decline), records cost/latency/attempts/number-check status, writes
  `evals/reports/<date>.json` and a Markdown summary by difficulty and tag. Flags: `--limit N`, `--ids`, `--dry-run` (FakeLLM).
  Print the estimated cost and require `--yes` before real calls. Unit-test the comparison logic.

### 5.2 Dockerfile
- `python:3.12-slim`; install uv; `uv sync --frozen --no-dev`; copy `src/`, `semantic/`, `prompts/`, `memory/`, `demo_runs/`.
- Pre-download the embedding model at build time (`.cache/models`) and pre-build `.cache/embeddings.npz` if data is available,
  so cold starts are fast. Do NOT bake the data into the image.
- Run as non-root; `CMD ["uv","run","--no-sync","pantree-web"]` with `HOST=0.0.0.0`; Cloud Run sets `PORT`.
- `.dockerignore`: `.venv`, `data`, `.cache/app.db`, `.git`, tests, `*.parquet` under memory.

### 5.3 Data on Cloud Storage
- README command: `uv run pantree-data download --size full` then
  `gcloud storage cp -r data/full gs://pantree-insights-data/full`.
- Cloud Run mounts the bucket as a volume (Cloud Storage FUSE) at `/data`; set `DATA_DIR=/data`, `DATA_SIZE=full`.
  DuckDB only reads it. Check `db.connect` `allowed_directories` works with the mount path.

### 5.4 Database (Neon Postgres) and sign-in
- Owner creates a free Neon database; store its URL as secret `database-url` (`postgresql://...?sslmode=require`).
  `store.py` already converts `postgres://`/`postgresql://` to `postgresql+psycopg://`.
- Google sign-in: owner creates an OAuth client (Web application) in project `pantree-insights`:
  APIs & Services → OAuth consent screen (External, app name "Pantree Insights", scopes email/profile/openid) → Credentials →
  Create OAuth client ID → authorized redirect URI `https://<cloud-run-url>/auth/google/callback` (add `http://127.0.0.1:8000/auth/google/callback` for local).
  Secrets: `google-client-id`, `google-client-secret`. Also `session-secret` (random 48+ chars).
- Optional email codes: Gmail app password → secrets `smtp-username`, `smtp-password`, `smtp-from-email`; env `SMTP_HOST=smtp.gmail.com`.

### 5.5 Deploy script `scripts/deploy_cloudrun.sh` (+ Windows notes in README)
Build with Cloud Build into `us-central1-docker.pkg.dev/pantree-insights/pantree/app:<git-sha>`, then `gcloud run deploy pantree-insights` with:
`--region us-central1 --min-instances 0 --max-instances 1 --memory 2Gi --cpu 1 --concurrency 20 --timeout 300
--cpu-throttling (request-based billing) --allow-unauthenticated --execution-environment gen2
--add-volume name=data,type=cloud-storage,bucket=pantree-insights-data,readonly=true --add-volume-mount volume=data,mount-path=/data
--set-env-vars APP_ENV=production,DATA_DIR=/data,DATA_SIZE=full,PUBLIC_URL=<url>,ADMIN_EMAILS=["<owner email>"],DAILY_QUESTION_LIMIT=10,DAILY_BUDGET_USD=0.50
--set-secrets OPENAI_API_KEY=openai-api-key:latest,DATABASE_URL=database-url:latest,SESSION_SECRET=session-secret:latest,GOOGLE_CLIENT_ID=google-client-id:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest`.
Grant the Cloud Run service account `roles/secretmanager.secretAccessor` and `roles/storage.objectViewer` on the bucket.
**min-instances must stay 0 and max-instances 1** (free tier; the app keeps runs and clarify state in memory, so one instance only).
After first deploy, set `PUBLIC_URL` to the real URL and add the redirect URI to the OAuth client.
Note: `.cache/llm_spend.json` (daily budget) resets on restart — acceptable; optionally move spend tracking to the DB `usage` table.

### 5.6 Production checks
- `APP_ENV=production` requires `SESSION_SECRET` (already enforced) and makes cookies secure.
- `uvicorn` already uses `proxy_headers`/`forwarded_allow_ips="*"` so OAuth redirects use https.
- `/healthz` exists. Startup builds the retriever and runs the pulse in a background thread (~5–15 s).
- Verify: visitor sees demos; Google sign-in works; a live question streams; daily limit returns 429 after 10; admin unlimited.

### 5.7 Optional: CI deploy
`.github/workflows/deploy.yml` on push to main using Workload Identity Federation (no JSON keys) — only if the owner wants it.

### 5.8 Final README
Live demo link, screenshots (sign-in, answer with agent panel, clarify, demo replay, memory), architecture diagram (Mermaid),
eval scorecard table, cost/latency numbers, "how the numbers can be trusted" (metric definitions validated against the simulator,
guardrails, number check, leakage-free data), limitations (simulated data, single instance), and links to ecommerce-simulator and
pantree-recommendation-system.

## Known gotchas (learned while building)
- pandas 3 uses a `str` dtype, not `object` — check with `pd.api.types.is_string_dtype`.
- Plotly reads labels like "Diwali 2025" as dates unless the axis `type="category"`.
- FastAPI request models must be at module level (with `from __future__ import annotations`).
- sqlglot 30 uses `from_` (not `from`) in Select args.
- OpenAI reasoning models via LangChain: pass `reasoning={"effort": "low"}`; temperature is not supported.
- The planner works best when SQL stays simple (per-segment totals); the analyst derives % changes.
