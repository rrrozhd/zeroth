"""Fail-closed HTTP adapter for the real measured Rightsizing endpoint.

The adapter owns only transport and evidence correlation. It never resolves an
LLM credential, infers internal replay/judge roles, or retains the service API
key supplied for one request.
"""

from __future__ import annotations

import ipaddress
import json as json_module
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .reconciliation import ReconciliationInput
from .rightsizing_live_checkpoint import (
    ARM_PHRASE,
    CAMPAIGN_CAP_USD,
    PER_RUN_CAP_USD,
    ServiceRightsizingCapture,
    capture_service_response,
    validate_service_capture,
)

_ROUTE = "/v1/econ/rightsizing/experiment"


def _opaque(value: str, field: str) -> None:
    if not value or len(value) > 512 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError(f"{field} must be a nonblank opaque identifier")


def _integer_range(value: int, field: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")


def _money(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a finite nonnegative Decimal")


@dataclass(frozen=True, slots=True)
class ExperimentRequest:
    """The exact public ``ExperimentRequest`` body, including explicit defaults."""

    node_id: str
    incumbent: str
    instruction: str
    needs_tools: bool = False
    needs_vision: bool = False
    judge_model: str | None = None
    max_candidates: int = 2
    max_cases: int = 5
    min_cases: int = 5
    tolerance_pct: float = 5.0
    mode: Literal["equivalence", "correctness"] = "equivalence"

    def __post_init__(self) -> None:
        for value, field in (
            (self.node_id, "node_id"),
            (self.incumbent, "incumbent"),
        ):
            _opaque(value, field)
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("instruction must be nonblank")
        if not isinstance(self.needs_tools, bool) or not isinstance(self.needs_vision, bool):
            raise ValueError("capability flags must be booleans")
        if self.judge_model is not None:
            _opaque(self.judge_model, "judge_model")
        _integer_range(self.max_candidates, "max_candidates", 1, 6)
        _integer_range(self.max_cases, "max_cases", 1, 25)
        _integer_range(self.min_cases, "min_cases", 1, 50)
        if (
            isinstance(self.tolerance_pct, bool)
            or not isinstance(self.tolerance_pct, (int, float))
            or not 0 <= self.tolerance_pct <= 100
        ):
            raise ValueError("tolerance_pct must be between 0 and 100")
        if self.mode not in {"equivalence", "correctness"}:
            raise ValueError("mode must be equivalence or correctness")

    def payload(self) -> dict[str, object]:
        """Return only fields accepted by the real Pydantic request model."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ServiceCallIdentity:
    operation_id: str
    provider_request_id: str | None
    cost_event_id: str
    audit_event_id: str

    def __post_init__(self) -> None:
        for field in (
            "operation_id",
            "cost_event_id",
            "audit_event_id",
        ):
            _opaque(getattr(self, field), field)
        if self.provider_request_id is not None:
            _opaque(self.provider_request_id, "provider_request_id")


@dataclass(frozen=True, slots=True)
class ServiceExperimentIdentity:
    """Sanitized keys sufficient for durable cost-plane readback."""

    campaign_id: str
    run_id: str
    calls: tuple[ServiceCallIdentity, ...]

    def __post_init__(self) -> None:
        _opaque(self.campaign_id, "campaign_id")
        _opaque(self.run_id, "run_id")
        if not self.calls:
            raise ValueError("execution identity requires live provider calls")


class ReconciliationCollector(Protocol):
    """Read sanitized Audit, reservation, local-cost, and Regulus records."""

    def collect(self, identity: ServiceExperimentIdentity) -> ReconciliationInput: ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


class HttpPoster(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> HttpResponse: ...


@dataclass(slots=True)
class _UrllibResponse:
    status_code: int
    body: bytes

    def json(self) -> object:
        return json_module.loads(self.body)


class _UrllibPoster:
    """Small concrete transport kept injectable for deterministic campaign tests."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> _UrllibResponse:
        request = Request(
            url,
            data=json_module.dumps(json, separators=(",", ":")).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback checked
                return _UrllibResponse(response.status, response.read())
        except HTTPError as exc:
            return _UrllibResponse(exc.code, exc.read())


def _loopback_origin(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        is_loopback = hostname == "localhost" or (
            hostname is not None and ipaddress.ip_address(hostname).is_loopback
        )
    except ValueError as exc:
        raise ValueError("rightsizing service base URL must be a loopback origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("rightsizing service base URL must be a loopback origin")
    return base_url.rstrip("/")


def _identity_from_response(response: Mapping[str, object]) -> ServiceExperimentIdentity:
    execution = response.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("measured service response is missing execution identity")
    calls_raw = execution.get("calls")
    if not isinstance(calls_raw, list) or not calls_raw:
        raise ValueError("measured service response is missing call identity")
    calls: list[ServiceCallIdentity] = []
    for raw in calls_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("measured service response has invalid call identity")
        operation_id = raw.get("operation_id")
        cost_event_id = raw.get("cost_event_id")
        audit_event_id = raw.get("audit_event_id")
        for field, value in (
            ("operation_id", operation_id),
            ("cost_event_id", cost_event_id),
            ("audit_event_id", audit_event_id),
        ):
            if not isinstance(value, str):
                raise ValueError(f"measured service response is missing {field} identity")
        provider_request_id = raw.get("provider_request_id")
        if provider_request_id is not None and not isinstance(provider_request_id, str):
            raise ValueError("measured service response has invalid provider_request_id")
        assert isinstance(operation_id, str)
        assert isinstance(cost_event_id, str)
        assert isinstance(audit_event_id, str)
        calls.append(
            ServiceCallIdentity(
                operation_id=operation_id,
                provider_request_id=provider_request_id,
                cost_event_id=cost_event_id,
                audit_event_id=audit_event_id,
            )
        )
    provider_call_count = execution.get("provider_call_count")
    if (
        isinstance(provider_call_count, bool)
        or not isinstance(provider_call_count, int)
        or provider_call_count != len(calls)
    ):
        raise ValueError("measured service response provider call identity count differs")
    campaign_id = execution.get("campaign_id")
    run_id = execution.get("run_id")
    if not isinstance(campaign_id, str):
        raise ValueError("measured service response is missing campaign_id identity")
    if not isinstance(run_id, str):
        raise ValueError("measured service response is missing run_id identity")
    return ServiceExperimentIdentity(
        campaign_id=campaign_id,
        run_id=run_id,
        calls=tuple(calls),
    )


class RightsizingServiceAdapter:
    """Execute and reconcile one explicitly armed real service experiment."""

    def __init__(
        self,
        *,
        base_url: str,
        http: HttpPoster | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._base_url = _loopback_origin(base_url)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._http = http if http is not None else _UrllibPoster()

    def collect(
        self,
        *,
        request: ExperimentRequest,
        tenant_id: str,
        cases_sha256: str,
        prior_campaign_spend_usd: Decimal,
        arm: str,
        provider_ready: Callable[[], bool],
        auth_source: Callable[[], str],
        reconciliation_collector: ReconciliationCollector,
    ) -> ServiceRightsizingCapture:
        if arm != ARM_PHRASE:
            raise PermissionError("live provider execution is not explicitly armed")
        if provider_ready() is not True:
            raise PermissionError("opaque provider readiness is not confirmed")
        _opaque(tenant_id, "tenant_id")
        _money(prior_campaign_spend_usd, "prior_campaign_spend_usd")
        if prior_campaign_spend_usd + PER_RUN_CAP_USD > CAMPAIGN_CAP_USD:
            raise PermissionError("remaining campaign capacity cannot admit a $0.25 run")

        try:
            service_auth = auth_source()
        except Exception:
            raise PermissionError("ephemeral service authentication is unavailable") from None
        if not isinstance(service_auth, str) or not service_auth:
            raise PermissionError("ephemeral service authentication is unavailable")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": service_auth,
            "X-Tenant-ID": tenant_id,
        }
        try:
            response = self._http.post(
                f"{self._base_url}{_ROUTE}",
                headers=headers,
                json=request.payload(),
                timeout=self._timeout_seconds,
            )
        except Exception:
            raise RuntimeError("rightsizing service request failed") from None
        finally:
            # Do not make the credential or credential-bearing header map part of
            # adapter state, returned evidence, exception text, or logs.
            service_auth = ""
            headers.clear()

        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"rightsizing service returned HTTP {response.status_code}")
        try:
            raw = response.json()
        except Exception:
            raise ValueError("rightsizing service returned malformed JSON") from None
        if not isinstance(raw, Mapping):
            raise ValueError("rightsizing service response must be a JSON object")

        identity = _identity_from_response(raw)
        reconciliation = reconciliation_collector.collect(identity)
        if not isinstance(reconciliation, ReconciliationInput):
            raise TypeError("reconciliation collector returned an invalid result")
        capture = capture_service_response(
            response=raw,
            cases_sha256=cases_sha256,
            prior_campaign_spend_usd=prior_campaign_spend_usd,
            reconciliation=reconciliation,
        )
        if (capture.campaign_id, capture.run_id) != (identity.campaign_id, identity.run_id):
            raise ValueError("captured service identity changed during reconciliation")
        validate_service_capture(capture)
        return capture


__all__ = [
    "ExperimentRequest",
    "HttpPoster",
    "ReconciliationCollector",
    "RightsizingServiceAdapter",
    "ServiceCallIdentity",
    "ServiceExperimentIdentity",
]
