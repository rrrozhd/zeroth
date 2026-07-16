from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from zeroth.econ_plane.auth.deps import require_roles
from zeroth.econ_plane.auth.schemas import UserClaims
from zeroth.econ_plane.costing.schemas import CostEstimateOut, CostProfileCreate, CostProfileOut, PricingCatalogCreate
from zeroth.econ_plane.costing.service import (
    create_cost_profile,
    create_pricing_catalog,
    get_cost_profile,
    latest_cost_estimate,
)
from zeroth.econ_plane.database import get_db

router = APIRouter(tags=["costing"])

# Audit F7: these routes previously carried NO auth dependency, unlike every
# sibling econ router — leaving the pricing catalog + cost profiles that drive
# every cost estimate writable by any caller that reaches the router (anonymous
# in a standalone deploy; any authenticated principal via the mount). Writes are
# now Admin-only; reads require any valid role, matching dashboard/reconciliation.
_READ_ROLES = require_roles("Admin", "Analyst", "Approver", "Viewer")
_WRITE_ROLES = require_roles("Admin")


@router.post("/costing/profiles", response_model=CostProfileOut)
def post_profile(
    payload: CostProfileCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(_WRITE_ROLES),
) -> CostProfileOut:
    row = create_cost_profile(db, payload)
    return CostProfileOut.model_validate(row)


@router.get("/costing/profiles/{profile_id}", response_model=CostProfileOut)
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(_READ_ROLES),
) -> CostProfileOut:
    row = get_cost_profile(db, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Cost profile not found")
    return CostProfileOut.model_validate(row)


@router.post("/costing/pricing-catalog")
def post_pricing(
    payload: PricingCatalogCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(_WRITE_ROLES),
) -> dict[str, int]:
    row = create_pricing_catalog(db, payload)
    return {"id": row.id}


@router.get("/costing/estimates/{capability_id}/latest", response_model=CostEstimateOut)
def get_latest_estimate(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(_READ_ROLES),
) -> CostEstimateOut:
    row = latest_cost_estimate(db, capability_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No cost estimate found")
    return CostEstimateOut.model_validate(row)
