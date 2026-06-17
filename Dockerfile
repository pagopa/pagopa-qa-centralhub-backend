# syntax=docker/dockerfile:1.7

############################
# Stage 1 — build deps with uv
############################
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_INSTALL_DIR=/usr/local/bin

# Install uv via the official installer (avoids pulling a separate registry image,
# which can hit anonymous-pull 403/rate-limits on ghcr.io / Docker Hub).
ARG UV_VERSION=0.5.11
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/* \
 && uv --version

WORKDIR /app

# Install only dependencies first (better layer caching)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the source and install the project itself
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

############################
# Stage 2 — minimal runtime
############################
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root user (uid/gid 1000)
RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --home-dir /app --shell /sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

USER app

EXPOSE 8080

# Default command = API server. The Celery worker overrides this via `command:` in Helm values.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

