from __future__ import annotations

from datetime import datetime, timezone

import dramatiq
from sqlalchemy import and_, select

from zeroth.econ.plane.common.worker import redis_broker
from zeroth.econ.plane.database import SessionLocal
from zeroth.econ.plane.connectors.models import ConnectorOutbox
from zeroth.econ.plane.connectors.service import process_outbox_batch
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


dramatiq.set_broker(redis_broker)


def _eligible_tenant_ids() -> list[str]:
    """Discover scheduled work internally; tenant IDs never come from actor input."""
    with SessionLocal() as db:
        return list(
            db.execute(
                select(ConnectorOutbox.tenant_id)
                .where(
                    and_(
                        ConnectorOutbox.status.in_(["PENDING", "FAILED"]),
                        ConnectorOutbox.next_attempt_at <= datetime.now(timezone.utc),
                    )
                )
                .distinct()
                .order_by(ConnectorOutbox.tenant_id)
            ).scalars()
        )


def _process_connector_outbox(batch_size: int) -> int:
    processed = 0
    for tenant_id in _eligible_tenant_ids():
        scope = (
            TenantWideScopeContext.for_default_compatibility()
            if tenant_id == "default"
            else TenantWideScopeContext(tenant_id=tenant_id)
        )
        with SessionLocal() as db:
            processed += process_outbox_batch(ScopedSession(db, scope), batch_size=batch_size)
    return processed


@dramatiq.actor(max_retries=0)
def process_connector_outbox(batch_size: int = 100) -> int:
    return _process_connector_outbox(batch_size)
