#!/usr/bin/env bash
# Build and deploy Pantree Insights to Cloud Run with safe, free-tier settings.
# Run in Google Cloud Shell from the repository folder:
#   ADMIN_EMAIL=you@gmail.com bash scripts/deploy_cloudrun.sh
#
# Required secrets in Secret Manager: openai-api-key, database-url (Neon). Created here if missing:
# session-secret. Optional: google-client-id + google-client-secret (Google sign-in),
# smtp-username + smtp-password (email codes via Gmail).
set -euo pipefail
PROJECT="${PROJECT:-pantree-insights}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-pantree-insights}"
BUCKET="${BUCKET:-pantree-insights-data}"
REPO="${REPO:-pantree}"
ADMIN_EMAIL="${ADMIN_EMAIL:?Set ADMIN_EMAIL=you@example.com}"
SA="pantree-run@${PROJECT}.iam.gserviceaccount.com"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/app:${TAG}"
gcloud config set project "$PROJECT" >/dev/null

has_secret() { gcloud secrets describe "$1" >/dev/null 2>&1; }
for s in openai-api-key database-url; do
  has_secret "$s" || { echo "Missing secret '$s'. Create it in Secret Manager first."; exit 1; }
done
if ! has_secret session-secret; then
  head -c 48 /dev/urandom | base64 | tr -d '\n=/+' | gcloud secrets create session-secret --data-file=-
fi

# Service account the app runs as: may read the secrets and the data bucket, nothing else.
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create pantree-run --display-name "Pantree Insights (Cloud Run)"
SECRETS="OPENAI_API_KEY=openai-api-key:latest,DATABASE_URL=database-url:latest,SESSION_SECRET=session-secret:latest"
NAMES="openai-api-key database-url session-secret"
if has_secret google-client-id && has_secret google-client-secret; then
  SECRETS+=",GOOGLE_CLIENT_ID=google-client-id:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest"
  NAMES+=" google-client-id google-client-secret"
fi
EMAIL_ENV=""
if has_secret smtp-username && has_secret smtp-password; then
  SECRETS+=",SMTP_USERNAME=smtp-username:latest,SMTP_PASSWORD=smtp-password:latest"
  NAMES+=" smtp-username smtp-password"
  EMAIL_ENV=",SMTP_HOST=smtp.gmail.com,SMTP_PORT=465"
fi
for s in $NAMES; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:$SA" \
    --role roles/secretmanager.secretAccessor >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$SA" \
  --role roles/storage.objectViewer >/dev/null

if gcloud artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "Image $IMAGE already built; skipping the build."
else
  echo "Building $IMAGE with Cloud Build ..."
  gcloud builds submit --tag "$IMAGE" --region "$REGION" .
fi

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)' 2>/dev/null || true)"
ENV_VARS="APP_ENV=production,DATA_DIR=/data,DATA_SIZE=full,ADMIN_EMAILS=[\"${ADMIN_EMAIL}\"],DAILY_QUESTION_LIMIT=10,DAILY_BUDGET_USD=0.50,CHECKPOINT_BACKEND=postgres,MAX_PARALLEL_LLM_CALLS=2${EMAIL_ENV}"
[ -n "$URL" ] && ENV_VARS+=",PUBLIC_URL=${URL}"

# Checkpoints are durable in the existing Neon Postgres. Active execution still runs
# in one instance; interrupted in-flight model calls are not replayed after a restart.
# Keep min-instances=0 for low idle cost; min=1 is an optional paid latency upgrade.
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --concurrency 20 \
  --cpu 1 --memory 2Gi --cpu-throttling --timeout 300 \
  --execution-environment gen2 --cpu-boost \
  --add-volume "name=data,type=cloud-storage,bucket=${BUCKET},readonly=true" \
  --add-volume-mount "volume=data,mount-path=/data" \
  --set-env-vars "^|^$(echo "$ENV_VARS" | tr ',' '|')" \
  --set-secrets "$SECRETS"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "PUBLIC_URL=${URL}" >/dev/null
echo
echo "Live at: $URL"
echo "Google sign-in redirect URI to add to the OAuth client: ${URL}/auth/google/callback"
