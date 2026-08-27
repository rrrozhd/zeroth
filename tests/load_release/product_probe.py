"""Real product/backend observation collector for the ZER-33 release gate."""

from __future__ import annotations

from tests.load_release.fault_probe import collect_fault_observations
from tests.load_release.workload_probe import collect_workload, provision_scopes
from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase
from zeroth.service.bootstrap import run_migrations


async def collect_product_observations(
    profiles: dict, *, postgres_dsn: str, redis_url: str
) -> list[dict]:
    """Run the real product/backend probes and return raw request evidence."""
    run_migrations(postgres_dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    database = await AsyncPostgresDatabase.create(postgres_dsn, min_size=1, max_size=48)
    try:
        scopes = await provision_scopes(database, profiles["surfaces"])
        rows = await collect_workload(scopes, profiles["profiles"])
        anchors = {scope.surface: (scope.service, scope.auth, scope.secrets) for scope in scopes}
        rows.extend(
            await collect_fault_observations(
                database,
                anchors,
                postgres_dsn=postgres_dsn,
                redis_url=redis_url,
            )
        )
        return rows
    finally:
        await database.close()
