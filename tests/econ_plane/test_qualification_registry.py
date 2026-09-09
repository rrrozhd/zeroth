from __future__ import annotations

import importlib
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning import qualification as registry
from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord
from zeroth.econ.plane.decisioning.schemas import ProbabilisticMigrationRequest
from zeroth.econ.plane.decisioning.service import (
    _MODEL_MIGRATION_CALIBRATION_METRICS,
    evaluate_and_retain_probabilistic_migration,
)
from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser
from zeroth.econ.plane.instrumentation.models import EconErasureReceipt  # noqa: F401
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.probabilistic import (
    FORECAST_ALGORITHM_VERSION,
    ForecastCalibrationObservation,
    MigrationEvidence,
    MigrationObservation,
    MigrationRiskPolicy,
    assess_forecast_readiness,
)
from zeroth.platform.storage.scoping import TenantWideScopeContext


def test_qualification_registry_domain_exists() -> None:
    module = importlib.import_module("zeroth.econ.plane.decisioning.qualification")
    assert hasattr(module, "QualificationRecordV1")
    assert hasattr(module, "resolve_qualification")


def test_registry_exposes_server_owned_persistence_and_applicability_contracts() -> None:
    for name in (
        "QualificationApplicabilityV1",
        "QualificationResolution",
        "QualificationRegistryRecord",
        "retain_qualification",
        "revoke_qualification",
    ):
        assert hasattr(registry, name), name


def test_registry_mapper_uses_canonical_models_inventory_path() -> None:
    model = registry.QualificationRegistryRecord
    assert model.__module__ == "zeroth.econ.plane.decisioning.models"
    assert model.scope_definition.table_name == "model_migration_qualifications"
    assert model.scope_definition.resource_name == "econ.model_migration_qualification"
    assert model in {mapper.class_ for mapper in Base.registry.mappers}


def unsigned_record(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "record_version": "qualification-record-v1",
        "qualification_id": "qual_001",
        "tenant_id": "tenant-a",
        "workload": "invoice-agent",
        "incumbent_model": "model-a",
        "candidate_model": "model-b",
        "policy_digest": "b" * 64,
        "algorithm_version": "algo-v1",
        "cluster_family_set": ["two_point_incident", "beta_binomial"],
        "mean_probability_contract": "959173476724471/400000000000000000",
        "icc_upper_bound_exact": "1/100",
        "cluster_size_vector": [2, 3, 2],
        "cluster_size_vector_sha256": (
            "10afd67923f79e592efe89234515b65132183ea49865387664e80b0707d6d317"
        ),
        "authorization_count_threshold": 778,
        "boundary_error_alpha_exact": "1/20",
        "certificate_artifact_sha256": "c" * 64,
        "family_confidence_set_method": "method-family-v1",
        "family_qualification_artifact_sha256": "d" * 64,
        "icc_upper_confidence_method": "method-icc-v1",
        "icc_qualification_artifact_sha256": "e" * 64,
        "independent_unit_definition": "account-month",
        "grouping_keys": ["account_id"],
        "source_window_start": datetime(2026, 8, 1, tzinfo=UTC),
        "source_window_end": datetime(2026, 9, 1, tzinfo=UTC),
        "source_evidence_sha256": "f" * 64,
        "issuer": "offline-authority-v1",
        "issued_at": datetime(2026, 9, 1, tzinfo=UTC),
        "valid_from": datetime(2026, 9, 1, tzinfo=UTC),
        "valid_until": datetime(2026, 10, 1, tzinfo=UTC),
        "revoked_at": None,
        "supersedes_qualification_id": None,
    }
    values.update(updates)
    return values


def signed_record(**updates: object) -> registry.QualificationRecordV1:
    values = unsigned_record(**updates)
    values["record_digest"] = registry.qualification_record_digest(values)
    return registry.QualificationRecordV1.model_validate(values)


