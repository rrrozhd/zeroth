from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.deps import require_roles
from zeroth.econ.plane.auth.schemas import UserClaims
from zeroth.econ.plane.capabilities.schemas import (
    CapabilityCreate,
    CapabilityDetail,
    CapabilityOut,
    DeploymentCreate,
    DeploymentOut,
    ExperimentCreate,
    ExperimentOut,
    ImplementationCreate,
    ImplementationOut,
)
from zeroth.econ.plane.capabilities.service import (
    create_capability,
    create_deployment,
    create_experiment,
    create_implementation,
    deployment_active_impl_ids,
    get_capability,
    get_deployment,
    get_implementation,
    list_capabilities,
    list_experiments,
)
from zeroth.econ.plane.database import get_db

router = APIRouter(tags=["capabilities", "registry"])


def _cap_out(row: object) -> CapabilityOut:
    data = {
        "id": getattr(row, "id"),
        "tenant_id": getattr(row, "tenant_id", "tenant_default"),
        "name": getattr(row, "name"),
        "type": getattr(row, "capability_type", None) or getattr(row, "type", "RISK"),
        "description": getattr(row, "description", "") or "",
        "criticality": getattr(row, "criticality", "MED") or "MED",
        "is_protected": bool(getattr(row, "is_protected", False)),
        "valuation_config": getattr(row, "valuation_config", {}),
        "created_at": getattr(row, "created_at", None) or __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    }
    return CapabilityOut.model_validate(data)


def _impl_out(row: object) -> ImplementationOut:
    data = {
        "id": getattr(row, "id"),
        "tenant_id": getattr(row, "tenant_id", "tenant_default"),
        "capability_id": getattr(row, "capability_id"),
        "name": getattr(row, "name"),
        "provider": getattr(row, "provider", "custom"),
        "model_name": getattr(row, "model_name", ""),
        "model_version_hash": getattr(row, "model_version_hash", ""),
        "prompt_version_hash": getattr(row, "prompt_version_hash", ""),
        "pipeline_version_hash": getattr(row, "pipeline_version_hash", ""),
        "config_json": getattr(row, "config_json", {}) or {},
        "status": getattr(row, "status", "ACTIVE"),
        "model_version": getattr(row, "model_version", ""),
        "created_at": getattr(row, "created_at", None) or __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    }
    return ImplementationOut.model_validate(data)


@router.post("/capabilities", response_model=CapabilityOut)
def post_capability(
    payload: CapabilityCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> CapabilityOut:
    row = create_capability(db, payload)
    return _cap_out(row)


@router.post("/capabilities/{capability_id}/implementations", response_model=ImplementationOut)
def post_implementation(
    capability_id: str,
    payload: ImplementationCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> ImplementationOut:
    capability = get_capability(db, capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    row = create_implementation(db, capability_id, payload)
    return _impl_out(row)


@router.get("/capabilities", response_model=list[CapabilityOut])
def get_capabilities(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[CapabilityOut]:
    return [_cap_out(c) for c in list_capabilities(db)]


@router.get("/capabilities/{capability_id}", response_model=CapabilityDetail)
def get_capability_detail(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> CapabilityDetail:
    capability = get_capability(db, capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    data = _cap_out(capability).model_dump()
    data["implementations"] = [_impl_out(i).model_dump() for i in getattr(capability, "implementations", [])]
    return CapabilityDetail.model_validate(data)


@router.post("/registry/capabilities", response_model=CapabilityOut)
def post_registry_capability(
    payload: CapabilityCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> CapabilityOut:
    return post_capability(payload, db)


@router.get("/registry/capabilities", response_model=list[CapabilityOut])
def get_registry_capabilities(
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[CapabilityOut]:
    return get_capabilities(db)


@router.get("/registry/capabilities/{capability_id}", response_model=CapabilityDetail)
def get_registry_capability(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> CapabilityDetail:
    return get_capability_detail(capability_id, db)


@router.post("/registry/implementations", response_model=ImplementationOut)
def post_registry_implementation(
    payload: ImplementationCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> ImplementationOut:
    if not payload.capability_id:
        raise HTTPException(status_code=422, detail="capability_id is required")
    return post_implementation(payload.capability_id, payload, db)


@router.get("/registry/implementations/{implementation_id}", response_model=ImplementationOut)
def get_registry_implementation(
    implementation_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> ImplementationOut:
    implementation = get_implementation(db, implementation_id)
    if implementation is None:
        raise HTTPException(status_code=404, detail="Implementation not found")
    return _impl_out(implementation)


@router.post("/registry/deployments", response_model=DeploymentOut)
def post_registry_deployment(
    payload: DeploymentCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> DeploymentOut:
    row = create_deployment(db, payload)
    return DeploymentOut(
        id=row.id,
        tenant_id=row.tenant_id,
        capability_id=row.capability_id,
        active_impl_ids=deployment_active_impl_ids(db, row.id),
        weights_json=row.weights_json,
        routing_mode=row.routing_mode,
        updated_at=row.updated_at,
    )


@router.get("/registry/deployments/{capability_id}", response_model=DeploymentOut)
def get_registry_deployment(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> DeploymentOut:
    row = get_deployment(db, capability_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return DeploymentOut(
        id=row.id,
        tenant_id=row.tenant_id,
        capability_id=row.capability_id,
        active_impl_ids=deployment_active_impl_ids(db, row.id),
        weights_json=row.weights_json,
        routing_mode=row.routing_mode,
        updated_at=row.updated_at,
    )


@router.post("/registry/experiments", response_model=ExperimentOut)
def post_registry_experiment(
    payload: ExperimentCreate,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> ExperimentOut:
    row = create_experiment(db, payload)
    return ExperimentOut.model_validate(row)


@router.get("/registry/experiments/{capability_id}", response_model=list[ExperimentOut])
def get_registry_experiments(
    capability_id: str,
    db: Session = Depends(get_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[ExperimentOut]:
    return [ExperimentOut.model_validate(r) for r in list_experiments(db, capability_id)]
