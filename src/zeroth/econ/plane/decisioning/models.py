"""Durable tenant-scoped economic decision history."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class EconomicDecisionRecord(Base):
    __tablename__ = "economic_decisions"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.economic_decision",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index("uq_economic_decisions_tenant_digest", "tenant_id", "evidence_digest", unique=True),
        Index(
            "ix_economic_decisions_tenant_workflow_time",
            "tenant_id",
            "workflow",
            "evaluated_at",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    baseline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_version: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_type: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recommended_action: Mapped[str] = mapped_column(String(32), nullable=False)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    evaluated_by: Mapped[str] = mapped_column(String(128), nullable=False)


class ProbabilisticMigrationDecisionRecord(Base):
    """Immutable evidence snapshot and risk-calibrated model migration decision."""

    __tablename__ = "probabilistic_migration_decisions"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.probabilistic_migration_decision",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index(
            "uq_probabilistic_migration_decisions_tenant_digest",
            "tenant_id",
            "request_digest",
            unique=True,
        ),
        Index(
            "ix_probabilistic_migration_decisions_tenant_workload_time",
            "tenant_id",
            "workload",
            "evaluated_at",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    workload: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    incumbent_model: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_model: Mapped[str] = mapped_column(String(255), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recommended_action: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_lineage_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    evaluated_by: Mapped[str] = mapped_column(String(128), nullable=False)


class DecisionSchedule(Base):
    __tablename__ = "decision_schedules"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.decision_schedule",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index(
            "ix_decision_schedules_tenant_due",
            "tenant_id",
            "active",
            "next_run_at",
        ),
    )

    schedule_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workflow: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    baseline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_version: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_type: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_decision_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class ProbabilisticDecisionSchedule(Base):
    """A fresh-evidence selector and policy, never a scheduled evidence snapshot."""

    __tablename__ = "probabilistic_decision_schedules"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.probabilistic_decision_schedule",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index(
            "ix_probabilistic_decision_schedules_tenant_due",
            "tenant_id",
            "active",
            "next_run_at",
        ),
    )

    schedule_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    evidence_source_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    simulations: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_decision_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class RandomizedRolloutRecord(Base):
    __tablename__ = "randomized_rollouts"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.randomized_rollout",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )

    rollout_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decision_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    workload: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    incumbent_model: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_model: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_probability: Mapped[float] = mapped_column(Float, nullable=False)
    cohort_probabilities_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    assignment_salt: Mapped[str] = mapped_column(String(128), nullable=False)
    minimum_per_arm: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class RandomizedRolloutAssignmentRecord(Base):
    __tablename__ = "randomized_rollout_assignments"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.randomized_rollout_assignment",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index(
            "uq_randomized_rollout_assignment_subject",
            "tenant_id",
            "rollout_id",
            "subject_id",
            unique=True,
        ),
    )

    assignment_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    rollout_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(192), nullable=False)
    cohort: Mapped[str] = mapped_column(String(128), nullable=False)
    arm: Mapped[str] = mapped_column(String(16), nullable=False)
    assigned_model: Mapped[str] = mapped_column(String(255), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RandomizedRolloutVerificationRecord(Base):
    __tablename__ = "randomized_rollout_verifications"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.randomized_rollout_verification",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )

    verification_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    rollout_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    verified_by: Mapped[str] = mapped_column(String(128), nullable=False)


class ForecastCalibrationRecord(Base):
    __tablename__ = "forecast_calibration_observations"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.forecast_calibration_observation",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index(
            "uq_forecast_calibration_verification_metric",
            "tenant_id",
            "verification_id",
            "metric",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    forecast_id: Mapped[str] = mapped_column(String(128), nullable=False)
    verification_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    workload: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    incumbent_model: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_model: Mapped[str] = mapped_column(String(255), nullable=False)
    metric: Mapped[str] = mapped_column(String(128), nullable=False)
    predicted_mean: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_low: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_high: Mapped[float] = mapped_column(Float, nullable=False)
    observed: Mapped[float] = mapped_column(Float, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
