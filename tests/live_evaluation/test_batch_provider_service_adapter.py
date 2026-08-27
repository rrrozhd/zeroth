from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import pytest

import release.live_evaluation.batch_provider_service_adapter as subject
from release.live_evaluation.batch_provider_economics import (
    CONCURRENCY,
    ITEMS_PER_REPETITION,
    MAX_CAMPAIGN_USD,
    MAX_PER_RUN_USD,
    BatchProviderEconomicsHarness,
    LiveBatchGate,
    PlannedBatchSubmission,
    ReadinessAttestation,
)
from release.live_evaluation.campaign_http import provider_acknowledgement
from release.live_evaluation.config import CampaignConfig

_SECRET = "ephemeral-service-auth-value"
_CAMPAIGN = "evaluation-batch-economics"
_TENANT = "evaluation-batch-economics"
_ITEMS = tuple(
    {"index": index, "question": f"question-{index}"} for index in range(ITEMS_PER_REPETITION)
)


@dataclass
class _Response:
    status_code: int
    payload: object

    def json(self) -> object:
        return self.payload


class _Http:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.auth_seen = False
        self.parent_posts = 0
        self.parent_polls: dict[str, int] = {}
        self.override_status: dict[tuple[str, str], int] = {}
        self.mutate_children = lambda value: value
        self.mutate_child_evidence = lambda value: value
        self.mutate_chain = lambda value: value

    async def request(self, method, url, *, headers, json, timeout):
        path = "/" + url.split("/", 3)[-1]
        self.auth_seen = self.auth_seen or headers.get("X-API-Key") == _SECRET
        self.requests.append(
            {
                "method": method,
                "path": path,
                "header_names": tuple(sorted(headers)),
                "tenant": headers.get("X-Tenant-ID"),
                "json": json,
                "timeout": timeout,
            }
        )
        status = self.override_status.get((method, path))
        if status is not None:
            return _Response(status, {"detail": _SECRET})
        if method == "POST" and path == "/v1/runs":
            self.parent_posts += 1
            run_id = f"service-parent-{self.parent_posts}"
            self.parent_polls[run_id] = 0
            return _Response(
                202,
                {
                    "run_id": run_id,
                    "status": "queued",
                    "campaign_id": _CAMPAIGN,
                    "parent_run_id": None,
                },
            )
        if method == "GET" and path.endswith("/children"):
            parent_id = path.split("/")[-2]
            children = [
                {
                    "run_id": f"{parent_id}-child-{index}",
                    "status": "succeeded",
                    "parent_run_id": parent_id,
                    "thread_id": f"thread-{parent_id}-{index}",
                    "deployment_ref": "batch-child",
                    "graph_version_ref": "batch-child@1",
                    "campaign_id": _CAMPAIGN,
                }
                for index in range(ITEMS_PER_REPETITION)
            ]
            return _Response(200, self.mutate_children(children))
        if method == "GET" and path.endswith("/evidence"):
            run_id = path.split("/")[-2]
            if "-child-" not in run_id:
                return _Response(
                    200,
                    {
                        "run": {"run_id": run_id},
                        "audits": [{"audit_id": f"parent-audit-{run_id}", "run_id": run_id}],
                        "summary": {
                            "audit_count": 1,
                            "priced_call_count": 0,
                            "cost_event_count": 0,
                            "total_cost_usd": 0,
                            "cost_identity_state": "not_applicable_no_priced_call",
                            "reconciliation_state": "reconciled_zero_activity",
                        },
                    },
                )
            index = int(run_id.rsplit("-", 1)[1])
            repetition = int(run_id.split("service-parent-", 1)[1].split("-", 1)[0])
            evidence = {
                "run": {"run_id": run_id},
                "audits": [
                    {
                        "audit_id": f"audit-{repetition}-{index}",
                        "run_id": run_id,
                        "cost_event_id": f"cost-{repetition}-{index}",
                        "cost_usd": 0.000020,
                    }
                ],
                "summary": {
                    "audit_count": 1,
                    "priced_call_count": 1,
                    "cost_event_count": 1,
                    "total_cost_usd": 0.000020,
                    "cost_identity_state": "correlated",
                    "reconciliation_state": "reconciled",
                },
            }
            return _Response(200, self.mutate_child_evidence(evidence))
        if method == "POST" and path.endswith("/verify-chain"):
            return _Response(
                200,
                self.mutate_chain(
                    {
                        "verified": True,
                        "signature_verified": True,
                        "unsigned_record_count": 0,
                        "record_count": 1,
                    }
                ),
            )
        if method == "GET" and path.startswith("/v1/runs/"):
            run_id = path.split("/")[-1]
            if "-child-" in run_id:
                parent_id = run_id.rsplit("-child-", 1)[0]
                return _Response(
                    200,
                    {
                        "run_id": run_id,
                        "status": "succeeded",
                        "parent_run_id": parent_id,
                        "campaign_id": _CAMPAIGN,
                    },
                )
            poll = self.parent_polls[run_id]
            self.parent_polls[run_id] += 1
            return _Response(
                200,
                {
                    "run_id": run_id,
                    "status": "queued" if poll == 0 else "succeeded",
                    "parent_run_id": None,
                    "campaign_id": _CAMPAIGN,
                },
            )
        raise AssertionError((method, path))


