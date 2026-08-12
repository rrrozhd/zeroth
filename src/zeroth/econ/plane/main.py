import logging
import random
import time

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.requests import Request

from zeroth.econ.plane.auth.api import router as auth_router
from zeroth.econ.plane.capabilities.api import router as capabilities_router
from zeroth.econ.plane.common.bootstrap import bootstrap
from zeroth.econ.plane.config import settings, validate_startup_settings
from zeroth.econ.plane.connectors.api import router as connectors_router
from zeroth.econ.plane.connectors.service import init_otel_metrics, render_prometheus_metrics
from zeroth.econ.plane.counterfactual.api import router as counterfactual_router
from zeroth.econ.plane.costing.api import router as costing_router
from zeroth.econ.plane.dashboard.api import router as dashboard_router
from zeroth.econ.plane.enforcement.api import router as enforcement_router
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.performance.api import router as performance_router
from zeroth.econ.plane.reconciliation.api import router as reconciliation_router
from zeroth.econ.plane.database import SessionLocal

app = FastAPI(title="AI Economic Control Plane", version="0.1.0")

app.include_router(auth_router, prefix="/v1")
app.include_router(instrumentation_router, prefix="/v1")
app.include_router(capabilities_router, prefix="/v1")
app.include_router(counterfactual_router, prefix="/v1")
app.include_router(costing_router, prefix="/v1")
app.include_router(performance_router, prefix="/v1")
app.include_router(enforcement_router, prefix="/v1")
app.include_router(dashboard_router, prefix="/v1")
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
    bootstrap()
    init_otel_metrics()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    if not settings.prometheus_enabled:
        return ""
    with SessionLocal() as db:
        return render_prometheus_metrics(db)
