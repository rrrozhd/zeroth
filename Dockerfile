# Zeroth service image. Runs `zeroth-core serve`: migrations (SQLite or
# Postgres per ZEROTH_DATABASE__*) then uvicorn on :8000.
#
#   docker build -t zeroth-core .
#   docker run -p 8000:8000 \
#     -e ZEROTH_SERVICE_API_KEYS_JSON='[{"credential_id":"ops","secret":"<token>","subject":"ops","roles":["admin"]}]' \
#     -v zeroth-data:/data zeroth-core
#
# Seed a runnable demo deployment first (same volume):
#   docker run -v zeroth-data:/data zeroth-core zeroth-core seed-demo

FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim

# Optional extras baked into the image, e.g. --build-arg ZEROTH_EXTRAS=regulus
ARG ZEROTH_EXTRAS=""

RUN useradd --create-home --uid 10001 zeroth
COPY --from=build /dist/*.whl /tmp/
RUN WHEEL="$(ls /tmp/*.whl)" \
    && pip install --no-cache-dir "${WHEEL}${ZEROTH_EXTRAS:+[${ZEROTH_EXTRAS}]}" \
    && rm /tmp/*.whl

# Redis is disabled by default so the single-container image is
# self-contained and /health/ready reports healthy; override
# ZEROTH_REDIS__MODE (and ZEROTH_REDIS__HOST) when composing with Redis.
ENV ZEROTH_DATABASE__BACKEND=sqlite \
    ZEROTH_DATABASE__SQLITE_PATH=/data/zeroth.db \
    ZEROTH_REDIS__MODE=disabled \
    PORT=8000

RUN mkdir -p /data && chown zeroth:zeroth /data
VOLUME /data
USER zeroth
EXPOSE 8000

CMD ["zeroth-core", "serve"]
