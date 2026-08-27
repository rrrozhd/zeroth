from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import pytest

from release.live_evaluation.reconciliation import (
    AuditRecord,
    LocalCostEvent,
    ProviderWindowSummary,
    ReconciliationInput,
    RegulusExecutionEvent,
    ReservationRecord,
)
from release.live_evaluation.rightsizing_live_checkpoint import ARM_PHRASE
from release.live_evaluation.rightsizing_service_adapter import (
    ExperimentRequest,
    RightsizingServiceAdapter,
)


_SECRET = "service-secret-that-must-not-survive"
_RUN_ID = "rightsizing:live-run-1"


def _response(*, cost: str = "0.001") -> dict[str, Any]:
    return {
        "incumbent": "openai/gpt-4o-mini",
        "node_id": "research-agent",
        "mode": "equivalence",
        "cases": 1,
        "min_cases": 1,
        "tolerance_pct": 5.0,
        "incumbent_self_equivalence": 1.0,
        "mean_input_tokens": 100.0,
        "mean_output_tokens": 20.0,
        "token_profile_measured": True,
        "harvest": {"cases": 1},
        "outcomes": [
            {
                "model": "gpt-4o-mini",
                "provider": "openai",
                "is_incumbent": True,
                "cases_evaluated": 1,
                "cases_errored": 0,
                "meets_bar": False,
            },
            {
                "model": "gpt-4.1-nano",
                "provider": "openai",
                "is_incumbent": False,
                "cases_evaluated": 1,
                "cases_errored": 0,
                "meets_bar": True,
            },
        ],
        "recommended_model": "openai/gpt-4.1-nano",
        "verdict": "confirmed",
        "note": "candidate met the measured bar",
        "execution": {
            "run_id": _RUN_ID,
            "campaign_id": "campaign-live-1",
            "provider_call_count": 1,
            "measured_cost_usd": cost,
            "estimated_cost_usd": cost,
            "calls": [
                {
                    "operation_id": "operation-1",
                    "provider_request_id": "provider-1",
                    "cost_event_id": "cost-1",
                    "audit_event_id": "audit-1",
                    "model": "openai/gpt-4o-mini",
                    "cost_measurement": "measured",
                    "measured_cost_usd": cost,
                    "estimated_cost_usd": cost,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cleanup_status": "complete",
                    "provider_call_attempted": True,
                    "cache_hit": False,
                }
            ],
        },
    }


def _reconciliation(*, cost: str = "0.001") -> ReconciliationInput:
    amount = Decimal(cost)
    return ReconciliationInput(
        audits=(
            AuditRecord(
                audit_event_id="audit-1",
                operation_id="operation-1",
                run_id=_RUN_ID,
                cost_event_id="cost-1",
                provider_request_id="provider-1",
                cost_usd=amount,
                cache_hit=False,
                run_status="succeeded",
                signed=True,
                chain_verified=True,
            ),
        ),
        reservations=(
            ReservationRecord(
                reservation_id="reservation-1",
                operation_id="operation-1",
                run_id=_RUN_ID,
                state="committed",
                maximum_usd=Decimal("0.25"),
                retained_usd=Decimal("0"),
            ),
        ),
        local_cost_events=(
            LocalCostEvent(
                cost_event_id="cost-1",
                audit_event_id="audit-1",
                operation_id="operation-1",
                run_id=_RUN_ID,
                provider_request_id="provider-1",
                amount_usd=amount,
                cache_hit=False,
                run_status="succeeded",
                failure_tax_usd=Decimal("0"),
            ),
        ),
        regulus_events=(
            RegulusExecutionEvent(
                execution_event_id="regulus-1",
                cost_event_id="cost-1",
                audit_event_id="audit-1",
                operation_id="operation-1",
                run_id=_RUN_ID,
                provider_request_id="provider-1",
                amount_usd=amount,
                failure_tax_usd=Decimal("0"),
                valuation_recorded=False,
                value_usd=Decimal("0"),
                margin_usd=Decimal("0"),
            ),
        ),
        action_receipts=(),
        provider_window=ProviderWindowSummary(window_id="provider-window-1", total_usd=amount),
    )


@dataclass
class _Response:
    status_code: int
    payload: object

    def json(self) -> object:
        return self.payload


class _Http:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []
        self.auth_seen = False

    def post(self, url, *, headers, json, timeout):
        self.auth_seen = headers.get("X-API-Key") == _SECRET
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "header_names": tuple(sorted(headers)),
                "tenant": headers.get("X-Tenant-ID"),
                "json": json,
                "timeout": timeout,
            }
        )
        return self.response


class _ReconciliationCollector:
    def __init__(self, reconciliation: ReconciliationInput) -> None:
        self.reconciliation = reconciliation
        self.identities = []

    def collect(self, identity):
        self.identities.append(identity)
        return self.reconciliation


def _request() -> ExperimentRequest:
    return ExperimentRequest(
        node_id="research-agent",
        incumbent="openai/gpt-4o-mini",
        instruction="Answer only from the supplied context.",
        needs_tools=True,
        needs_vision=False,
        judge_model="openai/gpt-4o-mini",
        max_candidates=1,
        max_cases=1,
        min_cases=1,
        tolerance_pct=5.0,
        mode="equivalence",
    )


