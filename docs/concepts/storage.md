# Storage

## What it is

The **storage** subsystem is the persistence layer shared by every other part of Zeroth that needs durable state: runs, threads, approvals, audit trail, contract registry, and more. It provides a small, consistent async database protocol (`AsyncDatabase` / `AsyncConnection`) with two concrete backends — SQLite for local/single-node deployments and Postgres for production — plus a Redis client for distributed runtime state and a set of JSON helpers for serialisation.

## Why it exists

Every subsystem that persists data needs the same primitives: connection lifecycle, transactions, schema migrations, JSON round-tripping, and the ability to run against both SQLite (for tests and single-node apps) and Postgres (for production). Re-implementing those primitives in each subsystem would mean five different transaction semantics and five different migration stories.

Storage solves this by making one `AsyncDatabase` protocol the *only* way the rest of Zeroth talks to a database. Subsystems call `await db.execute(...)` and `await db.fetchall(...)`; they never instantiate a raw connection. The concrete backend — SQLite today, Postgres tomorrow — is selected once at startup via `create_database(settings)` and passed around.

Postgres support is gated behind the `memory-pg` extra and imported lazily: a vanilla `pip install zeroth-core` does not require `psycopg` at import time, so lean deployments stay lean.

## Where it fits

Storage sits underneath almost everything. [Runs](runs.md) and threads persist through `RunRepository` / `ThreadRepository`, both of which take an `AsyncDatabase`. The [contracts](contracts.md) registry is an `AsyncDatabase` user. [Memory](memory.md) connectors that need SQL persistence use the same backends — `PgVectorMemoryConnector` rides on the same Postgres pool as `RunRepository`. Redis primitives (for dispatch, thread state, and distributed coordination) live alongside the SQL backends under the same package so that a single import location covers both worlds.

## Key types

All of these live under `zeroth.core.storage`:

- **`AsyncDatabase` / `AsyncConnection`** — the async database protocol everything else depends on.
- **`AsyncSQLiteDatabase`** — the always-available SQLite backend, with optional encryption via `EncryptedField`.
- **`AsyncPostgresDatabase`** — the Postgres backend, lazily imported (requires `memory-pg` extra).
- **`SQLiteDatabase` / `Migration`** — the lower-level synchronous SQLite database and its migration helper.
- **`create_database`** — factory that returns the right backend for the current `ZerothSettings`.
- **`GovernAIRedisRuntimeStores` / `RedisConfig` / `RedisDeploymentMode`** — Redis-backed runtime state shared across workers.
- **`build_governai_redis_runtime` / `docker_container_running`** — helpers for bootstrapping and detecting Redis in dev.

The `json` submodule (`to_json_value`, `from_json_value`, `load_typed_value`) is the canonical way to round-trip Pydantic models through database columns. Every repository in Zeroth uses it, which is why runs, threads, and contracts all persist cleanly across both SQLite and Postgres without bespoke serialisation code.

## Migrations

Storage owns the migration story too. Each repository runs its own Alembic migrations on first `initialize()` call, so bootstrapping a new database is a single call. In multi-worker deployments, only one worker should win the migration race — plan for a leader-election step or run migrations out-of-band in CI before starting workers.

## See also

- [Usage Guide: storage](../how-to/storage.md) — open a database, run a migration, query runs.
- [Concept: runs](runs.md) — the biggest consumer of storage.
- [Concept: memory](memory.md) — memory connectors persist via storage backends.