def applicability(**updates: object) -> registry.QualificationApplicabilityV1:
    values: dict[str, object] = {
        "workload": "invoice-agent",
        "incumbent_model": "model-a",
        "candidate_model": "model-b",
        "policy_digest": "b" * 64,
        "algorithm_version": "algo-v1",
        "cluster_family_set": ["beta_binomial", "two_point_incident"],
        "mean_probability_contract": "959173476724471/400000000000000000",
        "icc_upper_bound_exact": "1/100",
        "cluster_size_vector": [2, 3, 2],
        "authorization_count_threshold": 778,
        "boundary_error_alpha_exact": "1/20",
        "source_window_start": datetime(2026, 8, 1, tzinfo=UTC),
        "source_window_end": datetime(2026, 9, 1, tzinfo=UTC),
        "source_evidence_sha256": "f" * 64,
    }
    values.update(updates)
    return registry.QualificationApplicabilityV1.model_validate(values)


def scoped(session: Session, tenant: str = "tenant-a") -> ScopedSession:
    return ScopedSession(session, TenantWideScopeContext(tenant_id=tenant))


def public_request() -> ProbabilisticMigrationRequest:
    def observations(source: str, cost: str) -> list[MigrationObservation]:
        return [
            MigrationObservation(
                case_id=f"case-{index}",
                cost_usd=Decimal(cost),
                latency_ms=800,
                accepted=True,
                critical_error=False,
                source=source,
            )
            for index in range(100)
        ]

    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.02,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.05,
        max_constraint_breach_probability=0.1,
        max_cvar_loss_usd=Decimal("100"),
    )
    calibration = [
        ForecastCalibrationObservation(
            forecast_id=f"{metric}-{index}",
            metric=metric,
            predicted_mean=100,
            predicted_low=90,
            predicted_high=110,
            observed=100,
            observed_at=datetime(2026, index + 1, 1, tzinfo=UTC),
        )
        for metric in (
            "monthly_cost_usd",
            "success_rate",
            "p95_latency_ms",
            "critical_error_rate",
        )
        for index in range(6)
    ]
    return ProbabilisticMigrationRequest(
        evidence=MigrationEvidence(
            workload="invoice-agent",
            incumbent_model="model-a",
            candidate_model="model-b",
            incumbent=observations("production", "1.00"),
            candidate=observations("replay", "0.50"),
            period_request_counts=[90, 100, 110],
            demand_horizon="month",
        ),
        policy=policy,
        calibration_observations=calibration,
        simulations=100,
        seed=7,
        qualification_id="qual_001",
    )


def test_canonical_vector_and_record_digest_known_answers() -> None:
    values = unsigned_record()
    assert registry.cluster_size_vector_digest([2, 3, 2]) == (
        "10afd67923f79e592efe89234515b65132183ea49865387664e80b0707d6d317"
    )
    assert registry.qualification_record_digest(values) == (
        "2a78105c69ac1f5b4c1b48885edae5c40ebc377daf2b060db01d5535efb37071"
    )
    revoked = {**values, "revoked_at": datetime(2026, 9, 2, tzinfo=UTC)}
    assert registry.qualification_record_digest(revoked) == registry.qualification_record_digest(
        values
    )


