# ============================================================
# Stage 1: Builder -- install all dependencies via uv
# ============================================================
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# GovernAI requires git for its GitHub commit pin
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Copy dependency manifests first for layer caching
COPY pyproject.toml uv.lock ./

# Copy pre-built Regulus SDK wheel (local path dep won't resolve in Docker)
COPY docker/regulus-sdk/ /wheels/

# Install dependencies (without the project itself)
RUN uv sync --locked --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev

# Install the pre-built Regulus SDK wheel explicitly
RUN uv pip install /wheels/*.whl 2>/dev/null || true

# Copy source and install the project itself
COPY src/ src/
RUN uv sync --locked --no-dev || uv sync --no-dev

# ============================================================
# Stage 2: Runtime -- minimal image with only what's needed
# ============================================================
FROM python:3.12-slim

WORKDIR /app

# Non-root user for security
RUN useradd --create-home --uid 1001 zeroth
USER zeroth

# Copy virtual environment and source from builder
COPY --from=builder --chown=zeroth:zeroth /app/.venv /app/.venv
COPY --from=builder --chown=zeroth:zeroth /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Health check for Docker (uses the /health/live endpoint from Plan 17-01)
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

# Production entrypoint: runs migrations then starts uvicorn
CMD ["python", "-m", "zeroth.core.service.entrypoint"]
