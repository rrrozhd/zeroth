"""Settings sections composed by :class:`~zeroth.platform.config.settings.ZerothSettings`.

These models are configuration schema, not domain behavior. They live in the
platform layer so the unified settings model can compose them without the
config package importing the econ or integration domains; those domains read
the sections they need from here (every domain may depend on platform).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class RegulusSettings(BaseModel):
    """Regulus backend connection settings.

    Configured via environment variables with ZEROTH_REGULUS__ prefix
    when nested inside ZerothSettings.
    """

    # Default-ENABLED (G1): the bundled in-process economic control plane is
    # wired out of the box so per-tenant spend caps are enforced in a default
    # deploy with no env flags. A fresh deploy auto-generates a strong ephemeral
    # signing secret for the mount (see core/service/app.py lifespan). Point at an
    # external Regulus by setting ZEROTH_REGULUS__BASE_URL, or turn the plane off
    # with ZEROTH_REGULUS__ENABLED=false.
    enabled: bool = True
    base_url: str = "http://localhost:8000/v1"
    api_key: SecretStr | None = None
    budget_cache_ttl: int = 30  # seconds
    request_timeout: float = 5.0
    # Budget-check failure mode. Default fail-OPEN (D-12): a backend outage
    # never blocks production workloads (logged at WARNING). Set fail-CLOSED
    # to DENY when the control plane is unreachable — tighter governance at
    # the cost of availability. Applies ONLY to the error path; a 200 with a
    # null cap stays unlimited regardless.
    fail_closed: bool = False
    # Optional per-run cumulative cost ceiling (USD). Enforced locally from the
    # run's own audit cost_usd, so it works even with the control plane disabled.
    # Post-hoc: a run halts on the NEXT node once cumulative spend crosses the
    # cap. ``None`` disables the ceiling.
    per_run_cap_usd: float | None = None


class HttpClientSettings(BaseModel):
    """Global resilient-HTTP-client configuration.

    Every field has a sensible default so the client works out of the box.
    """

    model_config = ConfigDict(extra="forbid")

    max_retries: int = 3
    retry_backoff_base: float = 0.5
    retry_max_delay: float = 60.0
    retryable_status_codes: set[int] = Field(
        default_factory=lambda: {408, 429, 500, 502, 503, 504},
    )
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset_timeout: float = 30.0
    pool_max_connections: int = 100
    pool_max_keepalive: int = 20
    pool_keepalive_expiry: float = 5.0
    default_timeout: float = 30.0
    default_rate_limit_rate: float = 10.0
    default_rate_limit_burst: int = 20