def test_family_set_order_is_equivalent_but_vector_order_is_identity_significant() -> None:
    forward = unsigned_record()
    reverse_families = unsigned_record(cluster_family_set=["beta_binomial", "two_point_incident"])
    reordered_vector = unsigned_record(
        cluster_size_vector=[3, 2, 2],
        cluster_size_vector_sha256=registry.cluster_size_vector_digest([3, 2, 2]),
    )

    assert registry.qualification_record_digest(forward) == registry.qualification_record_digest(
        reverse_families
    )
    assert registry.qualification_record_digest(forward) != registry.qualification_record_digest(
        reordered_vector
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("record_version", "qualification-record-v2"),
        ("qualification_id", "qual_changed"),
        ("tenant_id", "tenant-b"),
        ("workload", "support-agent"),
        ("incumbent_model", "model-c"),
        ("candidate_model", "model-c"),
        ("policy_digest", "a" * 64),
        ("algorithm_version", "algo-v2"),
        ("cluster_family_set", ["beta_binomial"]),
        ("mean_probability_contract", "1/2"),
        ("icc_upper_bound_exact", "1/20"),
        ("cluster_size_vector", [2, 3, 3]),
        ("cluster_size_vector_sha256", "a" * 64),
        ("authorization_count_threshold", 779),
        ("boundary_error_alpha_exact", "1/25"),
        ("certificate_artifact_sha256", "a" * 64),
        ("family_confidence_set_method", "method-family-v2"),
        ("family_qualification_artifact_sha256", "a" * 64),
        ("icc_upper_confidence_method", "method-icc-v2"),
        ("icc_qualification_artifact_sha256", "a" * 64),
        ("independent_unit_definition", "account-quarter"),
        ("grouping_keys", ["account_id", "region"]),
        ("source_window_start", datetime(2026, 8, 2, tzinfo=UTC)),
        ("source_window_end", datetime(2026, 8, 31, tzinfo=UTC)),
        ("source_evidence_sha256", "a" * 64),
        ("issuer", "offline-authority-v2"),
        ("issued_at", datetime(2026, 9, 2, tzinfo=UTC)),
        ("valid_from", datetime(2026, 9, 2, tzinfo=UTC)),
        ("valid_until", datetime(2026, 11, 1, tzinfo=UTC)),
        ("supersedes_qualification_id", "qual_prior"),
    ],
)
def test_every_immutable_record_field_is_digest_bound(field: str, replacement: object) -> None:
    original = unsigned_record()
    tampered = {**original, field: replacement}
    assert registry.qualification_record_digest(tampered) != registry.qualification_record_digest(
        original
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"mean_probability_contract": "0.5"},
        {"icc_upper_bound_exact": "2/200"},
        {"boundary_error_alpha_exact": "0/1"},
        {"cluster_family_set": ["beta_binomial", "beta_binomial"]},
        {"cluster_size_vector_sha256": "0" * 64},
        {"record_digest": "0" * 64},
        {"valid_until": datetime(2026, 9, 1, tzinfo=UTC)},
        {"issued_at": datetime(2026, 9, 1)},
        {"source_window_end": datetime(2026, 9, 2, tzinfo=UTC)},
        {"revoked_at": datetime(2026, 8, 31, tzinfo=UTC)},
    ],
)
def test_malformed_or_hash_invalid_records_fail_closed(updates: dict[str, object]) -> None:
    values = unsigned_record()
    values["record_digest"] = registry.qualification_record_digest(values)
    values.update(updates)
    with pytest.raises(ValidationError):
        registry.QualificationRecordV1.model_validate(values)


def test_server_retains_and_resolves_exact_qualification_without_authorizing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        retained = registry.retain_qualification(db, signed_record())
        result = registry.resolve_qualification(
            db,
            applicability(),
            qualification_id=retained.qualification_id,
            checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )

        assert result.status == "eligible_for_next_evidence_stage_only"
        assert result.reason_code == "qualification_applicable"
        assert result.record_digest == retained.record_digest
        assert result.certificate_artifact_sha256 == "c" * 64
        assert result.public_authorization is False


def test_missing_and_cross_tenant_ids_are_equally_opaque() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as tenant_b_session:
        registry.retain_qualification(
            scoped(tenant_b_session, "tenant-b"), signed_record(tenant_id="tenant-b")
        )
    with Session(engine) as tenant_a_session:
        db = scoped(tenant_a_session, "tenant-a")
        for qualification_id in ("qual_001", "qual_missing"):
            result = registry.resolve_qualification(
                db,
                applicability(),
                qualification_id=qualification_id,
                checked_at=datetime(2026, 9, 2, tzinfo=UTC),
            )
            assert result.reason_code == "qualification_absent"
            assert result.qualification_id is None
            assert result.record_digest is None


def test_server_scope_selection_refuses_ambiguous_candidates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record())
        duplicate = registry._row_for_record(signed_record(qualification_id="qual_002"))
        duplicate.active_scope_key = "corrupt-second-active-key"
        db.add(duplicate)
        db.commit()
        result = registry.resolve_qualification(
            db,
            applicability(),
            checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        assert result.reason_code == "qualification_ambiguous"
        assert result.qualification_id is None


def test_server_scope_selection_ignores_unrelated_policy_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record())
        registry.retain_qualification(
            db,
            signed_record(qualification_id="qual_002", policy_digest="a" * 64),
        )
        result = registry.resolve_qualification(
            db, applicability(), checked_at=datetime(2026, 9, 2, tzinfo=UTC)
        )
        assert result.reason_code == "qualification_applicable"
        assert result.qualification_id == "qual_001"


