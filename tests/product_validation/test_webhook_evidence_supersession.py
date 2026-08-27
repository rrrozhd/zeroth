from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore
from release.product_validation.evidence_mapper import ProductEvidenceSource, _file_failure, _load_source
from release.product_validation.webhook_evidence_supersession import (
    WebhookAtomicityEvidenceSource,
    WebhookEvidenceSource,
    build_webhook_atomicity_product_supersession,
    build_webhook_evidence_supersession,
)


def _source(
    base: Path,
    *,
    criterion_id: str = "webhooks.success",
    record: str = "results.json",
    seal: bool = True,
) -> WebhookEvidenceSource:
    root = base / "campaign-a/evidence/source-root"
    record_root = root / Path(record).parent
    indexed = record_root / "indexed"
    indexed.mkdir(parents=True)
    (indexed / "proof.json").write_text('{"outcome":"delivered"}\n', encoding="utf-8")
    document = {
        "schema_version": 1,
        "completed": True,
        "criteria": [
            {
                "criterion_id": criterion_id,
                "status": "pass",
                "evidence": ["console/proof.json"],
            }
        ],
        "artifacts": [
            {
                "source": "indexed/proof.json",
                "destination": "console/proof.json",
            }
        ],
    }
    (root / record).write_text(json.dumps(document) + "\n", encoding="utf-8")
    if seal:
        EvidenceStore(root).write_checksums()
    return WebhookEvidenceSource(
        label="core",
        campaign="campaign-a",
        bucket="evidence",
        root="source-root",
        record=record,
        criterion_ids=("webhooks.success",),
    )


def test_supersession_normalizes_exact_indexed_proof_and_seals_it(tmp_path: Path) -> None:
    destination = tmp_path / "campaign-a/evidence/superseding-root"

    result = build_webhook_evidence_supersession(
        evidence_base=tmp_path,
        destination=destination,
        sources=(_source(tmp_path),),
    )

    assert result == destination
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert acceptance["criteria"] == [
        {
            "criterion_id": "webhooks.success",
            "evidence": ["console/core/proof.json"],
            "note": None,
            "status": "pass",
        }
    ]
    assert (destination / "console/core/proof.json").is_file()
    assert (destination / "reconciliation/core-source-results.json").is_file()
    assert (destination / "reconciliation/core-source-sha256sums.txt").is_file()
    assert (destination / "SHA256SUMS").is_file()

    source = ProductEvidenceSource(
        campaign="campaign-a",
        bucket="evidence",
        root="superseding-root",
        record="acceptance.json",
        record_kind="acceptance",
    )
    loaded = _load_source(tmp_path, source)
    assertion = loaded.assertions["webhooks.success"]
    assert assertion.status == "pass"
    assert all(_file_failure(loaded, reference) is None for reference in assertion.evidence)


def test_supersession_refuses_alias_instead_of_inventing_exact_criterion(tmp_path: Path) -> None:
    source = _source(tmp_path, criterion_id="PLAYWRIGHT-WEBHOOKS-SUCCESS")

    with pytest.raises(RuntimeError, match="exact passing criterion"):
        build_webhook_evidence_supersession(
            evidence_base=tmp_path,
            destination=tmp_path / "campaign-a/evidence/superseding-root",
            sources=(source,),
        )


def test_supersession_refuses_unsealed_source(tmp_path: Path) -> None:
    source = _source(tmp_path, seal=False)

    with pytest.raises(RuntimeError, match="checksum"):
        build_webhook_evidence_supersession(
            evidence_base=tmp_path,
            destination=tmp_path / "campaign-a/evidence/superseding-root",
            sources=(source,),
        )


def test_supersession_supports_nested_completed_results(tmp_path: Path) -> None:
    source = _source(tmp_path, record="playwright/results.json")

    build_webhook_evidence_supersession(
        evidence_base=tmp_path,
        destination=tmp_path / "campaign-a/evidence/superseding-root",
        sources=(source,),
    )

    assert (
        tmp_path
        / "campaign-a/evidence/superseding-root/console/core/proof.json"
    ).is_file()


