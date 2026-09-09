"""Real parent app + scheduler scan + temporary SQL store; no paid hosting."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane import config, main
from zeroth.econ.plane.common import bootstrap as econ_bootstrap
from zeroth.econ.plane.connectors import service as connectors
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning import scheduler, service
from zeroth.econ.plane.decisioning.models import ProbabilisticDecisionSchedule
from zeroth.econ.plane.decisioning.schemas import ProbabilisticDecisionScheduleCreate
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.config.settings import get_settings
from zeroth.platform.storage.scoping import TenantWideScopeContext
from zeroth.service.app import create_app
from zeroth.service.bootstrap.lifecycle import service_lifespan


@pytest.fixture
def mounted_store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'mounted.db'}")
    Base.metadata.create_all(engine)
    for name, value in {
        "jwt_secret": "test-mounted-scheduler-only",
        "cloud_entitlements_enabled": False,
        "cloud_scheduler_enabled": True,
        "cloud_scheduler_interval_seconds": 0.01,
        "workos_authkit_enabled": False,
        "paddle_billing_enabled": False,
        "report_email_enabled": False,
    }.items():
        monkeypatch.setattr(config.settings, name, value)
    monkeypatch.setattr(
        get_settings().auth, "allow_ephemeral_browser_session_secret_development", True
    )
    monkeypatch.setattr(econ_bootstrap, "bootstrap", lambda: None)
    monkeypatch.setattr(connectors, "init_otel_metrics", lambda: None)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(main.app.state, "cloud_scheduler_task", None, raising=False)
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        created = service.create_probabilistic_decision_schedule(
            db,
            ProbabilisticDecisionScheduleCreate.model_validate(
                {
                    "evidence_source": {
                        "workload": "w",
                        "incumbent_model": "a",
                        "candidate_model": "b",
                    },
                    "policy": {},
                    "simulations": 100,
                }
            ),
            created_by="audit-test",
            now=datetime.now(UTC),
        )
    yield engine, created.schedule_id
    engine.dispose()


def parent(worker=None):
    return create_app(
        SimpleNamespace(regulus_client=SimpleNamespace(stop=lambda: None), worker=worker)
    )


def last_run(store):
    engine, schedule_id = store
    with Session(engine) as db:
        return db.get(ProbabilisticDecisionSchedule, schedule_id).last_run_at


def test_self_hosted_enabled_configuration_does_not_require_cloud_plan(mounted_store):
    config.validate_startup_settings()


@pytest.mark.asyncio
async def test_real_mounted_parent_executes_due_schedule_then_drains(mounted_store):
    app = parent()
    async with service_lifespan(app):

        async def observed_run():
            while last_run(mounted_store) is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(observed_run(), timeout=3)
        task = main.app.state.cloud_scheduler_task
        assert task is not None and not task.done()
    assert task.done()


@pytest.mark.asyncio
async def test_disabled_parent_creates_no_worker(mounted_store, monkeypatch):
    monkeypatch.setattr(config.settings, "cloud_scheduler_enabled", False)
    async with service_lifespan(parent()):
        assert getattr(main.app.state, "cloud_scheduler_task", None) is None
        assert last_run(mounted_store) is None


@pytest.mark.asyncio
async def test_later_parent_startup_failure_drains_scheduler(mounted_store):
    tasks = []

    class FailingWorker:
        async def start(self):
            tasks.append(getattr(main.app.state, "cloud_scheduler_task", None))
            raise RuntimeError("injected later startup failure")

    with pytest.raises(RuntimeError, match="injected later startup failure"):
        async with service_lifespan(parent(FailingWorker())):
            pytest.fail("startup must fail")
    assert tasks and tasks[0] is not None, "scheduler must have started before the later failure"
    assert tasks[0].done(), "startup failure must not leave an orphan scheduler"


@pytest.mark.asyncio
async def test_duplicate_parent_cannot_own_or_stop_first_scheduler(mounted_store):
    async with service_lifespan(parent()):
        first = getattr(main.app.state, "cloud_scheduler_task", None)
        assert first is not None and not first.done()
        with pytest.raises(RuntimeError):
            async with service_lifespan(parent()):
                pytest.fail("duplicate owner must be rejected")
        assert not first.done(), "rejected owner must not stop the original worker"
    assert first.done()


@pytest.mark.asyncio
async def test_standalone_self_hosted_scheduler_executes_and_drains(mounted_store):
    main.startup()
    await main.start_cloud_scheduler()
    task = main.app.state.cloud_scheduler_task
    try:

        async def observed_run():
            while last_run(mounted_store) is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(observed_run(), timeout=3)
        assert not task.done()
    finally:
        await main.stop_cloud_scheduler()
    assert task.done()


@pytest.mark.asyncio
async def test_parent_bootstraps_economics_once(mounted_store, monkeypatch):
    calls = []
    monkeypatch.setattr(econ_bootstrap, "bootstrap", lambda: calls.append("bootstrap"))
    async with service_lifespan(parent()):
        assert calls == ["bootstrap"]


@pytest.mark.asyncio
async def test_scheduler_stops_before_runtime_resources(mounted_store):
    order = []

    class Worker:
        scheduler_task = None
        stop = asyncio.Event()

        async def start(self):
            return None

        async def poll_loop(self):
            await self.stop.wait()

        async def graceful_shutdown(self):
            order.append("worker")
            assert self.scheduler_task is not None and self.scheduler_task.done()
            self.stop.set()

    worker = Worker()
    async with service_lifespan(parent(worker)):
        worker.scheduler_task = main.app.state.cloud_scheduler_task
        assert worker.scheduler_task is not None and not worker.scheduler_task.done()
    assert order == ["worker"]