@pytest.mark.parametrize(
    ("record_updates", "application_updates", "reason"),
    [
        ({}, {"workload": "other"}, "qualification_workload_scope_mismatch"),
        ({}, {"incumbent_model": "other"}, "qualification_model_pair_scope_mismatch"),
        ({}, {"policy_digest": "a" * 64}, "qualification_policy_scope_mismatch"),
        ({}, {"algorithm_version": "algo-v2"}, "qualification_algorithm_scope_mismatch"),
        (
            {"cluster_family_set": ["beta_binomial"]},
            {"cluster_family_set": ["two_point_incident"]},
            "qualification_family_set_mismatch",
        ),
        (
            {"cluster_family_set": ["beta_binomial", "unsupported_family"]},
            {"cluster_family_set": ["beta_binomial"]},
            "qualification_unsupported_family",
        ),
        ({}, {"mean_probability_contract": "1/2"}, "qualification_mean_contract_mismatch"),
        ({}, {"icc_upper_bound_exact": "1/50"}, "qualification_icc_bound_exceeded"),
        ({}, {"cluster_size_vector": [2, 2, 3]}, "qualification_cluster_vector_mismatch"),
        ({}, {"authorization_count_threshold": 779}, "qualification_threshold_mismatch"),
        ({}, {"boundary_error_alpha_exact": "1/25"}, "qualification_alpha_mismatch"),
        (
            {},
            {"source_window_start": datetime(2026, 8, 2, tzinfo=UTC)},
            "qualification_evidence_window_mismatch",
        ),
        (
            {},
            {"cluster_family_set": ["unsupported_family"]},
            "qualification_unsupported_family",
        ),
    ],
)
def test_every_applicability_mismatch_has_a_stable_reason(
    record_updates: dict[str, object], application_updates: dict[str, object], reason: str
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record(**record_updates))
        result = registry.resolve_qualification(
            db,
            applicability(**application_updates),
            qualification_id="qual_001",
            checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        assert result.status == "abstain"
        assert result.reason_code == reason


@pytest.mark.parametrize(
    ("checked_at", "reason"),
    [
        (datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC), "qualification_not_yet_valid"),
        (datetime(2026, 10, 1, tzinfo=UTC), "qualification_expired"),
        (datetime(2026, 9, 2), "qualification_incomplete"),
    ],
)
def test_validity_boundaries_and_naive_time_fail_closed(checked_at: datetime, reason: str) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record())
        result = registry.resolve_qualification(
            db, applicability(), qualification_id="qual_001", checked_at=checked_at
        )
        assert result.reason_code == reason


def test_duplicate_retention_is_idempotent_but_conflicting_identity_is_safe() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        first = registry.retain_qualification(db, signed_record())
        repeated = registry.retain_qualification(db, signed_record())
        assert repeated.record_digest == first.record_digest
        assert len(list(db.scalars(select(registry.QualificationRegistryRecord)))) == 1

        with pytest.raises(registry.QualificationConflict):
            registry.retain_qualification(
                db,
                signed_record(certificate_artifact_sha256="a" * 64),
            )
        with pytest.raises(registry.QualificationConflict):
            registry.retain_qualification(
                db,
                signed_record(revoked_at=datetime(2026, 9, 2, tzinfo=UTC)),
            )


def test_concurrent_same_scope_issuance_commits_exactly_one_active_record(tmp_path) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'qualification-concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)

    def retain(qualification_id: str) -> str:
        with Session(engine) as session:
            try:
                registry.retain_qualification(
                    scoped(session), signed_record(qualification_id=qualification_id)
                )
                return "retained"
            except registry.QualificationConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(retain, ("qual_001", "qual_002")))

    assert sorted(outcomes) == ["conflict", "retained"]
    with Session(engine) as session:
        rows = list(scoped(session).scalars(select(registry.QualificationRegistryRecord)))
        assert len(rows) == 1
        assert rows[0].active_scope_key is not None


