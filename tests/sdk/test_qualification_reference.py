from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError
import pytest

from zeroth.protocol import (
    MigrationEvidence,
    MigrationObservation,
    MigrationRiskPolicy,
    ProbabilisticMigrationRequest,
)


def test_sdk_migration_request_accepts_only_an_opaque_qualification_reference() -> None:
    observation = MigrationObservation(
        case_id="case-1",
        cost_usd=Decimal("1"),
        latency_ms=100,
        accepted=True,
        source="test",
    )
    request = ProbabilisticMigrationRequest(
        evidence=MigrationEvidence(
            workload="invoice-agent",
            incumbent_model="model-a",
            candidate_model="model-b",
            incumbent=[observation],
            candidate=[observation],
            period_request_counts=[100],
        ),
        policy=MigrationRiskPolicy(),
        qualification_id="qual_001",
    )
    assert request.qualification_id == "qual_001"
    with pytest.raises(ValidationError):
        ProbabilisticMigrationRequest.model_validate(
            {**request.model_dump(mode="json"), "qualification": {"issuer": "client"}}
        )
    with pytest.raises(ValidationError):
        ProbabilisticMigrationRequest.model_validate(
            {**request.model_dump(mode="json"), "qualification_id": "../tenant-b/qual_001"}
        )
