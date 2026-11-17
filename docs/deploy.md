# Deploy on Google Cloud Run

Everything below runs in **Google Cloud Shell** (the terminal button at the top right of the Google Cloud
console): it is already signed in and has `gcloud`, `git` and bash. Settings favor a low-cost demo deployment: **0 instances when idle, at most 1**, request-based billing, data mounted from Cloud Storage. Analysis checkpoints and run metadata persist in Postgres, so browser navigation or a container restart does not rely only on process memory.

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

The first request after an idle period starts a new instance (about 20-40 seconds: indexes and the KPI pulse are rebuilt). Live questions are limited to 10 per user per day and the app stops calling the model after US$0.50 a day; the OpenAI project has its own quota. `MAX_CONCURRENT_RUNS=1` queues LLM-heavy analyses so multiple browser tabs do not burst the provider at once. The deploy script maps `database-url` to both `DATABASE_URL` and `CHECKPOINT_DATABASE_URL`.

## Keep the instance warm (optional, about free)

The service scales to zero, so the first visit after a quiet period waits 20-40 seconds. To avoid that during
usage hours, a Cloud Scheduler job can call `/healthz` every 10 minutes from 08:00 to 23:00 India time:

```bash
bash scripts/keep_warm.sh
```

With request-based billing an idle instance that is not a minimum instance is not charged, and Cloud
Scheduler includes 3 free jobs per billing account, so this costs about nothing. It is not a guarantee
(Cloud Run can still restart an instance; outside those hours the first visit is cold). The guaranteed option
is `--min-instances 1`, about 19 USD a month for 1 vCPU and 2 GiB at the idle rate.
