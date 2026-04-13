# Artifacts

*Added in v4.0*

The artifact store lets execution units and agents externalize large binary outputs (files, images, serialized models) so they travel outside the normal node output payload. Artifacts are stored on a pluggable backend (filesystem or Redis) and retrievable via the REST API.

## How It Works

When a node produces a large output, it can call the artifact store to externalize the data. The store returns an `ArtifactReference` containing a unique key that travels through the graph as a lightweight pointer. Downstream nodes or external clients retrieve the actual content by key. Artifacts support TTL-based expiration and prefix-based bulk cleanup for run-scoped lifecycle management.

## Key Components

- **`ArtifactStore`** -- Abstract base defining the store interface: `store()`, `retrieve()`, `delete()`, `list_by_prefix()`, and `cleanup_prefix()`.
- **`FilesystemArtifactStore`** -- Default store implementation writing artifacts to a configurable directory. Suitable for single-node deployments.
- **`RedisArtifactStore`** -- Redis-backed store for distributed deployments. Supports TTL-based expiration natively.
- **`ArtifactReference`** -- Lightweight pointer returned on externalization; carries the key, content type, size, and metadata.
- **`ArtifactStoreSettings`** -- Pydantic settings model configuring backend type, base path, TTL, and Redis connection parameters.

## REST API

- `GET /v1/artifacts/{artifact_id}` -- Retrieve an artifact by key. Returns raw binary content with the stored content type. Requires `run:read` permission. Returns 404 if not found, 503 if the artifact store is not configured.

## Configuration

The artifact store is configured via `ArtifactStoreSettings` and constructed during `bootstrap_service()`. If no store is configured, the REST endpoint returns 503. Settings include:

- `backend` -- `filesystem` or `redis`
- `base_path` -- Directory for filesystem backend
- `default_ttl` -- Default time-to-live for stored artifacts
- `redis_url` -- Connection URL for Redis backend

## Error Handling

- **`ArtifactNotFoundError`** -- Raised when retrieving a key that does not exist.
- **`ArtifactStorageError`** -- Raised on backend I/O failures (disk full, Redis unavailable).
- **`ArtifactTTLError`** -- Raised when TTL validation fails.

See the [API Reference](../reference/http-api.md) for endpoint details and the source code under `zeroth.core.artifacts` for implementation.
