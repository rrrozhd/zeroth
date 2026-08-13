from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from zeroth.econ.plane.connectors.service import enqueue_connector_event
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.enforcement.models import AuditLog, EnforcementAction, PolicyAction, TenantBudget, TrafficPolicy
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
    capability_id: str,
    action_type: str,
    payload_json: dict,
    confidence_state_json: dict | None = None,
) -> PolicyAction:
    db = _require_exact_scoped_session(db)
    row = PolicyAction(
        tenant_id=_bound_tenant(db),
        capability_id=capability_id,
        proposed_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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
    )
    db.commit()
    db.refresh(row)
    return row


def list_actions(db: ScopedSession, status: Optional[str] = None) -> list[EnforcementAction]:
    db = _require_exact_scoped_session(db)
    stmt = select(EnforcementAction)
    if status:
        stmt = stmt.where(EnforcementAction.status == status)
    return list(db.execute(stmt.order_by(EnforcementAction.id.desc())).scalars())


def list_policy_actions(db: ScopedSession, status: Optional[str] = None) -> list[PolicyAction]:
    db = _require_exact_scoped_session(db)
    stmt = select(PolicyAction)
    if status:
        stmt = stmt.where(PolicyAction.status == status.upper())
    return list(db.execute(stmt.order_by(PolicyAction.id.desc())).scalars())


def _apply_traffic_policy(
    db: ScopedSession, capability_id: str, after_config: dict
) -> None:
    db = _require_exact_scoped_session(db)
    policy = db.execute(select(TrafficPolicy).where(TrafficPolicy.capability_id == capability_id)).scalar_one_or_none()
    if policy is None:
        policy = TrafficPolicy(capability_id=capability_id, weights=after_config)
        db.add(policy)
    else:
        policy.weights = after_config


def decide_action(
    db: ScopedSession,
    action_id: int,
    decision: str,
    approver_sub: str,
    reason: str,
) -> Optional[EnforcementAction]:
    db = _require_exact_scoped_session(db)
    row = db.get(EnforcementAction, action_id)
    if row is None:
        return None
    row.status = "approved" if decision == "approve" else "rejected"
    row.approver_sub = approver_sub
    row.approved_at = datetime.now(timezone.utc)
    if reason:
        row.reason = reason

    policy = db.execute(
        select(PolicyAction)
        .where(PolicyAction.capability_id == row.capability_id)
        .order_by(PolicyAction.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if policy is not None:
        if decision == "approve":
            policy.status = "APPROVED"
            policy.approved_by = approver_sub
            policy.approved_at = datetime.now(timezone.utc)
            if row.action_type == "AdjustTrafficWeights":
                _apply_traffic_policy(db, row.capability_id, row.after_config)
            policy.status = "APPLIED"
            policy.applied_at = datetime.now(timezone.utc)
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
                            "approved_at": policy.approved_at.isoformat() if policy.approved_at else None,
                            "applied_at": policy.applied_at.isoformat() if policy.applied_at else None,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            policy.status = "REJECTED"
            policy.approved_by = approver_sub
            policy.approved_at = datetime.now(timezone.utc)
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
                            "approved_at": policy.approved_at.isoformat() if policy.approved_at else None,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass

    audit = AuditLog(
        actor_sub=approver_sub,
        action=f"enforcement_{decision}",
        entity_type="EnforcementAction",
        entity_id=str(action_id),
        payload={"before": row.before_config, "after": row.after_config, "decision_reason": reason},
        created_at=datetime.now(timezone.utc),
    )
    db.add(audit)
    db.commit()
    db.refresh(row)
    return row


def get_budget_status(db: ScopedSession, tenant_id: str) -> dict:
    """Month-to-date spend (execution_events cost columns) vs the tenant's cap."""
    db = _require_exact_scoped_session(db)
    from zeroth.econ.plane.instrumentation.models import ExecutionEvent

    now = datetime.now(timezone.utc)
    window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spend = db.execute(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(ExecutionEvent.token_cost_usd, 0)
                    + func.coalesce(ExecutionEvent.tool_cost_usd, 0)
                    + func.coalesce(ExecutionEvent.compute_cost_usd, 0)
                ),
                0,
            )
        ).where(
            ExecutionEvent.tenant_id == tenant_id,
            ExecutionEvent.timestamp >= window_start.replace(tzinfo=None),
        )
    ).scalar_one()
    measurements = set(
        db.execute(
            select(ExecutionEvent.cost_measurement).where(
                ExecutionEvent.tenant_id == tenant_id,
                ExecutionEvent.timestamp >= window_start.replace(tzinfo=None),
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
    return {
        "tenant_id": tenant_id,
        "total_cost_usd": float(spend or 0),
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
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row is None:
        row = TenantBudget(tenant_id=tenant_id, budget_cap_usd=budget_cap_usd, updated_at=now)
        db.add(row)
    else:
        row.budget_cap_usd = budget_cap_usd
        row.updated_at = now
    db.commit()
    db.refresh(row)
    return row
