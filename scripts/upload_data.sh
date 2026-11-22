#!/usr/bin/env bash
# Download the simulator shop and copy it to the Cloud Storage bucket that Cloud Run mounts at /data.
# Run in Google Cloud Shell from the repository folder:  bash scripts/upload_data.sh
set -euo pipefail
PROJECT="${PROJECT:-pantree-insights}"
BUCKET="${BUCKET:-pantree-insights-data}"
SIZE="${DATA_SIZE:-full}"

command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
uv sync --frozen --no-dev
uv run pantree-data download --size "$SIZE"
DATA_SIZE="$SIZE" uv run pantree-profile --publish-joins
gcloud storage rsync --recursive --delete-unmatched-destination-objects "data/$SIZE" "gs://$BUCKET/$SIZE" --project "$PROJECT"
echo "Uploaded data/$SIZE to gs://$BUCKET/$SIZE"
