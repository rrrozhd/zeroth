from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from tests.conftest import requires_docker
from zeroth.core.retention.coordination import RetentionCoordinator, RetentionTransaction
from zeroth.core.retention.legal_hold_repository import LegalHoldRepository
from zeroth.core.retention.models import LegalHold
from zeroth.core.storage.async_postgres import AsyncPostgresDatabase
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.core.storage.database import AsyncDatabase, CoordinationTimeoutError


class _BlockingPlaceRepository(LegalHoldRepository):
    def __init__(
        self,
        database: AsyncDatabase,
        *,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(database)
        self._entered = entered
        self._release = release

    async def place_in_transaction(
        self,
        transaction: RetentionTransaction,
        *,
        run_id: str | None = None,
        reason: str | None = None,
        placed_by: str | None = None,
    ) -> LegalHold:
        self._entered.set()
        await self._release.wait()
        return await super().place_in_transaction(
            transaction,
            run_id=run_id,
            reason=reason,
            placed_by=placed_by,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_run_id", "second_run_id"),
    [("run-a", None), (None, "run-b")],
    ids=["run-specific-blocks-tenant-wide", "tenant-wide-blocks-run-specific"],
)
async def test_hold_placements_share_one_tenant_coordination_row(
    async_database: AsyncSQLiteDatabase,
    first_run_id: str | None,
    second_run_id: str | None,
) -> None:
    second_database = AsyncSQLiteDatabase(
        async_database.path,
        coordination_timeout_seconds=0.05,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    first_repository = _BlockingPlaceRepository(
        async_database,
        entered=entered,
        release=release,
    )
    second_repository = LegalHoldRepository(second_database)

    first_task = asyncio.create_task(first_repository.place("tenant-a", run_id=first_run_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        contender_attempted = asyncio.Event()

        async def place_contender() -> LegalHold:
            contender_attempted.set()
            return await second_repository.place("tenant-a", run_id=second_run_id)

        second_task = asyncio.create_task(place_contender())
        await asyncio.wait_for(contender_attempted.wait(), timeout=1.0)

        with pytest.raises(CoordinationTimeoutError, match="coordination lock"):
            await second_task
        assert not release.is_set()
    finally:
        release.set()
        first_hold = await first_task
    assert first_hold.tenant_id == "tenant-a"
    assert first_hold.run_id == first_run_id

    async with async_database.transaction() as connection:
        rows = await connection.fetch_all(
            "SELECT tenant_id FROM retention_coordination WHERE tenant_id = ?",
            ("tenant-a",),
        )
    assert rows == [{"tenant_id": "tenant-a"}]


@pytest.mark.postgres
@requires_docker
@pytest.mark.asyncio
async def test_postgres_hold_placements_share_one_tenant_coordination_row(
    postgres_database: AsyncDatabase,
    postgres_container: object,
) -> None:
    url = postgres_container.get_connection_url()  # type: ignore[attr-defined]
    dsn = url.replace("postgresql+psycopg2://", "postgresql://")
    contender_database = await AsyncPostgresDatabase.create(
        dsn,
        min_size=1,
        max_size=1,
        coordination_timeout_seconds=0.05,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    first_repository = _BlockingPlaceRepository(
        postgres_database,
        entered=entered,
        release=release,
    )
    second_repository = LegalHoldRepository(contender_database)
    tenant_id = "tenant-task4-postgres"

    first_task = asyncio.create_task(first_repository.place(tenant_id, run_id="run-postgres"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        contender_attempted = asyncio.Event()

        async def place_contender() -> LegalHold:
            contender_attempted.set()
            return await second_repository.place(tenant_id)

        second_task = asyncio.create_task(place_contender())
        await asyncio.wait_for(contender_attempted.wait(), timeout=1.0)

        with pytest.raises(CoordinationTimeoutError, match="coordination lock"):
            await second_task
        assert not release.is_set()
    finally:
        release.set()
        first_hold = await first_task
        await contender_database.close()
    assert first_hold.run_id == "run-postgres"


@pytest.mark.asyncio
async def test_connection_aware_hold_operations_share_caller_transaction(
    async_database: AsyncSQLiteDatabase,
) -> None:
    repository = LegalHoldRepository(async_database)
    coordinator = RetentionCoordinator(async_database)

    async with coordinator.transaction("tenant-a") as transaction:
        run_hold = await repository.place_in_transaction(
            transaction,
            run_id="run-a",
        )
        tenant_hold = await repository.place_in_transaction(transaction)
        holds = await repository.active_holds_for_tenant_in_transaction(transaction)
        released = await repository.release_in_transaction(transaction, run_hold.hold_id)

    assert holds.tenant_wide
    assert holds.run_ids == {"run-a"}
    assert released
    stored_run_hold = await repository.get(run_hold.hold_id)
    stored_tenant_hold = await repository.get(tenant_hold.hold_id)
    assert stored_run_hold is not None and not stored_run_hold.active
    assert stored_tenant_hold is not None and stored_tenant_hold.active


@pytest.mark.asyncio
async def test_retention_transaction_binds_tenant_identity(
    async_database: AsyncSQLiteDatabase,
) -> None:
    repository = LegalHoldRepository(async_database)
    tenant_b_hold = await repository.place("tenant-b", run_id="run-b")
    coordinator = RetentionCoordinator(async_database)

    async with coordinator.transaction("tenant-a") as transaction:
        with pytest.raises(FrozenInstanceError):
            transaction.tenant_id = "tenant-b"  # type: ignore[misc]
        with pytest.raises(TypeError):
            await repository.place_in_transaction(transaction, "tenant-b")  # type: ignore[call-arg]
        tenant_a_hold = await repository.place_in_transaction(transaction, run_id="run-a")
        tenant_a_holds = await repository.active_holds_for_tenant_in_transaction(transaction)
        released_foreign_hold = await repository.release_in_transaction(
            transaction,
            tenant_b_hold.hold_id,
        )

    assert tenant_a_hold.tenant_id == "tenant-a"
    assert tenant_a_holds.run_ids == {"run-a"}
    assert released_foreign_hold is False
    stored_tenant_b_hold = await repository.get(tenant_b_hold.hold_id)
    assert stored_tenant_b_hold is not None and stored_tenant_b_hold.active
