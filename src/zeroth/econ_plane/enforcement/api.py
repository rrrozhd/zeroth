from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from zeroth.econ_plane.auth.deps import require_roles
from zeroth.econ_plane.auth.schemas import UserClaims
from zeroth.econ_plane.database import get_db
from zeroth.econ_plane.enforcement.schemas import BudgetStatusOut, DecisionRequest, EnforcementActionCreate, EnforcementActionOut, PolicyActionOut, TenantBudgetUpsert
from zeroth.econ_plane.enforcement.service import create_action, decide_action, get_budget_status, list_actions, list_policy_actions, upsert_tenant_budget

router = APIRouter(tags=["enforcement", "policy"])


@router.post("/enforcement/actions", response_model=EnforcementActionOut)
def create(
    payload: EnforcementActionCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),
) -> EnforcementActionOut:
    row = create_action(db, payload)
    return EnforcementActionOut.model_validate(row)


@router.get("/enforcement/actions", response_model=list[EnforcementActionOut])
def list_all(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[EnforcementActionOut]:
    return [EnforcementActionOut.model_validate(r) for r in list_actions(db, status)]


@router.get("/enforcement/policy-actions", response_model=list[PolicyActionOut])
def list_policies(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[PolicyActionOut]:
    return [PolicyActionOut.model_validate(r) for r in list_policy_actions(db, status)]


@router.post("/enforcement/actions/{action_id}/approve", response_model=EnforcementActionOut)
def approve(
    action_id: int,
    payload: DecisionRequest,
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
    user: UserClaims = Depends(require_roles("Approver", "Admin")),
) -> EnforcementActionOut:
    row = decide_action(db, action_id, "reject", user.sub, payload.reason)
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return EnforcementActionOut.model_validate(row)


@router.get("/budget/status", response_model=BudgetStatusOut)
def budget_status(
    tenant_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> BudgetStatusOut:
    return BudgetStatusOut(**get_budget_status(db, tenant_id))


@router.put("/budget/tenants/{tenant_id}", response_model=BudgetStatusOut)
def set_tenant_budget(
    tenant_id: str,
    payload: TenantBudgetUpsert,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> BudgetStatusOut:
    upsert_tenant_budget(db, tenant_id, payload.budget_cap_usd)
    return BudgetStatusOut(**get_budget_status(db, tenant_id))
