# Stage 1: Build stage - with full build dependencies
FROM python:3.12-slim as builder

WORKDIR /app

# Install build dependencies (including ODBC libraries)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        unixodbc \
        unixodbc-dev \
        build-essential \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN pip install uv

# Build context: repo root on CI (SERVICE_DIR=.), or monorepo parent (SERVICE_DIR=ms-insights-portal-client, etc.)
ARG SERVICE_DIR=.

# Service package + vendored platform_core under src/
COPY ${SERVICE_DIR}/pyproject.toml ${SERVICE_DIR}/README.md ./
COPY ${SERVICE_DIR}/src/ /app/src

# Install project (setuptools discovers packages in src/ per pyproject.toml)
RUN uv pip install --system --no-cache-dir .

# Stage 2: Final stage - minimal production image
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        unixodbc \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app \
    && mkdir -p /tmp/uv-cache \
    && chown -R app:app /tmp/uv-cache /home/app /app

ENV HOME=/home/app \
    UV_CACHE_DIR=/tmp/uv-cache

USER app

COPY --chown=app:app --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --chown=app:app --from=builder /usr/local/bin /usr/local/bin

COPY --chown=app:app --from=builder /app/src /app/src

EXPOSE 8016

CMD ["uvicorn", "src.ms_crm_insights_portal.main:app", "--host", "0.0.0.0", "--port", "8016"]
