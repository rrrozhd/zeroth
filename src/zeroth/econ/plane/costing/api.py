from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from zeroth.econ.plane.costing.schemas import CostEstimateOut, CostProfileCreate, CostProfileOut, PricingCatalogCreate
from zeroth.econ.plane.costing.service import (
    create_cost_profile,
    create_pricing_catalog,
    get_cost_profile,
    latest_cost_estimate,
)
from zeroth.econ.plane.database import get_db

router = APIRouter(tags=["costing"])


@router.post("/costing/profiles", response_model=CostProfileOut)
def post_profile(payload: CostProfileCreate, db: Session = Depends(get_db)) -> CostProfileOut:
    row = create_cost_profile(db, payload)
    return CostProfileOut.model_validate(row)


@router.get("/costing/profiles/{profile_id}", response_model=CostProfileOut)
def get_profile(profile_id: int, db: Session = Depends(get_db)) -> CostProfileOut:
    row = get_cost_profile(db, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Cost profile not found")
    return CostProfileOut.model_validate(row)


@router.post("/costing/pricing-catalog")
def post_pricing(payload: PricingCatalogCreate, db: Session = Depends(get_db)) -> dict[str, int]:
    row = create_pricing_catalog(db, payload)
    return {"id": row.id}


@router.get("/costing/estimates/{capability_id}/latest", response_model=CostEstimateOut)
def get_latest_estimate(capability_id: str, db: Session = Depends(get_db)) -> CostEstimateOut:
    row = latest_cost_estimate(db, capability_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No cost estimate found")
    return CostEstimateOut.model_validate(row)
