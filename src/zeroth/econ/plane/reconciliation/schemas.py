from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


class GroundTruthCostIn(BaseModel):
    period_start: datetime
    period_end: datetime
    capability_id: str
    component: str
    amount_usd: float


class GroundTruthImportRequest(BaseModel):
    rows: list[GroundTruthCostIn]


class ProviderCostBucketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_id: str = Field(min_length=1, max_length=192)
    period_start: datetime
    period_end: datetime
    amount_usd: Decimal = Field(gt=0, decimal_places=8, max_digits=18)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    provider_dimensions: dict[str, str] = Field(default_factory=dict)

    @field_validator("provider_dimensions")
    @classmethod
    def validate_provider_dimensions(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 8:
            raise ValueError("provider_dimensions may contain at most 8 entries")
        for key, item in value.items():
            if (
                not key
                or len(key) > 64
                or not key.replace("_", "").replace("-", "").replace(".", "").isalnum()
            ):
                raise ValueError(
                    "provider dimension keys must be 1-64 letters, digits, '.', '_' or '-'"
                )
            if not item or len(item) > 256:
                raise ValueError("provider dimension values must contain 1-256 characters")
        return value

    @model_validator(mode="after")
    def validate_period(self) -> ProviderCostBucketIn:
        self.period_start = _utc(self.period_start, field="bucket period_start")
        self.period_end = _utc(self.period_end, field="bucket period_end")
        if self.period_start >= self.period_end:
            raise ValueError("bucket period_start must be before period_end")
        return self


class ProviderBillImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str = Field(
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    period_start: datetime
    period_end: datetime
    currency: Literal["USD"] = "USD"
    billed_total_usd: Decimal = Field(gt=0, decimal_places=8, max_digits=18)
    source_kind: Literal["cost_api", "csv_export", "invoice_export", "manual"]
    buckets: list[ProviderCostBucketIn] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_statement(self) -> ProviderBillImportRequest:
        self.period_start = _utc(self.period_start, field="statement period_start")
        self.period_end = _utc(self.period_end, field="statement period_end")
        if self.period_start >= self.period_end:
            raise ValueError("statement period_start must be before period_end")
        bucket_ids = [bucket.bucket_id for bucket in self.buckets]
        if len(bucket_ids) != len(set(bucket_ids)):
            raise ValueError("bucket_id must be unique within a provider bill")
        if any(
            bucket.period_start < self.period_start or bucket.period_end > self.period_end
            for bucket in self.buckets
        ):
            raise ValueError("bucket periods must be contained by the statement period")
        if sum((bucket.amount_usd for bucket in self.buckets), Decimal("0")) != (
            self.billed_total_usd
        ):
            raise ValueError("billed_total_usd must equal the sum of buckets")
        return self


class ProviderBillOut(BaseModel):
    statement_id: str
    provider: str
    period_start: datetime
    period_end: datetime
    currency: Literal["USD"]
    billed_total_usd: Decimal
    source_kind: Literal["cost_api", "csv_export", "invoice_export", "manual"]
    bucket_count: int
    statement_digest: str
    imported_at: datetime


class UnmatchedProviderBucket(BaseModel):
    bucket_id: str
    reason: Literal["no_measured_telemetry", "ambiguous_bucket_scope"]


class ProviderBillAllocation(BaseModel):
    bucket_id: str
    model: str | None
    provider_dimensions: dict[str, str]
    workflow_id: str
    workflow_version: str
    outcome_status: Literal["success", "failure", "unresolved"]
    billed_cost_usd: Decimal
    telemetry_cost_usd: Decimal
    run_count: int
    event_count: int


class ProviderBillReport(BaseModel):
    statement_id: str
    provider: str
    statement_digest: str
    period_start: datetime
    period_end: datetime
    currency: Literal["USD"]
    reconciliation_state: Literal[
        "reconciled", "allocated_with_variance", "outcomes_unresolved", "unreconciled"
    ]
    billed_total_usd: Decimal
    allocated_billed_usd: Decimal
    unreconciled_billed_usd: Decimal
    telemetry_measured_usd: Decimal
    telemetry_variance_usd: Decimal
    unbilled_telemetry_usd: Decimal
    outcome_unresolved_usd: Decimal
    matched_buckets: int
    unmatched_buckets: list[UnmatchedProviderBucket]
    allocations: list[ProviderBillAllocation]
    allocation_method: Literal["measured_cost_proportional"]
    limitations: list[str]
