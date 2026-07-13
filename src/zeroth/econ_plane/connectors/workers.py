from __future__ import annotations

import dramatiq

from zeroth.econ_plane.common.worker import redis_broker
from zeroth.econ_plane.database import SessionLocal
from zeroth.econ_plane.connectors.service import process_outbox_batch


dramatiq.set_broker(redis_broker)


@dramatiq.actor(max_retries=0)
def process_connector_outbox(batch_size: int = 100) -> int:
    with SessionLocal() as db:
        return process_outbox_batch(db, batch_size=batch_size)
