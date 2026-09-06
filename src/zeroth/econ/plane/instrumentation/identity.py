"""Public workflow/run identity, independent of legacy registry primary keys."""

import hashlib
import json
from collections import defaultdict

from sqlalchemy import Integer, and_, bindparam, func, or_, select

from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession

RunKey = tuple[str, str, str]


def workflow_identity(row: ExecutionEvent | OutcomeEvent) -> tuple[str, str]:
    return (
        row.workflow_id if row.workflow_id is not None else row.capability_id,
        row.workflow_version if row.workflow_version is not None else row.implementation_id or "",
    )


def run_identity(event: ExecutionEvent) -> RunKey:
    return (*workflow_identity(event), event.run_id or event.join_key or event.execution_id)


def workflow_filter(model, workflow: str, version: str):
    return and_(
        func.coalesce(model.workflow_id, model.capability_id) == workflow,
        func.coalesce(model.workflow_version, model.implementation_id) == version,
    )


def sdk_registry_ids(db: ScopedSession, workflow: str, version: str) -> tuple[str, str]:
    """Retain proven, owned legacy mappings; scope new mappings by tenant and workflow.

    An arbitrary registry name is insufficient proof of the old SDK mapping.
    Require evidence bearing that public identity and both owned registry rows.
    This single mapping applies to new events and retries; history is never rewritten.
    """
    tenant_id = db.scope.tenant_id

    def digest(parts: list[str]) -> str:
        return hashlib.sha256(
            json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()

    canonical = (
        f"sdk_wf_{digest([tenant_id, workflow])}",
        f"sdk_ver_{digest([tenant_id, workflow, version])}",
    )
    registered = db.get(Implementation, canonical[1])
    if registered is not None and registered.capability_id == canonical[0]:
        return canonical
    legacy = db.scalars(
        select(ExecutionEvent.id)
        .where(
            ExecutionEvent.capability_id == workflow,
            ExecutionEvent.implementation_id == version,
            workflow_filter(ExecutionEvent, workflow, version),
        )
        .limit(1)
    ).first()
    if legacy is not None:
        capability = db.get(Capability, workflow)
        implementation = db.get(Implementation, version)
        if (
            capability is not None
            and implementation is not None
            and implementation.capability_id == workflow
        ):
            return workflow, version
    return canonical


def outcomes_for_events(
    db: ScopedSession,
    events: list[ExecutionEvent],
    *,
    outcome_type: str | None = None,
    limit: int | None = None,
) -> list[tuple[RunKey, OutcomeEvent]]:
    """Join explicit public identities, or exact legacy storage identities.

    Legacy outcomes have no public workflow columns. Their execution rows supply
    the mapping; ambiguous mappings stay unresolved. Bare run IDs never suffice.
    """
    keys = {run_identity(event) for event in events}
    if not keys:
        return []
    run_column = func.coalesce(
        func.nullif(ExecutionEvent.run_id, ""),
        func.nullif(ExecutionEvent.join_key, ""),
        ExecutionEvent.execution_id,
    )
    storage_match = and_(
        ExecutionEvent.capability_id == OutcomeEvent.capability_id,
        ExecutionEvent.implementation_id == OutcomeEvent.implementation_id,
        ExecutionEvent.join_key == OutcomeEvent.join_key,
    )
    matching_execution = (
        # Declare both mapped resources to the scoped ORM validator, including
        # the correlated outer row. EXISTS does not return either projection.
        select(ExecutionEvent.id, OutcomeEvent.id)
        .where(
            # Render trusted integer row IDs through SQLAlchemy's typed binder.
            # The 50k event bound exceeds some drivers' parameter-count limits.
            ExecutionEvent.id.in_(
                bindparam(
                    "execution_ids",
                    sorted({event.id for event in events}),
                    type_=Integer,
                    expanding=True,
                    literal_execute=True,
                )
            ),
            or_(
                and_(
                    func.coalesce(ExecutionEvent.workflow_id, ExecutionEvent.capability_id)
                    == OutcomeEvent.workflow_id,
                    func.coalesce(ExecutionEvent.workflow_version, ExecutionEvent.implementation_id)
                    == OutcomeEvent.workflow_version,
                    run_column == OutcomeEvent.join_key,
                ),
                and_(
                    OutcomeEvent.workflow_id.is_(None),
                    OutcomeEvent.workflow_version.is_(None),
                    storage_match,
                ),
            ),
        )
        .correlate(OutcomeEvent)
    )
    statement = (
        select(OutcomeEvent)
        .where(matching_execution.exists())
        .order_by(OutcomeEvent.occurred_at.desc(), OutcomeEvent.id.desc())
    )
    if outcome_type is not None:
        statement = statement.where(OutcomeEvent.outcome_type == outcome_type)
    if limit is not None:
        statement = statement.limit(limit)
    outcomes = list(db.scalars(statement))
    legacy = [
        outcome
        for outcome in outcomes
        if outcome.workflow_id is None and outcome.workflow_version is None
    ]
    legacy_keys: dict[tuple[str, str | None, str], set[RunKey]] = defaultdict(set)
    if legacy:
        # A version/window filter must not hide a conflicting historical mapping.
        # Project distinct identities only; do not load the full event history.
        mappings = db.execute(
            select(
                ExecutionEvent.capability_id,
                ExecutionEvent.implementation_id,
                ExecutionEvent.join_key,
                func.coalesce(ExecutionEvent.workflow_id, ExecutionEvent.capability_id),
                func.coalesce(ExecutionEvent.workflow_version, ExecutionEvent.implementation_id),
                run_column,
            )
            .where(
                select(OutcomeEvent.id, ExecutionEvent.id)
                .where(
                    OutcomeEvent.id.in_(
                        bindparam(
                            "outcome_ids",
                            sorted({outcome.id for outcome in legacy}),
                            type_=Integer,
                            expanding=True,
                            literal_execute=True,
                        )
                    ),
                    storage_match,
                )
                .correlate(ExecutionEvent)
                .exists(),
            )
            .distinct()
        )
        for cap, impl, join, workflow, version, run in mappings:
            legacy_keys[(cap, impl, join)].add((workflow, version, run))
    result = []
    for outcome in outcomes:
        if outcome.workflow_id is None and outcome.workflow_version is None:
            candidates = legacy_keys.get(
                (outcome.capability_id, outcome.implementation_id, outcome.join_key), set()
            )
        else:
            candidates = {(*workflow_identity(outcome), outcome.join_key)} & keys
        if len(candidates) == 1 and (key := next(iter(candidates))) in keys:
            result.append((key, outcome))
    return result
