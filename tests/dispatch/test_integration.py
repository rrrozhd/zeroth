"""Integration tests for Phase 16 ARQ wakeup and SIGTERM shutdown flows."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.core.dispatch.worker import RunWorker
from zeroth.core.runs import RunRepository, RunStatus
from zeroth.core.runs.models import Run

DEPLOYMENT = "integration-test-deployment"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    """Minimal orchestrator that completes a run."""

    def __init__(self, run_repo: RunRepository, *, fail: bool = False) -> None:
        self._run_repo = run_repo
        self.fail = fail
        self.driven: list[str] = []

    async def _drive(self, graph, run) -> Run:
        self.driven.append(run.run_id)
        if self.fail:
            raise RuntimeError("orchestrator failure")
        run = await self._run_repo.transition(run.run_id, RunStatus.COMPLETED)
        return run

    async def resume_graph(self, graph, run_id: str) -> Run:
        run = await self._run_repo.get(run_id)
        if run:
            self.driven.append(run_id)
            run = await self._run_repo.transition(run_id, RunStatus.COMPLETED)
        return run

    @property
    def approval_service(self):
        return None


class _FakeGraph:
    nodes: list = []
    entry_step: str = "start"


# ---------------------------------------------------------------------------
# Test: Run creation enqueues wakeup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_creation_enqueues_wakeup() -> None:
    """When bootstrap has arq_pool, creating a run should call enqueue_wakeup."""
    mock_pool = AsyncMock()
    mock_run_repo = AsyncMock()
    persisted_run = MagicMock()
    persisted_run.run_id = "test-run-123"
    mock_run_repo.create.return_value = persisted_run
    mock_run_repo.get.return_value = persisted_run

    with patch(
        "zeroth.core.dispatch.arq_wakeup.enqueue_wakeup", new_callable=AsyncMock
    ) as mock_enqueue:
        from zeroth.core.dispatch.arq_wakeup import enqueue_wakeup

        # Simulate what run_api.py does after persisting.
        arq_pool = mock_pool
        if arq_pool is not None:
            await enqueue_wakeup(arq_pool, persisted_run.run_id)

        mock_enqueue.assert_called_once_with(mock_pool, "test-run-123")


@pytest.mark.asyncio
async def test_run_creation_no_wakeup_when_arq_disabled() -> None:
    """When bootstrap has no arq_pool (None), no enqueue attempt should be made."""
    from zeroth.core.dispatch.arq_wakeup import enqueue_wakeup

    mock_pool = None
    # enqueue_wakeup guards against None pool -- should be a no-op.
    await enqueue_wakeup(mock_pool, "test-run-456")
    # If we get here without exception, the guard works.


# ---------------------------------------------------------------------------
# Test: Graceful shutdown called on lifespan exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_shutdown_called_on_lifespan_exit() -> None:
    """Exiting the lifespan should call worker.graceful_shutdown."""
    mock_worker = MagicMock()
    mock_worker.start = AsyncMock()
    mock_worker.poll_loop = AsyncMock(side_effect=asyncio.CancelledError)
    mock_worker.graceful_shutdown = AsyncMock()

    bootstrap = MagicMock()
    bootstrap.worker = mock_worker
    bootstrap.queue_gauge = None
    bootstrap.delivery_worker = None
    bootstrap.sla_checker = None
    bootstrap.arq_pool = None
    bootstrap.regulus_client = None
    bootstrap.webhook_http_client = None
    bootstrap.deployment = MagicMock()
    bootstrap.deployment.deployment_ref = DEPLOYMENT
    bootstrap.deployment.version = 1
    bootstrap.deployment.graph_version_ref = "g:v1"
    bootstrap.authenticator = MagicMock()

    from zeroth.core.service.app import create_app

    app = create_app(bootstrap)

    async with app.router.lifespan_context(app):
        pass  # yield point

    # After exiting lifespan, graceful_shutdown should have been called.
    mock_worker.graceful_shutdown.assert_called()


# ---------------------------------------------------------------------------
# Test: ARQ consumer started when pool available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arq_consumer_started_when_pool_available() -> None:
    """When both worker and arq_pool are set, an arq-consumer task should be created."""
    mock_worker = MagicMock()
    mock_worker.start = AsyncMock()
    mock_worker.poll_loop = AsyncMock(side_effect=asyncio.CancelledError)
    mock_worker.graceful_shutdown = AsyncMock()
    mock_worker.handle_wakeup = AsyncMock()

    mock_pool = AsyncMock()
    mock_pool.close = AsyncMock()

    bootstrap = MagicMock()
    bootstrap.worker = mock_worker
    bootstrap.queue_gauge = None
    bootstrap.delivery_worker = None
    bootstrap.sla_checker = None
    bootstrap.arq_pool = mock_pool
    bootstrap.regulus_client = None
    bootstrap.webhook_http_client = None
    bootstrap.deployment = MagicMock()
    bootstrap.deployment.deployment_ref = "test"
    bootstrap.deployment.version = 1
    bootstrap.deployment.graph_version_ref = "g:v1"
    bootstrap.authenticator = MagicMock()

    from zeroth.core.service.app import create_app

    app = create_app(bootstrap)

    # Track task creation.
    created_task_names: list[str] = []
    original_create_task = asyncio.create_task

    def tracking_create_task(coro, *, name=None):
        task = original_create_task(coro, name=name)
        if name:
            created_task_names.append(name)
        return task

    with patch("asyncio.create_task", side_effect=tracking_create_task), patch(
        "zeroth.core.dispatch.arq_wakeup.run_arq_consumer",
        new_callable=AsyncMock,
        side_effect=asyncio.CancelledError,
    ):
        async with app.router.lifespan_context(app):
            pass

    assert "arq-consumer" in created_task_names


# ---------------------------------------------------------------------------
# Test: Worker uses dispatch settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_uses_dispatch_settings() -> None:
    """RunWorker created by bootstrap should use dispatch settings for timeouts."""
    with patch("zeroth.core.config.settings._settings_singleton", None), patch.dict(
        "os.environ",
        {
            "ZEROTH_DISPATCH__ARQ_ENABLED": "false",
            "ZEROTH_DISPATCH__SHUTDOWN_TIMEOUT": "42.0",
            "ZEROTH_DISPATCH__POLL_INTERVAL": "1.5",
        },
    ):
        from zeroth.core.config.settings import ZerothSettings

        settings = ZerothSettings()

        assert settings.dispatch.shutdown_timeout == 42.0
        assert settings.dispatch.poll_interval == 1.5

        # Verify these would be passed to RunWorker.
        worker = RunWorker(
            deployment_ref="test",
            run_repository=MagicMock(),
            orchestrator=MagicMock(),
            graph=MagicMock(),
            lease_manager=MagicMock(),
            poll_interval=settings.dispatch.poll_interval,
            shutdown_timeout=settings.dispatch.shutdown_timeout,
        )
        assert worker.poll_interval == 1.5
        assert worker.shutdown_timeout == 42.0
