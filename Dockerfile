# Dockerfile
# ──────────────────────────────────────────────
# Single image used by three services:
#   app            — uvicorn / FastAPI
#   celery_worker  — celery worker
#   celery_beat    — celery beat scheduler
#
# Build:  docker compose build
# ──────────────────────────────────────────────

FROM python:3.12-slim

# uv for fast dependency installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (layer-cached, only re-runs when pyproject changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

# logs/ and logs/archive/ will be created at runtime by the app.
# Mount a volume over /app/logs in docker-compose so logs persist across restarts.

# Default command — overridden per-service in docker-compose.yml
CMD ["uv", "run","main.py"]