from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from zeroth.econ.plane.auth.deps import (
    get_current_scoped_db,
    require_claimed_tenant,
    require_roles,
)
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.enforcement.schemas import BudgetStatusOut, DecisionRequest, EnforcementActionCreate, EnforcementActionOut, PolicyActionOut, TenantBudgetUpsert
from zeroth.econ.plane.enforcement.service import create_action, decide_action, get_budget_status, list_actions, list_policy_actions, upsert_tenant_budget

router = APIRouter(tags=["enforcement", "policy"])


@router.post("/enforcement/actions", response_model=EnforcementActionOut)
def create(
    payload: EnforcementActionCreate,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> EnforcementActionOut:
    row = create_action(db, payload)
    return EnforcementActionOut.model_validate(row)


@router.get("/enforcement/actions", response_model=list[EnforcementActionOut])
def list_all(
    status: Optional[str] = None,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[EnforcementActionOut]:
    return [EnforcementActionOut.model_validate(r) for r in list_actions(db, status)]


@router.get("/enforcement/policy-actions", response_model=list[PolicyActionOut])
def list_policies(
    status: Optional[str] = None,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[PolicyActionOut]:
    return [PolicyActionOut.model_validate(r) for r in list_policy_actions(db, status)]


@router.post("/enforcement/actions/{action_id}/approve", response_model=EnforcementActionOut)
def approve(
    action_id: int,
    payload: DecisionRequest,
    db: ScopedSession = Depends(get_current_scoped_db),
    user: UserClaims = Depends(require_roles("Approver", "Admin")),
) -> EnforcementActionOut:
    row = decide_action(db, action_id, "approve", user.sub, payload.reason)
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return EnforcementActionOut.model_validate(row)


@router.post("/enforcement/actions/{action_id}/reject", response_model=EnforcementActionOut)
def reject(
    action_id: int,
    payload: DecisionRequest,
    db: ScopedSession = Depends(get_current_scoped_db),
    user: UserClaims = Depends(require_roles("Approver", "Admin")),
) -> EnforcementActionOut:
    row = decide_action(db, action_id, "reject", user.sub, payload.reason)
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return EnforcementActionOut.model_validate(row)


@router.get("/budget/status", response_model=BudgetStatusOut)
def budget_status(
    tenant_id: str,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> BudgetStatusOut:
    claimed_tenant = require_claimed_tenant(_user, tenant_id)
    return BudgetStatusOut(**get_budget_status(db, claimed_tenant))


@router.put("/budget/tenants/{tenant_id}", response_model=BudgetStatusOut)
def set_tenant_budget(
    tenant_id: str,
    payload: TenantBudgetUpsert,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> BudgetStatusOut:
    claimed_tenant = require_claimed_tenant(_user, tenant_id)
    upsert_tenant_budget(db, claimed_tenant, payload.budget_cap_usd)
    return BudgetStatusOut(**get_budget_status(db, claimed_tenant))
