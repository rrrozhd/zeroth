from __future__ import annotations

import dramatiq

from zeroth.econ.plane.common.worker import redis_broker
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import SessionLocal
from zeroth.econ.plane.connectors.service import process_outbox_batch
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


dramatiq.set_broker(redis_broker)


@dramatiq.actor(max_retries=0)
def process_connector_outbox(batch_size: int = 100) -> int:
    tenant_id = settings.service_principal_tenant_id
    scope = (
        TenantWideScopeContext.for_default_compatibility()
        if tenant_id == "default"
        else TenantWideScopeContext(tenant_id=tenant_id)
    )
    with SessionLocal() as db:
        return process_outbox_batch(ScopedSession(db, scope), batch_size=batch_size)