def test_same_scope_active_uniqueness_is_tenant_local() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        registry.retain_qualification(scoped(session), signed_record())
    with Session(engine) as session:
        registry.retain_qualification(
            scoped(session, "tenant-b"), signed_record(tenant_id="tenant-b")
        )
    with Session(engine) as session:
        assert len(list(session.scalars(select(registry.QualificationRegistryRecord)))) == 2


def test_supersession_cannot_cross_tenant_or_mutate_foreign_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        registry.retain_qualification(scoped(session), signed_record())
    with Session(engine) as session:
        with pytest.raises(registry.QualificationConflict):
            registry.retain_qualification(
                scoped(session, "tenant-b"),
                signed_record(
                    qualification_id="qual_002",
                    tenant_id="tenant-b",
                    supersedes_qualification_id="qual_001",
                ),
            )
    with Session(engine) as session:
        original = session.get(registry.QualificationRegistryRecord, ("qual_001", "tenant-a"))
        assert original is not None
        assert original.superseded_at is None
        assert original.superseded_by_qualification_id is None


def test_revocation_is_idempotent_and_supersession_preserves_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with Session(engine) as session:
        db = scoped(session)
        original = registry.retain_qualification(db, signed_record())
        first = registry.revoke_qualification(db, "qual_001", revoked_by="auditor", revoked_at=now)
        repeated = registry.revoke_qualification(
            db, "qual_001", revoked_by="auditor", revoked_at=now
        )
        assert first.revocation_digest == repeated.revocation_digest
        assert original.record_digest == first.record_digest
        assert (
            registry.resolve_qualification(
                db,
                applicability(),
                qualification_id="qual_001",
                checked_at=datetime(2026, 9, 2, tzinfo=UTC),
            ).reason_code
            == "qualification_applicable"
        )
        assert (
            registry.resolve_qualification(
                db, applicability(), qualification_id="qual_001", checked_at=now
            ).reason_code
            == "qualification_revoked"
        )

        successor = signed_record(
            qualification_id="qual_002",
            algorithm_version="algo-v2",
            supersedes_qualification_id="qual_001",
        )
        registry.retain_qualification(db, successor)
        assert (
            registry.resolve_qualification(
                db,
                applicability(algorithm_version="algo-v1"),
                qualification_id="qual_001",
                checked_at=now,
            ).reason_code
            == "qualification_superseded"
        )
        assert len(list(db.scalars(select(registry.QualificationRegistryRecord)))) == 2


def test_same_scope_supersession_releases_active_uniqueness_and_keeps_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record())
        successor = signed_record(
            qualification_id="qual_002",
            issued_at=datetime(2026, 9, 3, tzinfo=UTC),
            valid_from=datetime(2026, 9, 3, tzinfo=UTC),
            valid_until=datetime(2026, 11, 1, tzinfo=UTC),
            supersedes_qualification_id="qual_001",
        )
        registry.retain_qualification(db, successor)

        selected = registry.resolve_qualification(
            db, applicability(), checked_at=datetime(2026, 9, 4, tzinfo=UTC)
        )
        assert selected.qualification_id == "qual_002"
        rows = list(db.scalars(select(registry.QualificationRegistryRecord)))
        assert len(rows) == 2
        assert sum(row.active_scope_key is not None for row in rows) == 1


