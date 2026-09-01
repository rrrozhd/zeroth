import asyncio
import logging
import random
import time

from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from starlette.requests import Request

from zeroth.econ.plane.auth.api import router as auth_router
from zeroth.econ.plane.auth.deps import get_current_scoped_db, require_roles
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.backtesting.api import router as backtesting_router
from zeroth.econ.plane.capabilities.api import router as capabilities_router
from zeroth.econ.plane.cloud.api import router as cloud_router
from zeroth.econ.plane.cloud.authkit import router as cloud_authkit_router
from zeroth.econ.plane.cloud.keys_api import router as cloud_keys_router
from zeroth.econ.plane.cloud.paddle import router as paddle_router
from zeroth.econ.plane.cloud.web import router as cloud_web_router
from zeroth.econ.plane.common import bootstrap as common_bootstrap
from zeroth.econ.plane.config import settings, validate_startup_settings
from zeroth.econ.plane.connectors.api import router as connectors_router
from zeroth.econ.plane.connectors.service import init_otel_metrics, render_prometheus_metrics
from zeroth.econ.plane.counterfactual.api import router as counterfactual_router
from zeroth.econ.plane.costing.api import router as costing_router
from zeroth.econ.plane.dashboard.api import router as dashboard_router
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.decisioning.scheduler import run_scheduler_loop
from zeroth.econ.plane.enforcement.api import router as enforcement_router
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.performance.api import router as performance_router
from zeroth.econ.plane.reconciliation.api import router as reconciliation_router
from zeroth.econ.plane.scoped_session import ScopedSession

app = FastAPI(title="AI Economic Control Plane", version="0.1.0")

app.include_router(auth_router, prefix="/v1")
app.include_router(backtesting_router, prefix="/v1")
app.include_router(instrumentation_router, prefix="/v1")
app.include_router(capabilities_router, prefix="/v1")
app.include_router(cloud_router, prefix="/v1")
app.include_router(cloud_authkit_router, prefix="/v1")
app.include_router(cloud_keys_router, prefix="/v1")
app.include_router(paddle_router, prefix="/v1")
app.include_router(cloud_web_router)
app.include_router(counterfactual_router, prefix="/v1")
app.include_router(costing_router, prefix="/v1")
app.include_router(performance_router, prefix="/v1")
app.include_router(enforcement_router, prefix="/v1")
app.include_router(dashboard_router, prefix="/v1")
app.include_router(decisioning_router, prefix="/v1")
app.include_router(reconciliation_router, prefix="/v1")
app.include_router(connectors_router, prefix="/v1")

request_logger = logging.getLogger("econ_plane.requests")
request_logger.setLevel(getattr(logging, settings.request_log_level.upper(), logging.DEBUG))


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    start = time.perf_counter()
    response = await call_next(request)
    if settings.request_log_enabled and request.url.path != "/health":
        if random.random() <= max(0.0, min(1.0, settings.request_log_sample_rate)):
            duration_ms = (time.perf_counter() - start) * 1000
            request_logger.debug(
                "request method=%s path=%s status=%s duration_ms=%.2f",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
            )
    return response


@app.on_event("startup")
def startup() -> None:
    validate_startup_settings()
    common_bootstrap.bootstrap()
    init_otel_metrics()


@app.on_event("startup")
async def start_cloud_scheduler() -> None:
    if not settings.cloud_scheduler_enabled:
        return
    stop = asyncio.Event()
    app.state.cloud_scheduler_stop = stop
    app.state.cloud_scheduler_task = asyncio.create_task(
        run_scheduler_loop(
            stop,
            interval_seconds=settings.cloud_scheduler_interval_seconds,
        ),
        name="zeroth-cloud-decision-scheduler",
    )


@app.on_event("shutdown")
async def stop_cloud_scheduler() -> None:
    stop = getattr(app.state, "cloud_scheduler_stop", None)
    task = getattr(app.state, "cloud_scheduler_task", None)
    if stop is None or task is None:
        return
    stop.set()
    await task


@app.get("/health")
@app.get("/health/ready")
def health() -> dict[str, object]:
    revision = common_bootstrap.schema_revision()
    scheduler_task = getattr(app.state, "cloud_scheduler_task", None)
    scheduler_state = (
        "disabled"
        if not settings.cloud_scheduler_enabled
        else "ok"
        if scheduler_task is not None and not scheduler_task.done()
        else "failed"
    )
    return {
        "status": (
            "ok"
            if revision.state == "current" and scheduler_state != "failed"
            else "degraded"
        ),
        "schema_revision": revision,
        "scheduler": {"status": scheduler_state},
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> str:
    if not settings.prometheus_enabled:
        return ""
    return render_prometheus_metrics(db)
