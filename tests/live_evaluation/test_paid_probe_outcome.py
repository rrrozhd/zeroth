from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from release.live_evaluation.control_gate import PaidProbeResult
from release.live_evaluation.paid_probe_outcome import (
    AuthoritativePaidProbeOutcomeCollector,
    PaidProbeOutcomeError,
)


TENANT = "evaluation-studio-v1"
CAMPAIGN = "evaluation-studio-v1"
OPERATION = "control-probe:evaluation-studio-v1:provider"
RUN = "control-run:evaluation-studio-v1:provider"
COST = "cost-provider-1"
AUDIT = "audit-provider-1"


def _databases(tmp_path: Path, *, provider_request_id: str | None = None) -> tuple[Path, Path]:
    service = tmp_path / "zeroth.db"
    economics = tmp_path / "econ.db"
    with sqlite3.connect(service) as database:
        database.execute(
            """CREATE TABLE node_audits (
            audit_id TEXT, run_id TEXT, tenant_id TEXT, record_json TEXT,
            cost_usd REAL, cost_event_id TEXT, chain_sequence INTEGER)"""
        )
        database.execute(
            "INSERT INTO node_audits VALUES (?,?,?,?,?,?,?)",
            (
                AUDIT,
                RUN,
                TENANT,
                json.dumps(
                    {
                        "audit_id": AUDIT,
                        "run_id": RUN,
                        "tenant_id": TENANT,
                        "campaign_id": CAMPAIGN,
                        "cost_event_id": COST,
                        "cost_usd": "0.00000285",
                        "status": "completed",
                        "chain_sequence": 1,
                        "record_digest": "digest-1",
                        "record_signature": "signature-1",
                        "signing_algorithm": "HS256",
                        "signing_key_id": "local-campaign-key",
                    }
                ),
                0.00000285,
                COST,
                1,
            ),
        )
    with sqlite3.connect(economics) as database:
        database.execute(
            """CREATE TABLE cost_reservations (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT, run_id TEXT,
            status TEXT, actual_cost_usd REAL, cost_event_id TEXT,
            provider_request_id TEXT, cleanup_status TEXT)"""
        )
        database.execute(
            "INSERT INTO cost_reservations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                TENANT,
                CAMPAIGN,
                OPERATION,
                RUN,
                "committed",
                0.00000285,
                COST,
                provider_request_id,
                "complete",
            ),
        )
        database.execute(
            """CREATE TABLE execution_events (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT, execution_id TEXT,
            provider_request_id TEXT, cleanup_status TEXT, token_cost_usd REAL,
            tool_cost_usd REAL, compute_cost_usd REAL, metadata TEXT)"""
        )
        database.execute(
            "INSERT INTO execution_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                TENANT,
                CAMPAIGN,
                OPERATION,
                COST,
                provider_request_id,
                "complete",
                0.00000285,
                None,
                None,
                json.dumps(
                    {
                        "tenant_id": TENANT,
                        "campaign_id": CAMPAIGN,
                        "operation_id": OPERATION,
                        "run_id": RUN,
                        "provider_request_id": provider_request_id,
                        "probe": True,
                    }
                ),
            ),
        )
    return service, economics


def _collector(service: Path, economics: Path) -> AuthoritativePaidProbeOutcomeCollector:
    return AuthoritativePaidProbeOutcomeCollector(
        service_database=service,
        economics_database=economics,
        tenant_id=TENANT,
        campaign_id=CAMPAIGN,
        operation_id=OPERATION,
        run_id=RUN,
    )


@pytest.mark.parametrize("provider_request_id", [None, "provider-request-1"])
def test_collects_one_exact_committed_signed_provider_probe(
    tmp_path: Path, provider_request_id: str | None
) -> None:
    service, economics = _databases(tmp_path, provider_request_id=provider_request_id)

    result = _collector(service, economics).collect()

    assert result == PaidProbeResult(
        kind="provider",
        operation_id=OPERATION,
        run_id=RUN,
        audit_event_id=AUDIT,
        cost_event_id=COST,
        provider_request_id=provider_request_id,
        connector_request_id=None,
        request_count=1,
        cache_hit=False,
        audit_chain_signed=True,
        cleanup_state="committed",
        measured_cost_usd=result.measured_cost_usd,
    )
    assert str(result.measured_cost_usd) == "0.00000285"


def test_provider_request_identity_must_be_consistently_absent(tmp_path: Path) -> None:
    service, economics = _databases(tmp_path)
    with sqlite3.connect(economics) as database:
        database.execute(
            "UPDATE execution_events SET provider_request_id='unexpected-provider-request'"
        )

    with pytest.raises(PaidProbeOutcomeError, match="provider_request_identity_mismatch"):
        _collector(service, economics).collect()


def test_collects_chroma_probe_with_connector_request_identity(tmp_path: Path) -> None:
    service, economics = _databases(tmp_path, provider_request_id="embedding-provider-request-1")
    result = AuthoritativePaidProbeOutcomeCollector(
        service_database=service,
        economics_database=economics,
        tenant_id=TENANT,
        campaign_id=CAMPAIGN,
        operation_id=OPERATION,
        run_id=RUN,
        kind="chroma",
    ).collect()

    assert result.kind == "chroma"
    assert result.connector_request_id == "embedding-provider-request-1"
    assert result.provider_request_id == result.connector_request_id


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            "INSERT INTO cost_reservations SELECT * FROM cost_reservations",
            "reservation_not_unique",
        ),
        (
            "INSERT INTO execution_events SELECT * FROM execution_events",
            "execution_not_unique",
        ),
        (
            "UPDATE cost_reservations SET status='ambiguous'",
            "reservation_not_committed",
        ),
        (
            "UPDATE execution_events SET token_cost_usd=0.01",
            "cost_identity_mismatch",
        ),
    ],
)
def test_rejects_non_authoritative_economics_outcomes(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    service, economics = _databases(tmp_path)
    with sqlite3.connect(economics) as database:
        database.execute(mutation)

    with pytest.raises(PaidProbeOutcomeError, match=reason):
        _collector(service, economics).collect()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            "INSERT INTO node_audits SELECT * FROM node_audits",
            "audit_not_unique",
        ),
        (
            "UPDATE node_audits SET cost_event_id='other-cost'",
            "cost_identity_mismatch",
        ),
        (
            "UPDATE node_audits SET record_json=json_set(record_json, '$.record_signature', '')",
            "audit_not_signed",
        ),
    ],
)
def test_rejects_non_authoritative_audit_outcomes(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    service, economics = _databases(tmp_path)
    with sqlite3.connect(service) as database:
        database.execute(mutation)

    with pytest.raises(PaidProbeOutcomeError, match=reason):
        _collector(service, economics).collect()


def test_exact_scope_does_not_accept_other_tenant_rows(tmp_path: Path) -> None:
    service, economics = _databases(tmp_path)
    with sqlite3.connect(economics) as database:
        database.execute("UPDATE cost_reservations SET tenant_id='other-tenant'")

    with pytest.raises(PaidProbeOutcomeError, match="reservation_not_unique"):
        _collector(service, economics).collect()
