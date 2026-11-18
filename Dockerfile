# Pantree Insights: FastAPI app for Google Cloud Run.
# The shop data is NOT in the image: Cloud Run mounts gs://<bucket> at /data (see scripts/deploy_cloudrun.sh).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    HOST=0.0.0.0 PORT=8080 APP_ENV=production DATA_DIR=/data DATA_SIZE=full

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies first (cached layer)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# App code and the files it reads
COPY src ./src
COPY semantic ./semantic
COPY prompts ./prompts
COPY memory ./memory
COPY demo_runs ./demo_runs
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "pantree-web"]
