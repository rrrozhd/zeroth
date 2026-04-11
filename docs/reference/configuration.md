# Configuration Reference

Every Zeroth setting is loaded from (in priority order): environment variables
(`ZEROTH_` prefix, nested via `__`), a local `.env` file, then `zeroth.yaml`.
This reference is auto-generated from `zeroth.core.config.settings` via
`scripts/dump_config.py` — **do not edit by hand**.

CI runs `python scripts/dump_config.py --check` on every PR and fails if this
file is stale.

## Database

Database backend configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_DATABASE__BACKEND` | `str` | `"sqlite"` |  |  |
| `ZEROTH_DATABASE__SQLITE_PATH` | `str` | `"zeroth.db"` |  |  |
| `ZEROTH_DATABASE__POSTGRES_DSN` | `SecretStr \| None` | `None` | ✓ |  |
| `ZEROTH_DATABASE__POSTGRES_POOL_MIN` | `int` | `2` |  |  |
| `ZEROTH_DATABASE__POSTGRES_POOL_MAX` | `int` | `10` |  |  |
| `ZEROTH_DATABASE__ENCRYPTION_KEY` | `SecretStr \| None` | `None` | ✓ |  |

## Redis

Redis connection settings, absorbing the existing RedisConfig fields.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_REDIS__MODE` | `str` | `"local"` |  |  |
| `ZEROTH_REDIS__HOST` | `str` | `"127.0.0.1"` |  |  |
| `ZEROTH_REDIS__PORT` | `int` | `6379` |  |  |
| `ZEROTH_REDIS__PASSWORD` | `SecretStr \| None` | `None` | ✓ |  |
| `ZEROTH_REDIS__KEY_PREFIX` | `str` | `"zeroth"` |  |  |
| `ZEROTH_REDIS__DB` | `int` | `0` |  |  |
| `ZEROTH_REDIS__TLS` | `bool` | `False` |  |  |

## Auth

Service authentication settings.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_AUTH__API_KEYS_JSON` | `str \| None` | `None` |  |  |
| `ZEROTH_AUTH__BEARER_JSON` | `str \| None` | `None` |  |  |

## Regulus

Regulus backend connection settings.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_REGULUS__ENABLED` | `bool` | `False` |  |  |
| `ZEROTH_REGULUS__BASE_URL` | `str` | `"http://localhost:8000/v1"` |  |  |
| `ZEROTH_REGULUS__API_KEY` | `SecretStr \| None` | `None` | ✓ |  |
| `ZEROTH_REGULUS__BUDGET_CACHE_TTL` | `int` | `30` |  |  |
| `ZEROTH_REGULUS__REQUEST_TIMEOUT` | `float` | `5.0` |  |  |

## Memory

Memory backend configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_MEMORY__DEFAULT_CONNECTOR` | `str` | `"ephemeral"` |  |  |
| `ZEROTH_MEMORY__REDIS_KV_PREFIX` | `str` | `"zeroth:mem:kv"` |  |  |
| `ZEROTH_MEMORY__REDIS_THREAD_PREFIX` | `str` | `"zeroth:mem:thread"` |  |  |

## Pgvector

Pgvector-based vector memory configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_PGVECTOR__ENABLED` | `bool` | `False` |  |  |
| `ZEROTH_PGVECTOR__TABLE_NAME` | `str` | `"zeroth_memory_vectors"` |  |  |
| `ZEROTH_PGVECTOR__EMBEDDING_MODEL` | `str` | `"text-embedding-3-small"` |  |  |
| `ZEROTH_PGVECTOR__EMBEDDING_DIMENSIONS` | `int` | `1536` |  |  |

## Chroma

ChromaDB vector memory configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_CHROMA__ENABLED` | `bool` | `False` |  |  |
| `ZEROTH_CHROMA__HOST` | `str` | `"localhost"` |  |  |
| `ZEROTH_CHROMA__PORT` | `int` | `8000` |  |  |
| `ZEROTH_CHROMA__COLLECTION_PREFIX` | `str` | `"zeroth_memory"` |  |  |

## Elasticsearch

Elasticsearch memory backend configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_ELASTICSEARCH__ENABLED` | `bool` | `False` |  |  |
| `ZEROTH_ELASTICSEARCH__HOSTS` | `list[str]` | `['http://localhost:9200']` |  |  |
| `ZEROTH_ELASTICSEARCH__INDEX_PREFIX` | `str` | `"zeroth_memory"` |  |  |

## Sandbox

Sandbox execution backend configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_SANDBOX__BACKEND` | `str` | `"local"` |  |  |
| `ZEROTH_SANDBOX__SIDECAR_URL` | `str` | `"http://sandbox-sidecar:8001"` |  |  |
| `ZEROTH_SANDBOX__DOCKER_CONTAINER_NAME` | `str` | `"zeroth-sandbox"` |  |  |
| `ZEROTH_SANDBOX__DOCKER_BINARY` | `str` | `"docker"` |  |  |

## Webhook

Webhook delivery system configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_WEBHOOK__ENABLED` | `bool` | `True` |  |  |
| `ZEROTH_WEBHOOK__DELIVERY_POLL_INTERVAL` | `float` | `2.0` |  |  |
| `ZEROTH_WEBHOOK__DELIVERY_TIMEOUT` | `float` | `10.0` |  |  |
| `ZEROTH_WEBHOOK__MAX_DELIVERY_CONCURRENCY` | `int` | `16` |  |  |
| `ZEROTH_WEBHOOK__DEFAULT_MAX_RETRIES` | `int` | `5` |  |  |
| `ZEROTH_WEBHOOK__RETRY_BASE_DELAY` | `float` | `1.0` |  |  |
| `ZEROTH_WEBHOOK__RETRY_MAX_DELAY` | `float` | `300.0` |  |  |

## Approval Sla

Approval SLA timeout and escalation configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_APPROVAL_SLA__ENABLED` | `bool` | `True` |  |  |
| `ZEROTH_APPROVAL_SLA__CHECKER_POLL_INTERVAL` | `float` | `10.0` |  |  |

## Dispatch

Dispatch and horizontal scaling configuration.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_DISPATCH__ARQ_ENABLED` | `bool` | `False` |  |  |
| `ZEROTH_DISPATCH__SHUTDOWN_TIMEOUT` | `float` | `30.0` |  |  |
| `ZEROTH_DISPATCH__POLL_INTERVAL` | `float` | `0.5` |  |  |

## Tls

TLS configuration for direct uvicorn SSL.

| Env Var | Type | Default | Secret | Description |
| --- | --- | --- | --- | --- |
| `ZEROTH_TLS__CERTFILE` | `str \| None` | `None` |  |  |
| `ZEROTH_TLS__KEYFILE` | `str \| None` | `None` |  |  |
