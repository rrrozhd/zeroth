"""Transport contracts for bounded hosted backtests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BacktestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(min_length=1)
    expected: dict[str, Any] = Field(min_length=1)


class EconomicConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_success_rate: float | None = Field(default=None, ge=0, le=1)
    max_cost_per_outcome_usd: Decimal | None = Field(default=None, ge=0)
    max_critical_error_rate: float | None = Field(default=None, ge=0, le=1)


class BacktestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1)
    baseline_version: str | None = Field(default=None, min_length=1)
    node_id: str | None = Field(default=None, min_length=1)
    incumbent_model: str | None = Field(default=None, min_length=1)
    instruction: str | None = Field(default=None, min_length=1)
    candidate: dict[str, Any] = Field(min_length=1)
    cases: list[BacktestCase] = Field(default_factory=list, max_length=25)
    constraints: EconomicConstraints


class BacktestComputation(BaseModel):
    """Credential-free result returned by a provider-backed executor."""

    model_config = ConfigDict(extra="forbid")

    incumbent_success_rate: float | None = Field(default=None, ge=0, le=1)
    candidate_success_rate: float | None = Field(default=None, ge=0, le=1)
    candidate_error_rate: float | None = Field(default=None, ge=0, le=1)
    savings_pct: float | None = None
    provider_calls: int = Field(default=0, ge=0)
    reasons: list[str] = Field(default_factory=list)


class EconomicBacktest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backtest_id: str
    request_digest: str
    workflow: str
    baseline_version: str | None
    node_id: str | None
    incumbent_model: str | None
    candidate_model: str | None
    verdict: Literal["pass", "fail", "abstain"]
    recommended_action: Literal["approve_candidate", "keep_incumbent", "collect_evidence"]
    cases: int = Field(ge=0, le=25)
    provider_call_credits: int = Field(ge=0)
    incumbent_success_rate: float | None = None
    candidate_success_rate: float | None = None
    candidate_error_rate: float | None = None
    savings_pct: float | None = None
    constraints: EconomicConstraints
    reasons: list[str] = Field(default_factory=list)
    evaluated_at: datetime
