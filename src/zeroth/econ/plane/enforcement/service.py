from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update

from zeroth.econ.plane.config import settings
from zeroth.econ.plane.connectors.service import enqueue_connector_event
from zeroth.econ.plane.enforcement.models import (
    AuditLog,
    CostReservation,
    EnforcementAction,
    PolicyAction,
    TenantBudget,
    TrafficPolicy,
)
from zeroth.econ.plane.enforcement.schemas import EnforcementActionCreate
from zeroth.econ.plane.scoped_session import ScopedSession


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("enforcement persistence requires an exact ScopedSession")
    return db


def _bound_tenant(db: ScopedSession) -> str:
    db = _require_exact_scoped_session(db)
    if db.scope is None:
        raise ValueError("enforcement persistence requires a tenant-bound scope")
    return db.scope.tenant_id


_ACTION_MAP = {
    "AdjustTrafficWeights": "SHIFT_TRAFFIC",
    "ApplyBudgetCap": "BUDGET_CAP",
    "TriggerInvestigation": "INVESTIGATION_FLAG",
    "EscalateAlert": "INVESTIGATION_FLAG",
}


def _propose_policy_action(
    db: ScopedSession,
    *,
    capability_id: str,
    action_type: str,
    payload_json: dict,
    enforcement_action_id: int,
    confidence_state_json: dict | None = None,
) -> PolicyAction:
    """Propose a policy action for ``enforcement_action_id``.

    The link is required rather than defaulted: a row created without one would be
    indistinguishable from a pre-link legacy row, which ``decide_action`` refuses
    to resolve -- so the proposal would be silently undecidable.
    """
    db = _require_exact_scoped_session(db)
    row = PolicyAction(
        tenant_id=_bound_tenant(db),
        capability_id=capability_id,
        enforcement_action_id=enforcement_action_id,
        proposed_at=datetime.now(UTC),
        proposed_by="system",
        action_type=_ACTION_MAP.get(action_type, action_type),
        payload_json=payload_json,
        confidence_state_json=confidence_state_json or {},
        status="PROPOSED",
    )
    db.add(row)
    db.flush()
    if settings.connectors_enabled:
        try:
            enqueue_connector_event(
                db,
                tenant_id=row.tenant_id,
                event_type="policy_action.lifecycle",
                event_key=f"{row.id}:PROPOSED",
                capability_id=capability_id,
                payload={
                    "policy_action_id": row.id,
                    "action_type": row.action_type,
                    "status": "PROPOSED",
                    "payload_json": payload_json,
                    "confidence_state_json": confidence_state_json or {},
                    "proposed_at": row.proposed_at.isoformat(),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return row


def create_action(db: ScopedSession, payload: EnforcementActionCreate) -> EnforcementAction:
    db = _require_exact_scoped_session(db)
    row = EnforcementAction(
        capability_id=payload.capability_id,
        action_type=payload.action_type,
        status="pending",
        reason=payload.reason,
        before_config=payload.before_config,
        after_config=payload.after_config,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()

    _propose_policy_action(
        db,
        capability_id=payload.capability_id,
        action_type=payload.action_type,
        payload_json={
            "reason": payload.reason,
            "before_config": payload.before_config,
            "after_config": payload.after_config,
        },
        enforcement_action_id=row.id,
    )
    db.commit()
    db.refresh(row)
    return row


def list_actions(db: ScopedSession, status: str | None = None) -> list[EnforcementAction]:
    db = _require_exact_scoped_session(db)
    stmt = select(EnforcementAction)
    if status:
        stmt = stmt.where(EnforcementAction.status == status)
    return list(db.execute(stmt.order_by(EnforcementAction.id.desc())).scalars())


def list_policy_actions(db: ScopedSession, status: str | None = None) -> list[PolicyAction]:
    db = _require_exact_scoped_session(db)
    stmt = select(PolicyAction)
    if status:
        stmt = stmt.where(PolicyAction.status == status.upper())
    return list(db.execute(stmt.order_by(PolicyAction.id.desc())).scalars())


def _apply_traffic_policy(
    db: ScopedSession, capability_id: str, after_config: dict
) -> None:
    db = _require_exact_scoped_session(db)
    policy = db.execute(select(TrafficPolicy).where(TrafficPolicy.capability_id == capability_id)).scalar_one_or_none()  # noqa: E501
    if policy is None:
        policy = TrafficPolicy(capability_id=capability_id, weights=after_config)
        db.add(policy)
    else:
        policy.weights = after_config


_LINKED = "linked"
_UNLINKED = "unlinked"


def _linked_policy_action(db: ScopedSession, row: EnforcementAction) -> PolicyAction | None:
    """Return the policy action proposed for ``row``, or ``None`` when unlinked.

    Linkage is structural: only the policy action whose ``enforcement_action_id``
    matches is eligible.  A policy action carrying NULL is *unlinked* -- it either
    predates the link column or was proposed outside an enforcement decision --
    and is deliberately left alone.  Falling back to "the newest policy action for
    this capability" is precisely the defect this column exists to remove (A01-11):
    it transitioned a row that belonged to a different enforcement action.

    At most one policy action is proposed per enforcement action, so a second
    match is an invariant violation rather than an expected data state; it raises
    instead of picking one.
    """
    db = _require_exact_scoped_session(db)
    return db.execute(
        select(PolicyAction).where(PolicyAction.enforcement_action_id == row.id)
    ).scalar_one_or_none()


def decide_action(
    db: ScopedSession,
    action_id: int,
    decision: str,
    approver_sub: str,
    reason: str,
) -> EnforcementAction | None:
    """Record an approval or rejection and propagate it to the linked policy action.

    When no linked policy action exists the enforcement action is still decided --
    refusing would make pre-link actions impossible to even *reject* -- but nothing
    is applied and the audit entry states that the decision did not propagate.
    """
    db = _require_exact_scoped_session(db)
    row = db.get(EnforcementAction, action_id)
    if row is None:
        return None
    row.status = "approved" if decision == "approve" else "rejected"
    row.approver_sub = approver_sub
    row.approved_at = datetime.now(UTC)
    if reason:
        row.reason = reason

    policy = _linked_policy_action(db, row)
    if policy is not None:
        if decision == "approve":
            policy.status = "APPROVED"
            policy.approved_by = approver_sub
            policy.approved_at = datetime.now(UTC)
            if row.action_type == "AdjustTrafficWeights":
                _apply_traffic_policy(db, row.capability_id, row.after_config)
            policy.status = "APPLIED"
            policy.applied_at = datetime.now(UTC)
            if settings.connectors_enabled:
                try:
                    enqueue_connector_event(
                        db,
                        tenant_id=policy.tenant_id,
                        event_type="policy_action.lifecycle",
                        event_key=f"{policy.id}:APPLIED",
                        capability_id=policy.capability_id,
                        payload={
                            "policy_action_id": policy.id,
                            "action_type": policy.action_type,
                            "status": "APPLIED",
                            "approved_by": approver_sub,
                            "approved_at": policy.approved_at.isoformat() if policy.approved_at else None,  # noqa: E501
                            "applied_at": policy.applied_at.isoformat() if policy.applied_at else None,  # noqa: E501
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            policy.status = "REJECTED"
            policy.approved_by = approver_sub
            policy.approved_at = datetime.now(UTC)
            if settings.connectors_enabled:
                try:
                    enqueue_connector_event(
                        db,
                        tenant_id=policy.tenant_id,
                        event_type="policy_action.lifecycle",
                        event_key=f"{policy.id}:REJECTED",
                        capability_id=policy.capability_id,
                        payload={
                            "policy_action_id": policy.id,
                            "action_type": policy.action_type,
                            "status": "REJECTED",
                            "approved_by": approver_sub,
                            "approved_at": policy.approved_at.isoformat() if policy.approved_at else None,  # noqa: E501
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass

    audit = AuditLog(
        actor_sub=approver_sub,
        action=f"enforcement_{decision}",
        entity_type="EnforcementAction",
        entity_id=str(action_id),
        payload={
            "before": row.before_config,
            "after": row.after_config,
            "decision_reason": reason,
            "policy_action_id": policy.id if policy is not None else None,
            "policy_linkage": _LINKED if policy is not None else _UNLINKED,
        },
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    db.commit()
    db.refresh(row)
    return row


def get_budget_status(
    db: ScopedSession, tenant_id: str, deployment_ref: str | None = None
) -> dict:
    """Return non-overlapping realized spend, open exposure, and control evidence."""
    db = _require_exact_scoped_session(db)
    from zeroth.econ.plane.instrumentation.models import ExecutionEvent

    now = datetime.now(UTC)
    window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = window_start.replace(tzinfo=None)
    billable = ("production", "legacy_unknown")
    event_cost = (
        func.coalesce(ExecutionEvent.token_cost_usd, 0)
        + func.coalesce(ExecutionEvent.tool_cost_usd, 0)
        + func.coalesce(ExecutionEvent.compute_cost_usd, 0)
    )
    event_scope = (
        [ExecutionEvent.deployment_ref == deployment_ref]
        if deployment_ref is not None
        else []
    )
    reservation_scope = (
        [CostReservation.deployment_ref == deployment_ref]
        if deployment_ref is not None
        else []
    )

    def event_sum(measurement: str) -> Decimal:
        return Decimal(
            db.execute(
                select(func.coalesce(func.sum(event_cost), 0)).where(
                    ExecutionEvent.tenant_id == tenant_id,
                    ExecutionEvent.timestamp >= start,
                    ExecutionEvent.operation_id.is_(None),
                    ExecutionEvent.evidence_kind.in_(billable),
                    ExecutionEvent.cost_measurement == measurement,
                    *event_scope,
                )
            ).scalar_one()
            or 0
        )

    def reservation_sum(*, status: str, measurement: str | None = None) -> Decimal:
        value = CostReservation.actual_cost_usd if status == "committed" else CostReservation.held_cost_usd
        conditions = [
            CostReservation.status == status,
            CostReservation.evidence_kind.in_(billable),
            *reservation_scope,
        ]
        if status == "committed":
            conditions.append(CostReservation.updated_at >= start)
        if measurement is not None:
            conditions.append(CostReservation.cost_measurement == measurement)
        return Decimal(
            db.execute(select(func.coalesce(func.sum(value), 0)).where(*conditions)).scalar_one()
            or 0
        )

    paid = event_sum("measured") + reservation_sum(
        status="committed", measurement="measured"
    )
    estimated = event_sum("estimated") + reservation_sum(
        status="committed", measurement="estimated"
    )
    unmeasured = event_sum("unmeasured") + reservation_sum(
        status="committed", measurement="unmeasured"
    )
    active = reservation_sum(status="reserved")
    ambiguous = reservation_sum(status="ambiguous")
    synthetic = Decimal(
        db.execute(
            select(func.coalesce(func.sum(CostReservation.held_cost_usd), 0)).where(
                CostReservation.evidence_kind == "synthetic_control",
                CostReservation.status.in_(_HELD_STATUSES),
                *reservation_scope,
            )
        ).scalar_one()
        or 0
    )
    measurements = set(
        db.execute(
            select(CostReservation.cost_measurement).where(
                CostReservation.status.in_(_HELD_STATUSES),
                CostReservation.evidence_kind.in_(billable),
                *reservation_scope,
            )
        ).scalars()
    )
    measurements |= set(
        db.execute(
            select(ExecutionEvent.cost_measurement).where(
                ExecutionEvent.tenant_id == tenant_id,
                ExecutionEvent.timestamp >= start,
                ExecutionEvent.operation_id.is_(None),
                ExecutionEvent.evidence_kind.in_(billable),
                *event_scope,
            )
        ).scalars()
    )
    measurement_complete = "unmeasured" not in measurements
    cost_measurement = (
        "unmeasured"
        if not measurement_complete
        else "estimated"
        if "estimated" in measurements
        else "measured"
    )
    row = db.execute(
        select(TenantBudget).where(TenantBudget.tenant_id == tenant_id)
    ).scalar_one_or_none()
    actual_spend = paid + estimated + unmeasured
    budget_consumed = actual_spend + active + ambiguous
    return {
        "tenant_id": tenant_id,
        "total_cost_usd": float(budget_consumed),
        "actual_spend_usd": float(actual_spend),
        "paid_spend_usd": float(paid),
        "estimated_spend_usd": float(estimated),
        "unmeasured_spend_usd": float(unmeasured),
        "active_exposure_usd": float(active),
        "ambiguous_exposure_usd": float(ambiguous),
        "budget_consumed_usd": float(budget_consumed),
        "synthetic_control_usd": float(synthetic),
        "budget_cap_usd": row.budget_cap_usd if row is not None else None,
        "measurement_complete": measurement_complete,
        "cost_measurement": cost_measurement,
        "window": "month_to_date",
        "window_start": window_start,
    }


def upsert_tenant_budget(
    db: ScopedSession, tenant_id: str, budget_cap_usd: float
) -> TenantBudget:
    db = _require_exact_scoped_session(db)
    row = db.execute(
        select(TenantBudget).where(TenantBudget.tenant_id == tenant_id)
    ).scalar_one_or_none()
    now = datetime.now(UTC).replace(tzinfo=None)
    if row is None:
        row = TenantBudget(tenant_id=tenant_id, budget_cap_usd=budget_cap_usd, updated_at=now)
        db.add(row)
    else:
        row.budget_cap_usd = budget_cap_usd
        row.updated_at = now
    db.commit()
    db.refresh(row)
    return row


class CostReservationDenied(RuntimeError):
    """Admission was refused before externally billable work began."""


_HELD_STATUSES = ("reserved", "ambiguous", "committed")


def _money(value: Decimal | float | str) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost amount must be finite and non-negative")
    return amount.quantize(Decimal("0.00000001"))


def _reservation(db: ScopedSession, operation_id: str) -> CostReservation | None:
    return db.execute(
        select(CostReservation).where(CostReservation.operation_id == operation_id)
    ).scalar_one_or_none()


def reserve_cost(
    db: ScopedSession,
    *,
    operation_id: str,
    max_cost_usd: Decimal | float | str,
    campaign_id: str | None = None,
    run_id: str | None = None,
    deployment_ref: str | None = None,
    evidence_kind: str = "production",
    run_cap_usd: Decimal | float | str | None = None,
    require_new: bool = False,
) -> CostReservation:
    """Atomically reserve worst-case cost against tenant and optional run ceilings.

    Updating the tenant-budget row is the coordination fence: PostgreSQL takes a
    row lock and SQLite takes its write lock before either spend is read.  Every
    concurrent admission for one tenant therefore observes the preceding commit.
    """
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    if not operation_id:
        raise ValueError("operation_id is required")
    if evidence_kind not in {"production", "synthetic_control", "legacy_unknown"}:
        raise ValueError("unsupported cost evidence kind")
    requested = _money(max_cost_usd)
    run_cap = _money(run_cap_usd) if run_cap_usd is not None else None
    if run_cap is not None and run_id is None:
        raise ValueError("run_id is required when run_cap_usd is set")

    try:
        # A no-op UPDATE is deliberately first. It serializes admission without
        # mutating ownership or requiring dialect-specific advisory locks.
        db.execute(
            update(TenantBudget)
            .where(TenantBudget.tenant_id == tenant_id)
            .values(updated_at=TenantBudget.updated_at)
        )
        budget = db.execute(
            select(TenantBudget).where(TenantBudget.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if budget is None:
            raise CostReservationDenied("tenant budget is not configured; failing closed")

        existing = _reservation(db, operation_id)
        if existing is not None:
            identity = (
                existing.campaign_id,
                existing.run_id,
                existing.deployment_ref,
                existing.evidence_kind,
                _money(existing.max_cost_usd),
            )
            if identity != (campaign_id, run_id, deployment_ref, evidence_kind, requested):
                raise CostReservationDenied(
                    "operation_id already exists with different reservation parameters"
                )
            if require_new:
                raise CostReservationDenied(
                    "operation_id was already admitted; provider work will not be replayed"
                )
            db.commit()
            db.refresh(existing)
            return existing

        held = Decimal(
            db.execute(
                select(func.coalesce(func.sum(CostReservation.held_cost_usd), 0)).where(
                    CostReservation.status.in_(_HELD_STATUSES)
                )
            ).scalar_one()
            or 0
        )
        from zeroth.econ.plane.instrumentation.models import ExecutionEvent

        ordinary_spend = Decimal(
            db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            func.coalesce(ExecutionEvent.token_cost_usd, 0)
                            + func.coalesce(ExecutionEvent.tool_cost_usd, 0)
                            + func.coalesce(ExecutionEvent.compute_cost_usd, 0)
                        ),
                        0,
                    )
                ).where(ExecutionEvent.operation_id.is_(None))
            ).scalar_one()
            or 0
        )
        if ordinary_spend + held + requested > _money(budget.budget_cap_usd):
            raise CostReservationDenied("tenant ceiling would be exceeded")

        if run_cap is not None:
            run_held = Decimal(
                db.execute(
                    select(func.coalesce(func.sum(CostReservation.held_cost_usd), 0)).where(
                        CostReservation.run_id == run_id,
                        CostReservation.status.in_(_HELD_STATUSES),
                    )
                ).scalar_one()
                or 0
            )
            if run_held + requested > run_cap:
                raise CostReservationDenied("run ceiling would be exceeded")

        now = datetime.now(UTC).replace(tzinfo=None)
        row = CostReservation(
            tenant_id=tenant_id,
            operation_id=operation_id,
            campaign_id=campaign_id,
            run_id=run_id,
            deployment_ref=deployment_ref,
            evidence_kind=evidence_kind,
            status="reserved",
            max_cost_usd=requested,
            held_cost_usd=requested,
            actual_cost_usd=None,
            released_cost_usd=Decimal("0"),
            cost_measurement="unmeasured",
            cleanup_status="not_started",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception:
        db.rollback()
        raise


def _transition_reservation(
    db: ScopedSession,
    *,
    operation_id: str,
    status: str,
    actual_cost_usd: Decimal | float | str | None = None,
    cost_measurement: str = "unmeasured",
    cost_event_id: str | None = None,
    provider_request_id: str | None = None,
    cleanup_status: str | None = None,
    allowed_statuses: frozenset[str] = frozenset({"reserved"}),
) -> CostReservation:
    db = _require_exact_scoped_session(db)
    row = _reservation(db, operation_id)
    if row is None:
        raise CostReservationDenied("reservation not found; failing closed")
    if row.status not in allowed_statuses:
        raise CostReservationDenied(
            f"invalid reservation transition from {row.status!r} to {status!r}"
        )
    actual = _money(actual_cost_usd) if actual_cost_usd is not None else None
    maximum = _money(row.max_cost_usd)
    if actual is not None and actual > maximum:
        raise CostReservationDenied("actual cost exceeds reserved maximum")
    row.status = status
    row.actual_cost_usd = actual
    row.held_cost_usd = maximum if status == "ambiguous" else actual or Decimal("0")
    row.released_cost_usd = maximum - row.held_cost_usd
    row.cost_measurement = cost_measurement
    row.cost_event_id = cost_event_id or row.cost_event_id
    row.provider_request_id = provider_request_id or row.provider_request_id
    row.cleanup_status = cleanup_status or row.cleanup_status
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    db.commit()
    db.refresh(row)
    return row


def commit_cost(
    db: ScopedSession,
    *,
    operation_id: str,
    actual_cost_usd: Decimal | float | str,
    cost_measurement: str,
    cost_event_id: str,
    provider_request_id: str | None = None,
    cleanup_status: str | None = None,
) -> CostReservation:
    return _transition_reservation(
        db,
        operation_id=operation_id,
        status="committed",
        actual_cost_usd=actual_cost_usd,
        cost_measurement=cost_measurement,
        cost_event_id=cost_event_id,
        provider_request_id=provider_request_id,
        cleanup_status=cleanup_status,
    )


def mark_cost_ambiguous(
    db: ScopedSession,
    *,
    operation_id: str,
    cost_event_id: str | None = None,
    provider_request_id: str | None = None,
    cleanup_status: str = "pending_reconciliation",
) -> CostReservation:
    return _transition_reservation(
        db,
        operation_id=operation_id,
        status="ambiguous",
        cost_event_id=cost_event_id,
        provider_request_id=provider_request_id,
        cleanup_status=cleanup_status,
    )


def release_cost(
    db: ScopedSession, *, operation_id: str, cleanup_status: str = "complete"
) -> CostReservation:
    return _transition_reservation(
        db,
        operation_id=operation_id,
        status="released",
        actual_cost_usd=Decimal("0"),
        cost_measurement="measured",
        cleanup_status=cleanup_status,
    )


def reconcile_cost(
    db: ScopedSession,
    *,
    operation_id: str,
    actual_cost_usd: Decimal | float | str,
    cost_measurement: str,
    provider_request_id: str | None = None,
    cleanup_status: str = "complete",
) -> CostReservation:
    """Resolve an ambiguous hold to measured/estimated actual provider cost."""
    row = _reservation(_require_exact_scoped_session(db), operation_id)
    if row is None or row.status != "ambiguous":
        raise CostReservationDenied("only an ambiguous reservation can be reconciled")
    return _transition_reservation(
        db,
        operation_id=operation_id,
        status="committed",
        actual_cost_usd=actual_cost_usd,
        cost_measurement=cost_measurement,
        cost_event_id=row.cost_event_id or f"reconciled:{operation_id}",
        provider_request_id=provider_request_id,
        cleanup_status=cleanup_status,
        allowed_statuses=frozenset({"ambiguous"}),
    )


def reconcile_provider_not_called(
    db: ScopedSession,
    *,
    operation_id: str,
    reason: str,
) -> CostReservation:
    """Release an ambiguous maximum when an operator proves no billable request occurred."""
    if not reason.strip():
        raise CostReservationDenied("provider-not-called reconciliation requires a reason")
    row = _reservation(_require_exact_scoped_session(db), operation_id)
    if row is None or row.status != "ambiguous":
        raise CostReservationDenied("only an ambiguous reservation can be reconciled")
    return _transition_reservation(
        db,
        operation_id=operation_id,
        status="released",
        actual_cost_usd=Decimal("0"),
        cost_measurement="measured",
        cost_event_id=row.cost_event_id,
        provider_request_id=None,
        cleanup_status="provider_not_called",
        allowed_statuses=frozenset({"ambiguous"}),
    )
