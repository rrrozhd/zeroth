from __future__ import annotations  # noqa: I001


from fastapi import APIRouter, Depends, HTTPException

from zeroth.econ.plane.auth.deps import (
    get_current_scoped_db,
    require_claimed_tenant,
    require_roles,
)
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.enforcement.models import EnforcementAction
from zeroth.econ.plane.enforcement.schemas import (
    BudgetStatusOut,
    DecisionRequest,
    EnforcementActionCreate,
    EnforcementActionOut,
    PolicyActionEffect,
    PolicyActionOut,
    TenantBudgetUpsert,
)
from zeroth.econ.plane.enforcement.service import (
    _linked_policy_action,
    create_action,
    decide_action,
    get_budget_status,
    list_actions,
    list_policy_actions,
    upsert_tenant_budget,
)
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["enforcement", "policy"])


def _decided(db: ScopedSession, row: EnforcementAction) -> EnforcementActionOut:
    """Render a decided action, stating whether the decision reached a policy action.

    ``decide_action`` decides an unlinked legacy action without transitioning any
    policy action or applying any traffic policy -- an audited skip that is correct
    (refusing would make pre-link actions impossible to *reject*) but, until this
    field existed, indistinguishable at the boundary from a decision that took
    effect.  The approver saw ``200 {"status": "approved"}`` either way.

    The linkage predicate is imported from the service rather than re-queried here:
    a second definition of "the policy action belonging to this enforcement action"
    could drift from the one ``decide_action`` acted on, and the response would then
    report an effect that never happened -- the defect class A01-11 removed.  The
    read runs after ``decide_action`` has committed, so it observes the same rows.
    """
    effect: PolicyActionEffect = (
        "applied" if _linked_policy_action(db, row) is not None else "not_applied"
    )
    return EnforcementActionOut.model_validate(row).model_copy(
        update={"policy_action_effect": effect}
    )


@router.post("/enforcement/actions", response_model=EnforcementActionOut)
def create(
    payload: EnforcementActionCreate,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin", "Analyst")),  # noqa: B008
) -> EnforcementActionOut:
    row = create_action(db, payload)
    return EnforcementActionOut.model_validate(row)


@router.get("/enforcement/actions", response_model=list[EnforcementActionOut])
def list_all(
    status: str | None = None,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),  # noqa: B008
) -> list[EnforcementActionOut]:
    return [EnforcementActionOut.model_validate(r) for r in list_actions(db, status)]


@router.get("/enforcement/policy-actions", response_model=list[PolicyActionOut])
def list_policies(
    status: str | None = None,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),  # noqa: B008
) -> list[PolicyActionOut]:
    return [PolicyActionOut.model_validate(r) for r in list_policy_actions(db, status)]


@router.post("/enforcement/actions/{action_id}/approve", response_model=EnforcementActionOut)
def approve(
    action_id: int,
    payload: DecisionRequest,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_roles("Approver", "Admin")),  # noqa: B008
) -> EnforcementActionOut:
    row = decide_action(db, action_id, "approve", user.sub, payload.reason)
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return _decided(db, row)


@router.post("/enforcement/actions/{action_id}/reject", response_model=EnforcementActionOut)
def reject(
    action_id: int,
    payload: DecisionRequest,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_roles("Approver", "Admin")),  # noqa: B008
) -> EnforcementActionOut:
    row = decide_action(db, action_id, "reject", user.sub, payload.reason)
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return _decided(db, row)


@router.get("/budget/status", response_model=BudgetStatusOut)
def budget_status(
    tenant_id: str,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),  # noqa: B008
) -> BudgetStatusOut:
    claimed_tenant = require_claimed_tenant(_user, tenant_id)
    return BudgetStatusOut(**get_budget_status(db, claimed_tenant))


@router.put("/budget/tenants/{tenant_id}", response_model=BudgetStatusOut)
def set_tenant_budget(
    tenant_id: str,
    payload: TenantBudgetUpsert,
    db: ScopedSession = Depends(get_current_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(require_roles("Admin")),  # noqa: B008
) -> BudgetStatusOut:
    claimed_tenant = require_claimed_tenant(_user, tenant_id)
    upsert_tenant_budget(db, claimed_tenant, payload.budget_cap_usd)
    return BudgetStatusOut(**get_budget_status(db, claimed_tenant))
