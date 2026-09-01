"""API contracts for economic workflow-version decisions."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from zeroth.econ.decisioning import DecisionPolicy


class VersionComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    outcome_type: str = Field(default="accepted", min_length=1)
    policy: DecisionPolicy = Field(default_factory=DecisionPolicy)


class DecisionScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    outcome_type: str = Field(default="accepted", min_length=1)
    policy: DecisionPolicy = Field(default_factory=DecisionPolicy)
    interval_minutes: int = Field(default=1440, ge=60, le=43_200)


class DecisionScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schedule_id: str
    workflow: str
    baseline_version: str
    candidate_version: str
    outcome_type: str
    policy: DecisionPolicy
    interval_minutes: int
    active: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_decision_id: str | None
    last_error: str | None
    created_at: datetime
