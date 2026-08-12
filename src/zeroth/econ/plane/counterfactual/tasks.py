import dramatiq

from zeroth.econ.plane.common.worker import redis_broker  # noqa: F401
from zeroth.econ.plane.counterfactual.schemas import EvaluationRunRequest
from zeroth.econ.plane.counterfactual.service import run_evaluation
from zeroth.econ.plane.costing.service import PricingCatalogReader
from zeroth.econ.plane.database import SessionLocal
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import ScopeContext, TenantWideScopeContext


def _task_scope(tenant_id: str, workspace_id: str | None):
    if workspace_id is None:
        if tenant_id == "default":
            return TenantWideScopeContext.for_default_compatibility()
        return TenantWideScopeContext(tenant_id=tenant_id)
    if tenant_id == "default":
        return ScopeContext.for_default_compatibility(workspace_id=workspace_id)
    return ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)


@dramatiq.actor
def run_evaluation_async(payload: dict) -> None:
    request = EvaluationRunRequest(**payload["request"])
    scope = _task_scope(str(payload["tenant_id"]), payload.get("workspace_id"))
    with SessionLocal() as db, SessionLocal() as pricing_db:
        run_evaluation(
            ScopedSession(db, scope),
            request,
            pricing=PricingCatalogReader(ScopedSession(pricing_db, None)),
        )