def _atomicity_source(
    base: Path,
    *,
    backend: str,
    source_criterion_id: str,
    case_names: tuple[str, ...],
) -> WebhookAtomicityEvidenceSource:
    root = base / f"campaign-a/evidence/{backend}-source"
    evidence = []
    for index, name in enumerate(case_names, start=1):
        relative = f"commands/{index:04d}-{name}.json"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"name": name, "exit_code": 0}) + "\n",
            encoding="utf-8",
        )
        evidence.append(relative)
    (root / "events.ndjson").write_text(
        '{"event_id":"semantic-check"}\n', encoding="utf-8"
    )
    evidence.append("events.ndjson#semantic-check")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "backend": backend,
                "required_case_count": len(case_names),
                "postgres_proven": backend == "postgres",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "acceptance.json").write_text(
        json.dumps(
            {
                "criteria": [
                    {
                        "criterion_id": source_criterion_id,
                        "status": "pass",
                        "evidence": evidence,
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    EvidenceStore(root).write_checksums()
    return WebhookAtomicityEvidenceSource(
        campaign="campaign-a",
        bucket="evidence",
        root=f"{backend}-source",
        backend=backend,
    )


def test_atomicity_supersession_emits_exact_product_ids_after_semantic_validation(
    tmp_path: Path,
) -> None:
    sqlite = _atomicity_source(
        tmp_path,
        backend="sqlite",
        source_criterion_id="WEBHOOKS-TRANSACTIONAL-STATE-AND-AUDIT-D013",
        case_names=(
            "subscription-create-rollback",
            "subscription-deactivate-rollback",
            "dead-letter-replay-rollback",
            "delivery-enqueue-rollback",
            "delivery-fanout-rollback",
            "delivery-unsigned-fail-closed",
            "delivery-chain-valid",
            "delivery-chain-head-rollback",
            "delivery-delivered-rollback",
            "delivery-failed-rollback",
            "delivery-dead-letter-rollback",
            "delivery-lost-fence",
            "subscription-tenant-collision",
            "subscription-audit-sanitization",
            "delivery-failure-audit-sanitization",
            "delivery-dead-letter-linkage",
        ),
    )
    postgres = _atomicity_source(
        tmp_path,
        backend="postgres",
        source_criterion_id="WEBHOOKS-POSTGRES-ATOMICITY-D013",
        case_names=(
            "subscription-create-rollback",
            "subscription-deactivate-rollback",
            "dead-letter-replay-rollback",
        ),
    )

    destination = tmp_path / "campaign-a/evidence/product-atomicity"
    build_webhook_atomicity_product_supersession(
        evidence_base=tmp_path,
        destination=destination,
        sources=(sqlite, postgres),
    )

    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert {
        row["criterion_id"] for row in acceptance["criteria"]
    } == {
        "webhooks.transactional-state-and-audit-sqlite",
        "webhooks.transactional-state-and-audit-postgres",
    }
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    assert (destination / "reconciliation/sqlite/source-acceptance.json").is_file()
    assert (destination / "reconciliation/postgres/source-acceptance.json").is_file()


def test_atomicity_supersession_refuses_semantically_incomplete_matrix(
    tmp_path: Path,
) -> None:
    sqlite = _atomicity_source(
        tmp_path,
        backend="sqlite",
        source_criterion_id="WEBHOOKS-TRANSACTIONAL-STATE-AND-AUDIT-D013",
        case_names=("subscription-create-rollback",),
    )

    with pytest.raises(RuntimeError, match="semantic case matrix"):
        build_webhook_atomicity_product_supersession(
            evidence_base=tmp_path,
            destination=tmp_path / "campaign-a/evidence/product-atomicity",
            sources=(sqlite,),
        )