def test_supersession_is_point_in_time_for_explicit_historical_replay() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record())
        registry.retain_qualification(
            db,
            signed_record(
                qualification_id="qual_002",
                issued_at=datetime(2026, 9, 3, tzinfo=UTC),
                valid_from=datetime(2026, 9, 3, tzinfo=UTC),
                valid_until=datetime(2026, 11, 1, tzinfo=UTC),
                supersedes_qualification_id="qual_001",
            ),
        )
        result = registry.resolve_qualification(
            db,
            applicability(),
            qualification_id="qual_001",
            checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        assert result.reason_code == "qualification_applicable"


def test_supersession_rejects_backdating_and_branching_without_mutating_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(
            db,
            signed_record(
                issued_at=datetime(2026, 9, 3, tzinfo=UTC),
                valid_from=datetime(2026, 9, 3, tzinfo=UTC),
            ),
        )
        with pytest.raises(registry.QualificationConflict, match="predates"):
            registry.retain_qualification(
                db,
                signed_record(
                    qualification_id="qual_backdated",
                    issued_at=datetime(2026, 9, 2, tzinfo=UTC),
                    valid_from=datetime(2026, 9, 2, tzinfo=UTC),
                    supersedes_qualification_id="qual_001",
                ),
            )
        registry.retain_qualification(
            db,
            signed_record(
                qualification_id="qual_002",
                issued_at=datetime(2026, 9, 4, tzinfo=UTC),
                valid_from=datetime(2026, 9, 4, tzinfo=UTC),
                valid_until=datetime(2026, 11, 1, tzinfo=UTC),
                supersedes_qualification_id="qual_001",
            ),
        )
        with pytest.raises(registry.QualificationConflict, match="already has a successor"):
            registry.retain_qualification(
                db,
                signed_record(
                    qualification_id="qual_branch",
                    issued_at=datetime(2026, 9, 5, tzinfo=UTC),
                    valid_from=datetime(2026, 9, 5, tzinfo=UTC),
                    valid_until=datetime(2026, 12, 1, tzinfo=UTC),
                    supersedes_qualification_id="qual_001",
                ),
            )
        original = db.get(registry.QualificationRegistryRecord, ("qual_001", "tenant-a"))
        assert original.superseded_by_qualification_id == "qual_002"


def test_non_utc_aware_record_round_trips_without_digest_drift() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    east = timezone(timedelta(hours=5, minutes=30))
    record = signed_record(
        source_window_start=datetime(2026, 8, 1, tzinfo=east),
        source_window_end=datetime(2026, 9, 1, tzinfo=east),
        issued_at=datetime(2026, 9, 1, tzinfo=east),
        valid_from=datetime(2026, 9, 1, tzinfo=east),
        valid_until=datetime(2026, 10, 1, tzinfo=east),
    )
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, record)
        result = registry.resolve_qualification(
            db,
            applicability(
                source_window_start=datetime(2026, 8, 1, tzinfo=east),
                source_window_end=datetime(2026, 9, 1, tzinfo=east),
            ),
            qualification_id=record.qualification_id,
            checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        assert result.reason_code == "qualification_applicable"
        assert result.record_digest == record.record_digest


def test_tampering_and_rollback_switch_fail_closed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record())
        assert (
            registry.resolve_qualification(
                db,
                applicability(),
                qualification_id="qual_001",
                checked_at=datetime(2026, 9, 2, tzinfo=UTC),
                resolution_enabled=False,
            ).reason_code
            == "qualification_resolution_disabled"
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE model_migration_qualifications "
                "SET certificate_artifact_sha256 = :digest WHERE qualification_id = 'qual_001'"
            ),
            {"digest": "0" * 64},
        )
    with Session(engine) as session:
        result = registry.resolve_qualification(
            scoped(session),
            applicability(),
            qualification_id="qual_001",
            checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        assert result.reason_code == "qualification_hash_invalid"


@pytest.mark.parametrize("column", ["cluster_family_set", "grouping_keys"])
def test_nested_unhashable_set_corruption_fails_closed_as_malformed(column: str) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        registry.retain_qualification(scoped(session), signed_record())
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE model_migration_qualifications SET {column} = :value "
                "WHERE tenant_id = 'tenant-a' AND qualification_id = 'qual_001'"
            ),
            {"value": json.dumps(["account_id", []])},
        )
    with Session(engine) as session:
        result = registry.resolve_qualification(
            scoped(session),
            applicability(),
            qualification_id="qual_001",
            checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
        assert result.reason_code == "qualification_malformed"


def test_workload_erasure_removes_registry_and_supersession_lineage() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, signed_record())
        registry.retain_qualification(
            db,
            signed_record(
                qualification_id="qual_002",
                algorithm_version="algo-v2",
                supersedes_qualification_id="qual_001",
            ),
        )
        assert registry.erase_qualifications_for_workload(db, "invoice-agent") == 2
        assert len(list(db.scalars(select(registry.QualificationRegistryRecord)))) == 0