class _Collector:
    def __init__(self, mutate=lambda value: value) -> None:
        self.identities = []
        self.mutate = mutate

    def collect(self, identity):
        self.identities.append(identity)
        repetition = identity.repetition
        actual = Decimal("0.000020")
        children = tuple(
            subject.CollectedChildReconciliation(
                item_index=index,
                child_run_id=child_run_id,
                operation_id=f"operation-{repetition}-{index}",
                provider_request_id=f"provider-{repetition}-{index}",
                audit_event_id=f"audit-{repetition}-{index}",
                cost_event_id=f"cost-{repetition}-{index}",
                regulus_execution_event_id=f"regulus-{repetition}-{index}",
                reservation_id=f"reservation-{repetition}-{index}",
                reservation_operation_id=f"operation-{repetition}-{index}",
                reservation_status="committed",
                reserved_max_cost_usd=Decimal("0.000025"),
                reservation_actual_cost_usd=actual,
                reservation_released_cost_usd=Decimal("0.000005"),
                reservation_cleanup_status="complete",
                cache_hit=False,
                audit_cost_usd=actual,
                local_cost_usd=actual,
                economics_cost_usd=actual,
            )
            for index, child_run_id in enumerate(identity.child_run_ids)
        )
        parent_actual = actual * ITEMS_PER_REPETITION
        return self.mutate(
            subject.CollectedParentReconciliation(
                campaign_id=identity.campaign_id,
                repetition=repetition,
                parent_run_id=identity.parent_run_id,
                configured_concurrency=CONCURRENCY,
                observed_peak_concurrency=CONCURRENCY,
                campaign_spend_after_usd=Decimal("0.000001") + parent_actual * repetition,
                children=children,
            )
        )


def _submission(repetition: int = 1) -> PlannedBatchSubmission:
    return PlannedBatchSubmission(
        campaign_id=_CAMPAIGN,
        repetition=repetition,
        items=ITEMS_PER_REPETITION,
        concurrency=CONCURRENCY,
        per_run_cap_usd=MAX_PER_RUN_USD,
        campaign_cap_usd=MAX_CAMPAIGN_USD,
    )


def _gate(tmp_path) -> LiveBatchGate:
    campaign = CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": _CAMPAIGN,
            "tenant_id": _TENANT,
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(tmp_path / "artifacts"),
            "action_sink_root": str(tmp_path / "artifacts/action-sink"),
        }
    )
    return LiveBatchGate(
        campaign=campaign,
        provider_execution_enabled=True,
        external_cost_acknowledgement=provider_acknowledgement(_CAMPAIGN),
        readiness=ReadinessAttestation(
            campaign_id=_CAMPAIGN,
            tenant_id=_TENANT,
            logical_secret_ref="llm.openai",
            installed=True,
            provider_probe_reconciled=True,
            provider_request_id="readiness-provider",
            operation_id="readiness-operation",
            run_id="readiness-run",
            audit_event_id="readiness-audit",
            cost_event_id="readiness-cost",
            measured_cost_usd=Decimal("0.000001"),
            campaign_spend_before_usd=Decimal("0.000001"),
            audit_signed=True,
        ),
    )


async def _no_sleep(_: float) -> None:
    return None


def _adapter(
    http: _Http,
    collector: _Collector,
    *,
    arm: str | None = None,
    provider_ready=lambda: True,
    auth_source=lambda: _SECRET,
):
    return subject.BatchProviderServiceAdapter(
        base_url="http://127.0.0.1:8122",
        tenant_id=_TENANT,
        items=_ITEMS,
        arm=subject.ARM_PHRASE if arm is None else arm,
        provider_ready=provider_ready,
        auth_source=auth_source,
        reconciliation_collector=collector,
        http=http,
        timeout_seconds=10,
        poll_interval_seconds=0,
        max_poll_attempts=3,
        sleeper=_no_sleep,
    )


