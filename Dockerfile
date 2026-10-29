# Multi-stage build: wheels are compiled once in the builder and copied into a
# slim runtime image that carries no compiler and no build cache.

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Dependency metadata only, so this layer is cached until pyproject.toml
# actually changes - application edits do not trigger a reinstall.
COPY pyproject.toml README.md ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && mkdir -p app \
    && touch app/__init__.py \
    && /opt/venv/bin/pip install .

COPY app ./app
RUN /opt/venv/bin/pip install --no-deps .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="WhereToNext" \
      org.opencontainers.image.description="AI travel itinerary planner with budget reconciliation and PDF export" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    WTN_ENVIRONMENT=prod \
    WTN_DATABASE_URL=sqlite+aiosqlite:////data/wheretonext.db

# curl is needed by the HEALTHCHECK below; nothing else is installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user and give it a writable volume for the database.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

COPY --from=builder /opt/venv /opt/venv
WORKDIR /srv
COPY --chown=appuser:appuser app ./app

USER appuser
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
