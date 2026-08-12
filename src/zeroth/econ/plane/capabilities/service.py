from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from sqlalchemy import delete, select

from zeroth.econ.plane.capabilities.models import (
    Capability,
    Deployment,
    DeploymentImplementation,
    Experiment,
    Implementation,
)
from zeroth.econ.plane.capabilities.schemas import CapabilityCreate, DeploymentCreate, ExperimentCreate, ImplementationCreate
from zeroth.econ.plane.common.tenant import resolve_tenant_id
from zeroth.econ.plane.scoped_session import ScopedSession


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("capability persistence requires an exact ScopedSession")
    return db


def _normalize_category(value: str | None) -> str:
    mapping = {
        "Revenue": "REVENUE",
        "CostReduction": "COST",
        "RiskMitigation": "RISK",
        "Productivity": "PRODUCTIVITY",
    }
    if not value:
        return "RISK"
    return mapping.get(value, value)


def _legacy_category(capability_type: str) -> str:
    mapping = {
        "REVENUE": "Revenue",
        "COST": "CostReduction",
        "RISK": "RiskMitigation",
        "PRODUCTIVITY": "Productivity",
    }
    return mapping.get(capability_type, "RiskMitigation")


def create_capability(db: ScopedSession, payload: CapabilityCreate) -> Capability:
    db = _require_exact_scoped_session(db)
    tenant_id = resolve_tenant_id(payload.tenant_id)
    capability_type = _normalize_category(payload.type or payload.category)
    existing = db.execute(select(Capability).where(Capability.id == payload.id)).scalar_one_or_none()
    if existing is not None:
        existing.tenant_id = tenant_id
        existing.name = payload.name
        existing.capability_type = capability_type
        existing.legacy_category = _legacy_category(capability_type)
        existing.description = payload.description
        existing.criticality = payload.criticality
        existing.is_protected = payload.is_protected
        existing.valuation_config = payload.valuation_config.model_dump()
        db.commit()
        db.refresh(existing)
        return existing

    row = Capability(
        id=payload.id,
        tenant_id=tenant_id,
        name=payload.name,
        legacy_category=_legacy_category(capability_type),
        capability_type=capability_type,
        description=payload.description,
        criticality=payload.criticality,
        is_protected=payload.is_protected,
        valuation_config=payload.valuation_config.model_dump(),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_implementation(db: ScopedSession, capability_id: str, payload: ImplementationCreate) -> Implementation:
    db = _require_exact_scoped_session(db)
    tenant_id = resolve_tenant_id(payload.tenant_id)
    existing = db.execute(select(Implementation).where(Implementation.id == payload.id)).scalar_one_or_none()
    if existing is not None:
        existing.tenant_id = tenant_id
        existing.capability_id = capability_id
        existing.name = payload.name
        existing.provider = payload.provider
        existing.model_name = payload.model_name
        existing.model_version_hash = payload.model_version_hash
        existing.prompt_version_hash = payload.prompt_version_hash
        existing.pipeline_version_hash = payload.pipeline_version_hash
        existing.config_json = payload.config_json
        existing.status = payload.status
        existing.model_version = payload.model_version
        existing.created_at = payload.created_at
        db.commit()
        db.refresh(existing)
        return existing

    row = Implementation(
        id=payload.id,
        tenant_id=tenant_id,
        capability_id=capability_id,
        name=payload.name,
        provider=payload.provider,
        model_name=payload.model_name,
        model_version_hash=payload.model_version_hash,
        prompt_version_hash=payload.prompt_version_hash,
        pipeline_version_hash=payload.pipeline_version_hash,
        config_json=payload.config_json,
        status=payload.status,
        model_version=payload.model_version,
        created_at=payload.created_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_capabilities(db: ScopedSession, tenant_id: str | None = None) -> list[Capability]:
    db = _require_exact_scoped_session(db)
    stmt = select(Capability)
    if tenant_id:
        stmt = stmt.where(Capability.tenant_id == tenant_id)
    return list(db.execute(stmt).scalars().all())


def get_capability(db: ScopedSession, capability_id: str) -> Optional[Capability]:
    db = _require_exact_scoped_session(db)
    return db.execute(select(Capability).where(Capability.id == capability_id)).scalar_one_or_none()


def get_implementation(db: ScopedSession, implementation_id: str) -> Optional[Implementation]:
    db = _require_exact_scoped_session(db)
    return db.execute(select(Implementation).where(Implementation.id == implementation_id)).scalar_one_or_none()


def create_deployment(db: ScopedSession, payload: DeploymentCreate) -> Deployment:
    db = _require_exact_scoped_session(db)
    tenant_id = resolve_tenant_id(payload.tenant_id)
    existing = db.execute(select(Deployment).where(Deployment.capability_id == payload.capability_id)).scalar_one_or_none()
    if existing is None:
        existing = Deployment(
            tenant_id=tenant_id,
            capability_id=payload.capability_id,
            weights_json=payload.weights_json,
            routing_mode=payload.routing_mode,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(existing)
        db.flush()
    else:
        existing.tenant_id = tenant_id
        existing.weights_json = payload.weights_json
        existing.routing_mode = payload.routing_mode
        existing.updated_at = datetime.now(timezone.utc)
        db.execute(select(DeploymentImplementation).where(DeploymentImplementation.deployment_id == existing.id))

    db.execute(
        delete(DeploymentImplementation).where(
            DeploymentImplementation.deployment_id == existing.id
        )
    )
    for implementation_id in payload.active_impl_ids:
        db.add(DeploymentImplementation(deployment_id=existing.id, implementation_id=implementation_id))

    db.commit()
    db.refresh(existing)
    return existing


def get_deployment(db: ScopedSession, capability_id: str) -> Optional[Deployment]:
    db = _require_exact_scoped_session(db)
    return db.execute(select(Deployment).where(Deployment.capability_id == capability_id)).scalar_one_or_none()


def deployment_active_impl_ids(db: ScopedSession, deployment_id: int) -> list[str]:
    db = _require_exact_scoped_session(db)
    rows = db.execute(
        select(DeploymentImplementation).where(DeploymentImplementation.deployment_id == deployment_id)
    ).scalars()
    return [r.implementation_id for r in rows]


def create_experiment(db: ScopedSession, payload: ExperimentCreate) -> Experiment:
    db = _require_exact_scoped_session(db)
    tenant_id = resolve_tenant_id(payload.tenant_id)
    row = Experiment(
        tenant_id=tenant_id,
        capability_id=payload.capability_id,
        mode=payload.mode,
        impl_a_id=payload.impl_a_id,
        impl_b_id=payload.impl_b_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        target_pct=payload.target_pct,
        assignment_key=payload.assignment_key,
        status=payload.status,
        constraints_json=payload.constraints_json,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_experiments(db: ScopedSession, capability_id: str) -> list[Experiment]:
    db = _require_exact_scoped_session(db)
    return list(db.execute(select(Experiment).where(Experiment.capability_id == capability_id)).scalars())


def pick_ab_arm(join_key: str, target_pct: float) -> str:
    digest = sha256(join_key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return "A" if bucket < int(target_pct) else "B"


def active_experiment(
    db: ScopedSession, capability_id: str, mode: str = "AB"
) -> Optional[Experiment]:
    db = _require_exact_scoped_session(db)
    stmt = (
        select(Experiment)
        .where(Experiment.capability_id == capability_id)
        .where(Experiment.mode == mode)
        .where(Experiment.status == "ACTIVE")
        .order_by(Experiment.id.desc())
        .limit(1)
    )
    # Nothing enforces a single ACTIVE experiment per (capability, mode), so cap
    # at the newest row. scalar_one_or_none() would raise MultipleResultsFound —
    # 500ing every execution ingest — the moment a second ACTIVE row exists (B7).
    return db.execute(stmt).scalars().first()
