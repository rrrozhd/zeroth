from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from release.product_validation.evidence_mapper import (
    ProductEvidenceSourceMap,
    audit_product_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_MAP_PATH = ROOT / "release/product_validation/evidence-source-map-v1.json"
SOURCE_MAP_SCHEMA_PATH = (
    ROOT / "release/product_validation/evidence-source-map-schema-v1.json"
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _seal(root: Path) -> None:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            relative = path.relative_to(root).as_posix()
            rows.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    (root / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _index(*criteria: str, status: str = "pass") -> dict[str, object]:
    return {
        "schema_version": 1,
        "catalog_id": "catalog-v1",
        "entries": [
            {
                "capability_id": "example",
                "status": status,
                "evidence_criteria": list(criteria),
            }
        ],
    }


def _source_map(
    *,
    record_kind: str = "acceptance",
    record: str = "acceptance.json",
    files: list[str] | None = None,
    unmapped: dict[str, str] | None = None,
) -> ProductEvidenceSourceMap:
    return ProductEvidenceSourceMap.model_validate(
        {
            "schema_version": 1,
            "catalog_id": "catalog-v1",
            "sources": {
                "accepted": {
                    "campaign": "campaign-a",
                    "bucket": "evidence",
                    "root": "accepted-root-1",
                    "record": record,
                    "record_kind": record_kind,
                }
            },
            "mappings": [
                {
                    "criterion_id": "product.example",
                    "source": "accepted",
                    "source_criterion_ids": ["source.example.exact"],
                    "files": files or ["proof.json"],
                }
            ],
            "unmapped": unmapped or {},
        }
    )


def _accepted_root(
    base: Path,
    *,
    record: str = "acceptance.json",
    completed: bool | None = None,
    status: str = "pass",
    criterion_key: str = "criterion_id",
) -> Path:
    root = base / "campaign-a/evidence/accepted-root-1"
    _write_json(root / "proof.json", {"outcome": "accepted"})
    document: dict[str, object] = {
        "criteria": [
            {
                criterion_key: "source.example.exact",
                "status": status,
                "evidence": ["proof.json"],
            }
        ]
    }
    if completed is not None:
        document["completed"] = completed
    _write_json(root / record, document)
    _seal(root)
    return root


def test_accepts_campaign_qualified_exact_pass_with_sealed_required_files(
    tmp_path: Path,
) -> None:
    _accepted_root(tmp_path)

    result = audit_product_evidence(
        _index("product.example"),
        evidence_base=tmp_path,
        source_map=_source_map(),
    )

    assert result.complete is True
    assert result.declared_passes_valid is True
    assert result.counts == {"pass": 1, "fail": 0, "unmapped": 0}
    assert result.entries[0].source_location == (
        "campaign-a/evidence/accepted-root-1/acceptance.json"
    )
    assert result.entries[0].source_criterion_ids == ("source.example.exact",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("campaign", "../campaign-a"),
        ("bucket", "/evidence"),
        ("root", "root/child"),
        ("record", "../acceptance.json"),
    ],
)
def test_source_locations_reject_unsafe_paths(field: str, value: str) -> None:
    document = _source_map().model_dump(mode="json")
    document["sources"]["accepted"][field] = value

    with pytest.raises(ValidationError, match="safe"):
        ProductEvidenceSourceMap.model_validate(document)


def test_results_source_requires_explicit_completed_true(tmp_path: Path) -> None:
    _accepted_root(tmp_path, record="results.json", completed=False)

    result = audit_product_evidence(
        _index("product.example"),
        evidence_base=tmp_path,
        source_map=_source_map(record_kind="completed_results", record="results.json"),
    )

    assert result.counts == {"pass": 0, "fail": 1, "unmapped": 0}
    assert "completed=true" in (result.entries[0].note or "")


@pytest.mark.parametrize("mutation", ["unsealed", "checksum", "inventory"])
def test_seal_failures_fail_closed(tmp_path: Path, mutation: str) -> None:
    root = _accepted_root(tmp_path)
    if mutation == "unsealed":
        (root / "SHA256SUMS").unlink()
    elif mutation == "checksum":
        _write_json(root / "proof.json", {"outcome": "tampered"})
    else:
        _write_json(root / "unlisted.json", {"outcome": "not sealed"})

    result = audit_product_evidence(
        _index("product.example"),
        evidence_base=tmp_path,
        source_map=_source_map(),
    )

    assert result.counts == {"pass": 0, "fail": 1, "unmapped": 0}
    assert "checksum" in (result.entries[0].note or "")


def test_secret_scan_failure_fails_closed_even_when_checksum_matches(tmp_path: Path) -> None:
    root = _accepted_root(tmp_path)
    (root / "SHA256SUMS").unlink()
    _write_json(root / "leak.json", {"authorization": "Bearer exposed-value"})
    _seal(root)

    result = audit_product_evidence(
        _index("product.example"),
        evidence_base=tmp_path,
        source_map=_source_map(),
    )

    assert result.counts == {"pass": 0, "fail": 1, "unmapped": 0}
    assert "secret" in (result.entries[0].note or "")


def test_required_and_assertion_referenced_files_must_exist_in_seal(tmp_path: Path) -> None:
    root = _accepted_root(tmp_path)
    (root / "SHA256SUMS").unlink()
    document = json.loads((root / "acceptance.json").read_text())
    document["criteria"][0]["evidence"] = ["missing-source-proof.json"]
    _write_json(root / "acceptance.json", document)
    _seal(root)

    result = audit_product_evidence(
        _index("product.example"),
        evidence_base=tmp_path,
        source_map=_source_map(files=["missing-required-proof.json"]),
    )

    assert result.counts == {"pass": 0, "fail": 1, "unmapped": 0}
    note = result.entries[0].note or ""
    assert "missing-required-proof.json" in note
    assert "missing-source-proof.json" in note


@pytest.mark.parametrize(
    ("status", "criterion_key", "expected"),
    [
        ("fail", "criterion_id", "source.example.exact=fail"),
        ("pass", "id", "criterion_id"),
    ],
)
def test_only_exact_passing_criterion_id_assertions_are_accepted(
    tmp_path: Path,
    status: str,
    criterion_key: str,
    expected: str,
) -> None:
    _accepted_root(tmp_path, status=status, criterion_key=criterion_key)

    result = audit_product_evidence(
        _index("product.example"),
        evidence_base=tmp_path,
        source_map=_source_map(),
    )

    assert result.counts == {"pass": 0, "fail": 1, "unmapped": 0}
    assert expected in (result.entries[0].note or "")


def test_unmapped_alias_is_reported_without_relabeling_index_status(tmp_path: Path) -> None:
    source_map = ProductEvidenceSourceMap.model_validate(
        {
            "schema_version": 1,
            "catalog_id": "catalog-v1",
            "sources": {},
            "mappings": [],
            "unmapped": {
                "product.semantic-alias": "no exact accepted source criterion id"
            },
        }
    )
    index = _index("product.semantic-alias")

    result = audit_product_evidence(
        index,
        evidence_base=tmp_path,
        source_map=source_map,
    )

    assert index["entries"][0]["status"] == "pass"
    assert result.complete is False
    assert result.declared_passes_valid is False
    assert result.counts == {"pass": 0, "fail": 0, "unmapped": 1}
    assert result.entries[0].note == "no exact accepted source criterion id"


def test_undeclared_index_criterion_is_explicitly_unmapped(tmp_path: Path) -> None:
    source_map = ProductEvidenceSourceMap.model_validate(
        {
            "schema_version": 1,
            "catalog_id": "catalog-v1",
            "sources": {},
            "mappings": [],
            "unmapped": {},
        }
    )

    result = audit_product_evidence(
        _index("product.not-reviewed", status="blocked"),
        evidence_base=tmp_path,
        source_map=source_map,
    )

    assert result.complete is False
    assert result.declared_passes_valid is True
    assert result.entries[0].status == "unmapped"
    assert result.entries[0].note == "no explicit accepted-source mapping"


def test_exact_blocked_capability_proof_does_not_relabel_capability(tmp_path: Path) -> None:
    _accepted_root(tmp_path)
    index = _index("product.example", status="blocked")

    result = audit_product_evidence(
        index,
        evidence_base=tmp_path,
        source_map=_source_map(),
    )

    assert result.entries[0].status == "pass"
    assert result.entries[0].capability_status == "blocked"
    assert index["entries"][0]["status"] == "blocked"
    assert result.declared_passes_valid is True


def test_source_map_rejects_duplicate_targets_and_unknown_index_targets(tmp_path: Path) -> None:
    document = _source_map().model_dump(mode="json")
    document["mappings"].append(document["mappings"][0])
    with pytest.raises(ValidationError, match="unique criterion targets"):
        ProductEvidenceSourceMap.model_validate(document)

    source_map = _source_map()
    with pytest.raises(ValueError, match="unknown product criterion"):
        audit_product_evidence(
            _index("another.criterion"),
            evidence_base=tmp_path,
            source_map=source_map,
        )


def test_source_map_catalog_id_must_match_index(tmp_path: Path) -> None:
    document = _source_map().model_dump(mode="json")
    document["catalog_id"] = "wrong-catalog"

    with pytest.raises(ValueError, match="catalog_id"):
        audit_product_evidence(
            _index("product.example"),
            evidence_base=tmp_path,
            source_map=ProductEvidenceSourceMap.model_validate(document),
        )


def test_checked_in_source_map_is_versioned_and_only_seeds_exact_reviewed_sources() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    index = json.loads((ROOT / "release/product_validation/evidence-index-v1.json").read_text())

    assert source_map.schema_version == 1
    assert source_map.catalog_id == "zeroth-published-product-v1"
    mapped = {mapping.criterion_id for mapping in source_map.mappings}
    indexed = {
        criterion
        for entry in index["entries"]
        for criterion in entry["evidence_criteria"]
    }
    assert mapped | set(source_map.unmapped) == indexed
    assert {
        "resilient-http.field-contract",
        "resilient-http.retry-success",
        "resilient-http.timeout-exhaustion",
        "resilient-http.circuit-open",
        "resilient-http.recovery",
        "resilient-http.sanitized-signed-audit",
        "resilient-http.zero-provider-economics",
        "runs.ambiguous-operation-control-conditional",
        "runs.ambiguous-operation-authorized-resolution",
        "runs.ambiguous-operation-signed-refresh-no-replay",
        "economics.tenant-deployment-run-measured-reconciliation",
        "economics.estimated-failed-run-tax-separation",
        "artifacts.workflow-output",
        "artifacts.preview-download",
        "artifacts.tenant-isolation",
        "artifacts.expiry",
        "artifacts.erasure",
        "templates.configured",
        "templates.persistence",
        "templates.secret-redaction",
        "templates.field-validation",
        "templates.versioning",
        "templates.twin-tenant-scope-isolation",
        "templates.refresh-persistence",
        "templates.backend-restart-persistence",
        "templates.delete-version",
        "templates.keyboard",
        "templates.accessibility",
    }.issubset(mapped)
    assert mapped.isdisjoint(
        {
            "batching.provider-economics",
            "templates.live-rendered-execution",
            "rightsizing.measured-experiment",
            "rightsizing.cost-reconciliation",
        }
    )
    economics_source = source_map.sources["economics-provider-independent-ui-20260826-1"]
    assert economics_source.campaign == "evaluation-studio-v1"
    assert economics_source.root == "economics-provider-independent-ui-20260826-1"
    assert {
        mapping.criterion_id: mapping.files
        for mapping in source_map.mappings
        if mapping.source == "economics-provider-independent-ui-20260826-1"
    } == {
        "economics.tenant-deployment-run-measured-reconciliation": (
            "screenshots/workflow-economics.png",
            "manifest.json",
        ),
        "economics.estimated-failed-run-tax-separation": (
            "screenshots/rightsizing-economics.png",
            "manifest.json",
        ),
    }
    repaired_source = source_map.sources["templates-artifacts-product-exact-20260826-2"]
    assert repaired_source.campaign == "evaluation-studio-v1"
    assert repaired_source.root == "templates-artifacts-product-exact-20260826-2"
    repaired_criteria = {
        "artifacts.workflow-output",
        "artifacts.preview-download",
        "artifacts.tenant-isolation",
        "artifacts.expiry",
        "artifacts.erasure",
        "templates.configured",
        "templates.persistence",
        "templates.secret-redaction",
        "templates.field-validation",
        "templates.versioning",
        "templates.twin-tenant-scope-isolation",
        "templates.refresh-persistence",
        "templates.backend-restart-persistence",
        "templates.delete-version",
        "templates.keyboard",
        "templates.accessibility",
    }
    repaired_mappings = {
        mapping.criterion_id: mapping
        for mapping in source_map.mappings
        if mapping.source == "templates-artifacts-product-exact-20260826-2"
    }
    assert set(repaired_mappings) == repaired_criteria
    assert repaired_criteria.isdisjoint(source_map.unmapped)
    assert "templates.live-rendered-execution" in source_map.unmapped
    for mapping in source_map.mappings:
        assert mapping.source_criterion_ids == (mapping.criterion_id,)
        matching_entries = [
            entry
            for entry in index["entries"]
            if mapping.criterion_id in entry["evidence_criteria"]
        ]
        assert matching_entries
        source = source_map.sources[mapping.source]
        cited_names = {source.root, f"{source.bucket}/{source.root}"}
        for entry in matching_entries:
            assert cited_names.intersection(
                [entry["source_root"], *entry.get("supplemental_source_roots", [])]
            )
    atomicity_mappings = {
        mapping.criterion_id: mapping
        for mapping in source_map.mappings
        if mapping.criterion_id.startswith("webhooks.transactional-state-and-audit-")
    }
    assert set(atomicity_mappings) == {
        "webhooks.transactional-state-and-audit-sqlite",
        "webhooks.transactional-state-and-audit-postgres",
    }
    assert {
        mapping.source for mapping in atomicity_mappings.values()
    } == {"webhook-atomicity-product-ids-accepted-20260826-1"}


def test_checked_in_source_map_closes_provider_independent_product_surface_groups() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    expected = {
        "CONTEXT-PERSISTENCE-COMPACTION-007",
        "CONTEXT-RUNTIME-COMPACTION-006",
        "audit.run-thread-deployment-graph-node-correlation",
        "batching.live-studio-three-repetitions",
        "deployments.exact-health-version",
        "deployments.rollback-rollforward-history",
        "deployments.signed-attestation",
        "runs.curl-exact-execution",
        "runs.signed-audit",
        "ui.focus-visible-order",
        "ui.operational-surfaces",
        "ui.reduced-motion",
    }

    mappings = {mapping.criterion_id: mapping for mapping in source_map.mappings}
    assert expected.isdisjoint(source_map.unmapped)
    assert expected <= mappings.keys()
    assert {
        mappings[criterion_id].source for criterion_id in expected
    } == {"provider-independent-surface-closure-accepted-20260826-1"}
    assert "batching.provider-economics" in source_map.unmapped


def test_checked_in_source_map_uses_dual_browser_resilient_http_evidence() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    expected = {
        "resilient-http.field-contract",
        "resilient-http.retry-success",
        "resilient-http.timeout-exhaustion",
        "resilient-http.circuit-open",
        "resilient-http.recovery",
        "resilient-http.sanitized-signed-audit",
        "resilient-http.zero-provider-economics",
    }
    mappings = {
        mapping.criterion_id: mapping
        for mapping in source_map.mappings
        if mapping.criterion_id in expected
    }

    assert set(mappings) == expected
    assert {mapping.source for mapping in mappings.values()} == {
        "resilient-http-dual-browser-accepted-20260826-1"
    }
    for mapping in mappings.values():
        assert any("desktop-1440-" in path for path in mapping.files)
        assert any("webkit-1440-" in path for path in mapping.files)


def test_checked_in_source_map_closes_fresh_disposable_ui_and_runs_journeys() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    expected = {
        "ui.sidebar-active-route-navigation",
        "ui.empty-canvas-authoring",
        "ui.node-placement",
        "ui.node-inspector",
        "ui.contract-configuration",
        "ui.undo-redo-refresh",
        "ui.preflight-error-focus",
        "ui.canvas-gestures",
        "ui.connector-configuration",
        "ui.approval-configuration",
        "runs.thread-continuation-refresh",
        "runs.interrupt-cancel-late-result-fence",
        "runs.filter-failure-replay",
    }

    mappings = {mapping.criterion_id: mapping for mapping in source_map.mappings}
    assert expected.isdisjoint(source_map.unmapped)
    assert expected <= mappings.keys()
    assert {
        mappings[criterion_id].source for criterion_id in expected
    } == {"provider-independent-ui-runs-live-accepted-20260826-1"}


def test_checked_in_source_map_repairs_exact_retention_erasure_evidence() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    expected = {
        "retention-and-erasure.nonheld-run-erasure",
        "retention-and-erasure.tenant-erasure",
        "retention-and-erasure.audit-chain-after-erasure",
        "retention-and-erasure.economics-after-erasure",
        "retention.erasure.configured-econ-adapter-reached",
    }
    mappings = {
        mapping.criterion_id: mapping
        for mapping in source_map.mappings
        if mapping.criterion_id in expected
    }

    assert set(mappings) == expected
    assert {
        mapping.source for mapping in mappings.values()
    } == {"retention-erasure-evidence-repair-accepted-20260826-1"}
    assert expected.isdisjoint(source_map.unmapped)


def test_checked_in_source_map_repairs_exact_templates_and_artifacts_evidence() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    expected = {
        "artifacts.workflow-output",
        "artifacts.preview-download",
        "artifacts.tenant-isolation",
        "artifacts.expiry",
        "artifacts.erasure",
        "templates.configured",
        "templates.persistence",
        "templates.secret-redaction",
        "templates.field-validation",
        "templates.versioning",
        "templates.twin-tenant-scope-isolation",
        "templates.refresh-persistence",
        "templates.backend-restart-persistence",
        "templates.delete-version",
        "templates.keyboard",
        "templates.accessibility",
    }
    mappings = {
        mapping.criterion_id: mapping
        for mapping in source_map.mappings
        if mapping.criterion_id in expected
    }

    assert set(mappings) == expected
    assert {mapping.source for mapping in mappings.values()} == {
        "templates-artifacts-product-exact-20260826-2"
    }
    assert expected.isdisjoint(source_map.unmapped)
    assert "templates.live-rendered-execution" in source_map.unmapped


def test_checked_in_source_map_repairs_exact_governance_economics_evidence() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    expected = {
        "economics.quality-verdict-nonrewriting",
        "economics-and-rightsizing.enforcement-approval",
        "workflow3.negative-rejection-zero-marker",
    }
    mappings = {
        mapping.criterion_id: mapping
        for mapping in source_map.mappings
        if mapping.criterion_id in expected
    }

    assert set(mappings) == expected
    assert {
        mapping.source for mapping in mappings.values()
    } == {"governance-economics-evidence-repair-accepted-20260826-1"}
    assert expected.isdisjoint(source_map.unmapped)


def test_checked_in_source_map_repairs_exact_webhook_evidence_without_aliases() -> None:
    source_map = ProductEvidenceSourceMap.model_validate_json(SOURCE_MAP_PATH.read_text())
    index = json.loads((ROOT / "release/product_validation/evidence-index-v1.json").read_text())
    webhook_entry = next(
        entry for entry in index["entries"] if entry["capability_id"] == "webhooks"
    )
    expected = {
        "webhooks.success",
        "webhooks.filtering",
        "webhooks.dead-letter-replay",
        "webhooks.tenant-isolation",
        "webhooks.deactivation-stops-delivery",
        "webhooks.signed-audit",
        "webhooks.replay-error-display",
        "webhooks.timeout",
        "webhooks.retry-exhaustion",
        "webhooks.runtime-correlation",
        "webhooks.sink-unavailable",
        "webhooks.timeout-after-commit",
        "webhooks.approval-requested-live",
        "webhooks.approval-resolved-live",
        "webhooks.approval-escalated-live",
        "webhooks.approval-event-unique",
        "webhooks.approval-signed-audit",
        "webhooks.role-authorization",
    }
    mappings = {
        mapping.criterion_id: mapping
        for mapping in source_map.mappings
        if mapping.criterion_id in expected
    }

    assert "webhooks-exact-supersession-accepted-20260826-1" in (
        webhook_entry["supplemental_source_roots"]
    )
    assert set(mappings) == expected
    assert {
        mapping.source for mapping in mappings.values()
    } == {"webhooks-exact-supersession-accepted-20260826-1"}
    assert all(
        mapping.source_criterion_ids == (mapping.criterion_id,)
        for mapping in mappings.values()
    )
    assert expected.isdisjoint(source_map.unmapped)
    unresolved = {
        criterion
        for criterion in webhook_entry["evidence_criteria"]
        if criterion.startswith("webhooks.")
        and criterion not in {mapping.criterion_id for mapping in source_map.mappings}
    }
    assert unresolved == set()


def test_checked_in_source_map_conforms_to_versioned_json_schema() -> None:
    schema = json.loads(SOURCE_MAP_SCHEMA_PATH.read_text())
    source_map = json.loads(SOURCE_MAP_PATH.read_text())

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(source_map)
    assert schema["$id"] == (
        "https://zeroth.dev/schemas/product-evidence-source-map-v1.json"
    )
