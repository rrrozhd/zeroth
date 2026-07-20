from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel, Field


class InstrumentationConfig(BaseModel):
    base_url: str = "http://localhost:8000/v1"
    enabled: bool = True
    buffer_max_events: int = 1000
    flush_interval_ms: int = 500
    request_timeout_s: float = 5.0
    capture_content: bool = False
    enable_otel: bool = False
    default_capability_id: Optional[str] = None
    default_implementation_id: Optional[str] = None
    max_retries: int = 5

    @classmethod
    def from_env(cls) -> "InstrumentationConfig":
        return cls(
            base_url=os.getenv("ECP_BASE_URL", "http://localhost:8000/v1"),
            enabled=os.getenv("ECP_ENABLED", "true").lower() == "true",
            buffer_max_events=int(os.getenv("ECP_BUFFER_MAX_EVENTS", "1000")),
            flush_interval_ms=int(os.getenv("ECP_FLUSH_INTERVAL_MS", "500")),
            request_timeout_s=float(os.getenv("ECP_REQUEST_TIMEOUT_S", "5.0")),
            capture_content=os.getenv("ECP_CAPTURE_CONTENT", "false").lower() == "true",
            enable_otel=os.getenv("ECP_ENABLE_OTEL", "false").lower() == "true",
            default_capability_id=os.getenv("ECP_DEFAULT_CAPABILITY_ID") or None,
            default_implementation_id=os.getenv("ECP_DEFAULT_IMPLEMENTATION_ID") or None,
            max_retries=int(os.getenv("ECP_MAX_RETRIES", "5")),
        )


class ExecutionCostBreakdown(BaseModel):
    token_cost_usd: float = 0.0
    tool_cost_usd: float = 0.0
    compute_cost_usd: float = 0.0


class LibraryContext(BaseModel):
    provider: str
    library: str
    model_name: str
    is_async: bool
    tags: dict[str, str] = Field(default_factory=dict)


class CostProfileInput(BaseModel):
    requests_per_period: int
    avg_input_tokens: float
    avg_output_tokens: float
    model_mix_json: dict = Field(default_factory=dict)
    provider_rate_overrides_json: dict = Field(default_factory=dict)
    avg_tool_calls_per_request_json: dict = Field(default_factory=dict)
    tool_unit_price_overrides_json: dict = Field(default_factory=dict)
    infra_monthly_spend_usd: float = 0.0
    retry_rate: float = 0.0
    cache_hit_rate: float = 0.0


class AutoInstrumentationConfig(BaseModel):
    capture_mode: Literal["hierarchical", "all_layers", "provider_only"] = "hierarchical"
    instrument_langchain: bool = True
    instrument_langgraph: bool = True
    instrument_openai: bool = True
    instrument_anthropic: bool = True
    provider_fallback_only: bool = True
    capture_streaming: bool = True
    include_tool_calls: bool = True
    include_retriever_calls: bool = True
    max_event_rate_per_sec: int = 200
    dedupe_window_ms: int = 2000
    strict_join_key: bool = True