def test_submits_real_parent_route_and_returns_only_service_owned_observations() -> None:
    http = _Http()
    collector = _Collector()
    adapter = _adapter(http, collector)

    observation = asyncio.run(adapter.submit_parent(_submission()))

    assert http.parent_posts == 1
    assert http.requests[0] == {
        "method": "POST",
        "path": "/v1/runs",
        "header_names": ("Accept", "Content-Type", "X-API-Key", "X-Tenant-ID"),
        "tenant": _TENANT,
        "json": {
            "input_payload": {"items": list(_ITEMS)},
            "campaign_id": _CAMPAIGN,
            "campaign_strict": True,
        },
        "timeout": 10.0,
    }
    assert observation.parent_run_id == "service-parent-1"
    assert [child.child_run_id for child in observation.children] == [
        f"service-parent-1-child-{index}" for index in range(8)
    ]
    assert observation.observed_peak_concurrency == 4
    assert observation.audit_cost_usd == Decimal("0.000160")
    assert collector.identities[0].parent_run_id == "service-parent-1"
    assert http.auth_seen is True
    serialized = (
        repr(adapter)
        + repr(observation)
        + repr(collector.identities)
        + json.dumps(http.requests, default=str)
    )
    assert _SECRET not in serialized
    assert "Authorization" not in serialized


def test_harness_and_service_adapter_submit_exactly_three_real_parent_runs(
    tmp_path,
) -> None:
    http = _Http()
    collector = _Collector()

    result = asyncio.run(
        BatchProviderEconomicsHarness(_gate(tmp_path)).execute(_adapter(http, collector))
    )

    assert http.parent_posts == 3
    assert [identity.repetition for identity in collector.identities] == [1, 2, 3]
    assert [parent.parent_run_id for parent in result.parent_observations] == [
        "service-parent-1",
        "service-parent-2",
        "service-parent-3",
    ]
    assert sum(len(parent.children) for parent in result.parent_observations) == 24


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
        subject.BatchProviderServiceAdapter(
            base_url=base_url,
            tenant_id=_TENANT,
            items=_ITEMS,
            arm=subject.ARM_PHRASE,
            provider_ready=lambda: True,
            auth_source=lambda: _SECRET,
            reconciliation_collector=_Collector(),
            http=_Http(),
        )


@pytest.mark.parametrize(
    ("arm", "provider_ready", "message"),
    (
        ("yes", lambda: True, "explicitly armed"),
        (None, lambda: False, "provider readiness"),
    ),
)
def test_arm_and_readiness_fail_before_auth_or_http(arm, provider_ready, message) -> None:
    http = _Http()
    auth_calls = 0

    def auth_source():
        nonlocal auth_calls
        auth_calls += 1
        return _SECRET

    adapter = _adapter(
        http,
        _Collector(),
        arm=subject.ARM_PHRASE if arm is None else arm,
        provider_ready=provider_ready,
        auth_source=auth_source,
    )
    with pytest.raises(PermissionError, match=message):
        asyncio.run(adapter.submit_parent(_submission()))
    assert auth_calls == 0
    assert http.requests == []


def test_auth_failure_is_redacted_and_prevents_http() -> None:
    def broken_auth():
        raise RuntimeError(_SECRET)

    http = _Http()
    with pytest.raises(PermissionError, match="authentication is unavailable") as raised:
        asyncio.run(
            _adapter(http, _Collector(), auth_source=broken_auth).submit_parent(_submission())
        )
    assert _SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert http.requests == []


def test_readiness_failure_is_redacted_and_prevents_auth_or_http() -> None:
    def broken_readiness():
        raise RuntimeError(_SECRET)

    http = _Http()
    auth_calls = 0

    def auth_source():
        nonlocal auth_calls
        auth_calls += 1
        return _SECRET

    with pytest.raises(PermissionError, match="provider readiness") as raised:
        asyncio.run(
            _adapter(
                http,
                _Collector(),
                provider_ready=broken_readiness,
                auth_source=auth_source,
            ).submit_parent(_submission())
        )
    assert _SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert auth_calls == 0
    assert http.requests == []


