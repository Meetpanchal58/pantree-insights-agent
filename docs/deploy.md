# Deploy on Google Cloud Run

Everything below runs in **Google Cloud Shell** (the terminal button at the top right of the Google Cloud
console): it is already signed in and has `gcloud`, `git` and bash. Default resource limits are:
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

The script creates a service account that can read the permitted secrets and data bucket, builds the image,
deploys, and prints the live URL. It sets `CHECKPOINT_BACKEND=postgres` for durable paused conversations,
using the existing Neon Postgres secret, and limits parallel provider calls to two per Cloud Run instance.
This does not increase the OpenAI organization's rate limits or prevent external 429 responses.

**Before deployment:** pass GitHub CI and the model evaluation for the exact commit. If the Cloud Run
home screen still displays "OR TRY" or older demos, it is running an older revision: a GitHub PR
is not itself a deploy. Verify the Cloud Run revision traffic after redeploying. A run interrupted
during an *active* model API request is reported as interrupted; only a paused checkpoint is resumable.

**Diagnosing 429s:** note the response `X-Request-ID` when present. Check Cloud Run logs
for `request_busy` and `run failed`, plus the provider's 429 and retry-after details.
Distinguish login/session API 429, daily user quota 429, provider RPM/TPM 429, and
Cloud Run upstream 429/503 before changing settings. One instance with 2 GiB and
`min-instances=0` can still cold-start. For low-latency usage, `--min-instances=1`
is an additional-cost option; do not infer an exact bill from previous examples.

**5. Finish Google sign-in:** add the printed redirect URI (`<URL>/auth/google/callback`) to the OAuth client's
"Authorized redirect URIs". Redeploy any time with the same command.

**6. Automatic deploys (optional, once).** A Cloud Build trigger can deploy every push to `main` with
`cloudbuild.yaml` (build → push → `gcloud run deploy --image`; secrets, data bucket and settings stay as the
script set them). In the Cloud console of this project:

- **Cloud Build → Permissions:** choose the Compute Engine default service account, turn on
  "Set as pre-selected for new triggers", and enable **Cloud Run Admin**, **Artifact Registry Writer**,
  **Logs Writer** and **Cloud Build Service Account**. When asked for the Cloud Run service account to
  impersonate, pick `pantree-run@<project>.iam.gserviceaccount.com`.
- **Cloud Build → Repositories → 1st gen → Connect repository:** GitHub (Cloud Build GitHub App), give the app
  access to this repository, connect it.
- **Cloud Build → Triggers → Create trigger:** name `deploy-main`, region `global`, event push to branch
  `^main$`, configuration "Cloud Build configuration file", location Repository, `/cloudbuild.yaml`.

Click **Run** on the trigger once to test it; builds show under Cloud Build → History. Use the deploy script
again when secrets, the data or service settings change.

The first request after an idle period starts a new instance. Publish measured join metadata with
`DATA_SIZE=full uv run pantree-profile --publish-joins` as part of the data upload (the upload script does this).
A matching artifact avoids join scans; a missing or stale artifact safely falls back to profiling.
KPI pulse work no longer runs at web startup: run `pantree-pulse` as a separate data-maintenance job and
publish its generated memory/kpis files with the deployment. The pulse page otherwise shows its last saved snapshot. Live questions are limited to 10 per user per day and the app stops calling the model after
US$0.50 a day; the OpenAI project has its own monthly limit.

## Keep the instance warm (optional)

The service scales to zero, so the first visit after a quiet period may wait for startup. To avoid that during
usage hours, a Cloud Scheduler job can call `/healthz` every 10 minutes from 08:00 to 23:00 India time:

```bash
bash scripts/keep_warm.sh
```

Warm-up requests do not guarantee that an instance stays alive. Cloud Run can still restart it.
Minimum instances reduce idle cold starts but incur charges; check current regional pricing and measure
the actual service before changing billing or resource settings.

## Cold starts

The first question after an instance starts loads the catalog, connects DuckDB and checks profile metadata.
Cloud Storage mount latency and missing profile artifacts can still increase preparation time. The question request returns at once and the app shows "Preparing the data"
as a step, so the browser never times out. With `--cpu-throttling` the start-up warm-up only gets CPU while a
request is open; `--no-cpu-throttling` (CPU always allocated) or `--min-instances 1` makes the first question fast
at a higher idle cost.


## Resource and concurrency controls

`MIN_INSTANCES`, `CPU`, `MEMORY` and `CONCURRENCY` can be set when invoking the deploy script; defaults
remain 0, 1, 2Gi and 20. Changing resources may increase costs. Measure on the deployed workload before tuning.
`MAX_PENDING_RUNS=32` bounds active, queued and paused requests in the process, and only 128 completed
run histories remain in RAM; completed histories are also stored in the database.

Keep `max-instances=1` and one Uvicorn worker. Durable checkpoints alone do not provide distributed job
leases, active-run recovery or cross-instance event delivery. Do not raise replica counts until those
protocols are implemented and load-tested. GCS-mounted Parquet is retained; no local-copy performance
claim has been made without testing the actual deployment.
