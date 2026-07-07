from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, Field


class ExecutionEvent(BaseModel):
    execution_id: str = Field(default_factory=lambda: f"exec_{uuid4().hex}")
    join_key: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    capability_id: str
    implementation_id: str
    model_version: str = "unknown"
    token_cost_usd: Decimal = Decimal("0")
    tool_cost_usd: Decimal = Decimal("0")
    compute_cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    compute_time_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutcomeEvent(BaseModel):
    execution_id: str
    join_key: str | None = None
    capability_id: str
    outcome_type: Literal["conversion", "fraud_flag", "approval", "custom"]
    outcome_value: Union[float, bool, str]
    outcome_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
