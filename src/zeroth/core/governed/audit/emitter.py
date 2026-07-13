from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from zeroth.core.governed.models.audit import AuditEvent, AuditExtension
from zeroth.core.governed.models.common import EventType


class AuditEmitter(ABC):
    @abstractmethod
    async def emit(self, event: AuditEvent) -> None:
        """Emit."""
        raise NotImplementedError


async def emit_event(
    emitter: AuditEmitter,
    *,
    run_id: str,
    thread_id: str | None = None,
    workflow_name: str,
    event_type: EventType,
    step_name: str | None = None,
    payload: dict[str, Any] | None = None,
    extensions: list[AuditExtension] | None = None,
) -> AuditEvent:
    """Emit event."""
    event = AuditEvent(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        thread_id=thread_id,
        workflow_name=workflow_name,
        step_name=step_name,
        event_type=event_type,
        payload=payload or {},
        extensions=extensions or [],
    )
    await emitter.emit(event)
    return event