def _collect(
    http: _Http,
    collector: _ReconciliationCollector,
    *,
    arm: str = ARM_PHRASE,
    provider_ready=lambda: True,
    auth_source=lambda: _SECRET,
    prior_campaign_spend_usd: Decimal = Decimal("0"),
):
    return RightsizingServiceAdapter(
        base_url="http://127.0.0.1:8122",
        http=http,
    ).collect(
        request=_request(),
        tenant_id="tenant-a",
        cases_sha256="a" * 64,
        prior_campaign_spend_usd=prior_campaign_spend_usd,
        arm=arm,
        provider_ready=provider_ready,
        auth_source=auth_source,
        reconciliation_collector=collector,
    )


def test_posts_exact_real_contract_and_retains_only_sanitized_identity() -> None:
    http = _Http(_Response(200, _response()))
    collector = _ReconciliationCollector(_reconciliation())

    capture = _collect(http, collector)

    assert http.calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8122/v1/econ/rightsizing/experiment",
            "header_names": ("Accept", "Content-Type", "X-API-Key", "X-Tenant-ID"),
            "tenant": "tenant-a",
            "json": {
                "node_id": "research-agent",
                "incumbent": "openai/gpt-4o-mini",
                "instruction": "Answer only from the supplied context.",
                "needs_tools": True,
                "needs_vision": False,
                "judge_model": "openai/gpt-4o-mini",
                "max_candidates": 1,
                "max_cases": 1,
                "min_cases": 1,
                "tolerance_pct": 5.0,
                "mode": "equivalence",
            },
            "timeout": 120.0,
        }
    ]
    assert http.auth_seen is True
    assert capture.run_id == _RUN_ID
    assert capture.calls[0].provider_request_id == "provider-1"
    assert len(collector.identities) == 1
    assert not hasattr(collector.identities[0].calls[0], "role")
    serialized = json.dumps(http.calls, default=str) + repr(capture) + repr(collector.identities)
    assert _SECRET not in serialized
    assert "Authorization" not in serialized


@pytest.mark.parametrize(
    "base_url",
    (
        "https://example.com",
        "http://192.0.2.1:8122",
        "http://localhost.evil.example:8122",
        "http://user:password@127.0.0.1:8122",
        "http://127.0.0.1:8122/v1",
        "http://[::1",
    ),
)
def test_rejects_non_loopback_or_non_origin_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="loopback origin"):
        RightsizingServiceAdapter(base_url=base_url, http=_Http(_Response(200, _response())))


@pytest.mark.parametrize(
    ("arm", "provider_ready", "message"),
    (
        ("yes", lambda: True, "explicitly armed"),
        (ARM_PHRASE, lambda: False, "provider readiness"),
    ),
)
def test_authorization_and_opaque_provider_readiness_gate_before_auth_or_http(
    arm, provider_ready, message
) -> None:
    http = _Http(_Response(200, _response()))
    collector = _ReconciliationCollector(_reconciliation())
    auth_calls = 0

    def auth_source() -> str:
        nonlocal auth_calls
        auth_calls += 1
        return _SECRET

    with pytest.raises(PermissionError, match=message):
        _collect(
            http,
            collector,
            arm=arm,
            provider_ready=provider_ready,
            auth_source=auth_source,
        )

    assert auth_calls == 0
    assert http.calls == []
    assert collector.identities == []


def test_campaign_capacity_is_checked_before_auth_or_http() -> None:
    http = _Http(_Response(200, _response()))
    collector = _ReconciliationCollector(_reconciliation())

    with pytest.raises(PermissionError, match="campaign capacity"):
        _collect(http, collector, prior_campaign_spend_usd=Decimal("9.76"))

    assert http.calls == []
    assert collector.identities == []


def test_auth_source_failure_is_redacted_and_prevents_http() -> None:
    http = _Http(_Response(200, _response()))
    collector = _ReconciliationCollector(_reconciliation())

    def broken_auth_source() -> str:
        raise RuntimeError(_SECRET)

    with pytest.raises(PermissionError, match="authentication is unavailable") as raised:
        _collect(http, collector, auth_source=broken_auth_source)

    assert _SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert http.calls == []
    assert collector.identities == []


def test_non_2xx_fails_closed_without_parsing_or_reconciliation() -> None:
    http = _Http(_Response(503, {"detail": "provider unavailable", "execution": _SECRET}))
    collector = _ReconciliationCollector(_reconciliation())

    with pytest.raises(RuntimeError, match="HTTP 503") as raised:
        _collect(http, collector)

    assert collector.identities == []
    assert _SECRET not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    (
        {"verdict": "none"},
        {**_response(), "execution": {**_response()["execution"], "run_id": ""}},
        {
            **_response(),
            "execution": {
                **_response()["execution"],
                "calls": [{**_response()["execution"]["calls"][0], "operation_id": ""}],
            },
        },
    ),
)
def test_missing_execution_or_identity_fails_before_reconciliation(payload) -> None:
    http = _Http(_Response(200, payload))
    collector = _ReconciliationCollector(_reconciliation())

    with pytest.raises(ValueError, match="execution|identity|run_id|operation_id"):
        _collect(http, collector)

    assert collector.identities == []


def test_post_response_per_run_cap_is_enforced() -> None:
    http = _Http(_Response(200, _response(cost="0.251")))
    collector = _ReconciliationCollector(_reconciliation(cost="0.251"))

    with pytest.raises(ValueError, match="per-run cap"):
        _collect(http, collector)

    assert len(collector.identities) == 1