async def test_retention_adapter_erasure_is_tenant_scoped_and_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'qualification-erasure.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        registry.retain_qualification(scoped(session), signed_record())
    with Session(engine) as session:
        registry.retain_qualification(
            scoped(session),
            signed_record(
                qualification_id="qual_other_workload",
                workload="support-agent",
            ),
        )
    with Session(engine) as session:
        registry.retain_qualification(
            scoped(session, "tenant-b"), signed_record(tenant_id="tenant-b")
        )

    eraser = SqlAlchemyEconEventEraser(session_factory=lambda: Session(engine))
    first = await eraser.delete_qualification_lineage(
        "tenant-a", "invoice-agent", idempotency_key="qualification-rte-1"
    )
    replay = await eraser.delete_qualification_lineage(
        "tenant-a", "invoice-agent", idempotency_key="qualification-rte-1"
    )
    reused_for_other_scope = await eraser.delete_qualification_lineage(
        "tenant-a", "support-agent", idempotency_key="qualification-rte-1"
    )

    assert first == replay == 1
    assert reused_for_other_scope == 1
    with Session(engine) as session:
        remaining = list(session.scalars(select(registry.QualificationRegistryRecord)))
        assert {(row.tenant_id, row.workload) for row in remaining} == {
            ("tenant-b", "invoice-agent"),
        }


def test_public_request_accepts_only_an_opaque_qualification_reference() -> None:
    request = public_request()
    assert request.qualification_id == "qual_001"
    payload = request.model_dump(mode="json")
    payload["qualification"] = {"issuer": "client-controlled", "record_digest": "0" * 64}
    with pytest.raises(ValidationError):
        ProbabilisticMigrationRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        ProbabilisticMigrationRequest.model_validate(
            {**request.model_dump(mode="json"), "qualification_id": "../tenant-b/qual_001"}
        )


def test_absent_qualification_reference_preserves_legacy_request_digest() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    request = public_request().model_copy(update={"qualification_id": None})
    with Session(engine) as session:
        db = scoped(session)
        evaluate_and_retain_probabilistic_migration(db, request, evaluated_by="test")
        row = db.scalars(select(ProbabilisticMigrationDecisionRecord)).one()

    readiness = assess_forecast_readiness(
        request.calibration_observations,
        required_metrics=_MODEL_MIGRATION_CALIBRATION_METRICS,
    )
    derived_evidence = request.evidence.model_copy(update={"readiness": readiness})
    legacy_payload = {
        "evidence": derived_evidence.model_dump(mode="json"),
        "forecast_algorithm_version": FORECAST_ALGORITHM_VERSION,
        "policy": request.policy.model_dump(mode="json"),
        "calibration_observations": [
            item.model_dump(mode="json") for item in request.calibration_observations
        ],
        "simulations": request.simulations,
        "seed": request.seed,
    }
    assert (
        row.request_digest
        == hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_valid_stored_resolution_is_lineage_only_and_public_cutoff_stays_closed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    request = public_request()
    policy_digest = hashlib.sha256(
        json.dumps(
            request.policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    record = signed_record(
        policy_digest=policy_digest,
        algorithm_version=FORECAST_ALGORITHM_VERSION,
    )
    application = applicability(
        policy_digest=policy_digest,
        algorithm_version=FORECAST_ALGORITHM_VERSION,
    )
    with Session(engine) as session:
        db = scoped(session)
        registry.retain_qualification(db, record)
        decision = evaluate_and_retain_probabilistic_migration(
            db,
            request,
            evaluated_by="test",
            qualification_applicability=application,
            qualification_checked_at=datetime(2026, 9, 2, tzinfo=UTC),
        )

        assert decision.verdict == "abstain"
        assert decision.recommended_action == "collect_evidence"
        assert "experimental_predictive_reliability_unapproved" in decision.reason_codes
        resolution = decision.evidence_lineage["qualification_resolution"]
        assert resolution["status"] == "eligible_for_next_evidence_stage_only"
        assert resolution["public_authorization"] is False
