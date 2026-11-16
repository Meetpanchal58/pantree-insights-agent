#!/usr/bin/env bash
# Keep the Cloud Run instance warm during usage hours, at almost no cost.
#
# Cloud Scheduler calls /healthz every 10 minutes from 08:00 to 23:00 India time. A recently used Cloud Run
# instance stays alive between calls, and with request-based billing an idle instance that is not a minimum
# instance is not charged; each call uses a few milliseconds. Cloud Scheduler includes 3 free jobs per
# billing account. Not a guarantee (Cloud Run can still restart an instance, and outside these hours the
# first visit is cold); min-instances 1 is the guaranteed option (about 19 USD a month for 1 vCPU / 2 GiB).
#
# Run once in Cloud Shell:   bash scripts/keep_warm.sh
# Remove it again:           gcloud scheduler jobs delete pantree-keep-warm --location "$REGION"
set -euo pipefail

PROJECT="${PROJECT:-pantree-insights}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-pantree-insights}"
SCHEDULE="${SCHEDULE:-*/10 8-22 * * *}"        # every 10 minutes, 08:00-22:59
TIME_ZONE="${TIME_ZONE:-Asia/Kolkata}"

gcloud config set project "$PROJECT" >/dev/null
gcloud services enable cloudscheduler.googleapis.com
URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"

if gcloud scheduler jobs describe pantree-keep-warm --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http pantree-keep-warm --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone "$TIME_ZONE" --uri "${URL}/healthz" --http-method GET
else
  gcloud scheduler jobs create http pantree-keep-warm --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone "$TIME_ZONE" --uri "${URL}/healthz" --http-method GET \
    --attempt-deadline 60s
fi
echo "Keep-warm job: ${URL}/healthz, '${SCHEDULE}' (${TIME_ZONE})"