def test_non_2xx_fails_closed_without_leaking_body_or_collecting() -> None:
    http = _Http()
    http.override_status[("POST", "/v1/runs")] = 503
    collector = _Collector()
    with pytest.raises(RuntimeError, match="HTTP 503") as raised:
        asyncio.run(_adapter(http, collector).submit_parent(_submission()))
    assert _SECRET not in str(raised.value)
    assert collector.identities == []


def test_missing_child_lineage_fails_before_reconciliation() -> None:
    http = _Http()

    def break_lineage(children):
        children[0] = {**children[0], "parent_run_id": "wrong-parent"}
        return children

    http.mutate_children = break_lineage
    collector = _Collector()
    with pytest.raises(RuntimeError, match="lineage"):
        asyncio.run(_adapter(http, collector).submit_parent(_submission()))
    assert collector.identities == []


def test_unsigned_or_broken_chain_fails_closed() -> None:
    http = _Http()
    http.mutate_chain = lambda value: {**value, "signature_verified": False}
    collector = _Collector()
    with pytest.raises(RuntimeError, match="signed audit chain"):
        asyncio.run(_adapter(http, collector).submit_parent(_submission()))
    assert collector.identities == []


def test_public_child_economics_must_be_correlated_and_match_collector() -> None:
    http = _Http()
    http.mutate_child_evidence = lambda value: {
        **value,
        "summary": {**value["summary"], "reconciliation_state": "incomplete"},
    }
    collector = _Collector()
    with pytest.raises(RuntimeError, match="economics evidence"):
        asyncio.run(_adapter(http, collector).submit_parent(_submission()))


def test_estimated_runtime_audit_uses_estimated_cost_field() -> None:
    http = _Http()

    def estimated(value):
        audit = value["audits"][0]
        return {
            **value,
            "audits": [
                {
                    **audit,
                    "cost_measurement": "estimated",
                    "cost_usd": None,
                    "estimated_cost_usd": 0.000020,
                }
            ],
        }

    http.mutate_child_evidence = estimated

    result = asyncio.run(_adapter(http, _Collector()).submit_parent(_submission()))

    assert result.status == "succeeded"
    assert result.audit_cost_usd == Decimal("0.000160")


def test_malformed_public_cost_fails_with_sanitized_adapter_error() -> None:
    http = _Http()
    http.mutate_child_evidence = lambda value: {
        **value,
        "summary": {**value["summary"], "total_cost_usd": "not-money"},
    }
    with pytest.raises(RuntimeError, match="invalid cost") as raised:
        asyncio.run(_adapter(http, _Collector()).submit_parent(_submission()))
    assert raised.value.__cause__ is None


def test_authoritative_collector_must_prove_exact_concurrency_and_identity() -> None:
    http = _Http()
    collector = _Collector(lambda value: replace(value, observed_peak_concurrency=3))
    with pytest.raises(RuntimeError, match="concurrency"):
        asyncio.run(_adapter(http, collector).submit_parent(_submission()))

    http = _Http()

    def wrong_audit(value):
        child = replace(value.children[0], audit_event_id="not-public")
        return replace(value, children=(child,) + value.children[1:])

    with pytest.raises(RuntimeError, match="audit identity"):
        asyncio.run(_adapter(http, _Collector(wrong_audit)).submit_parent(_submission()))


def test_parent_poll_timeout_fails_closed_without_collector() -> None:
    http = _Http()

    original = http.request

    async def never_terminal(method, url, **kwargs):
        response = await original(method, url, **kwargs)
        path = "/" + url.split("/", 3)[-1]
        if (
            method == "GET"
            and path.startswith("/v1/runs/service-parent-")
            and "/" not in path.removeprefix("/v1/runs/")
        ):
            return _Response(
                200,
                {
                    "run_id": "service-parent-1",
                    "status": "running",
                    "parent_run_id": None,
                    "campaign_id": _CAMPAIGN,
                },
            )
        return response

    http.request = never_terminal
    collector = _Collector()
    with pytest.raises(TimeoutError, match="parent run"):
        asyncio.run(_adapter(http, collector).submit_parent(_submission()))
    assert collector.identities == []


def test_declared_product_api_gaps_are_explicit_not_fabricated() -> None:
    assert subject.PRODUCT_API_GAPS == (
        "public run APIs do not expose configured or observed peak branch concurrency",
        "public run evidence does not expose provider request, operation, reservation, or Regulus execution identities",
    )
