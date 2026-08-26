from __future__ import annotations

import base64
import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


COMMIT = "1" * 40
IMAGE_DIGEST = "sha256:" + "2" * 64
SOURCE_DIGEST = "sha256:" + "3" * 64
ZEROTH_COMMIT = "4" * 40


def test_release_rejects_unsigned_promotion_evidence(tmp_path) -> None:
    promotion = importlib.import_module("release.app_certification.promotion")
    models = importlib.import_module("release.app_certification.models")
    evidence = importlib.import_module("release.app_certification.evidence")
    signing = importlib.import_module("zeroth.platform.signing")

    candidate = models.CandidateIdentity(
        app_name="support-agent",
        app_commit=COMMIT,
        zeroth_version="0.23.10.2",
        image_reference="registry.example/support-agent",
        image_digest=IMAGE_DIGEST,
        source_digest=SOURCE_DIGEST,
    )
    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        '{"spdxVersion":"SPDX-2.3","packages":'
        '[{"name":"zeroth-core","versionInfo":"0.23.10.2"}]}\n',
        encoding="utf-8",
    )
    evidence.bind_sbom(sbom, candidate)
    provenance = tmp_path / "provenance.json"
    evidence.write_provenance(
        provenance,
        candidate,
        zeroth_commit=ZEROTH_COMMIT,
        sbom_digest=models.file_digest(sbom),
        build_material_digests={
            "source": SOURCE_DIGEST,
            "image": IMAGE_DIGEST,
            "sbom": models.file_digest(sbom),
            "lock": "sha256:" + "5" * 64,
        },
    )
    report_path = tmp_path / "report.json"
    models.write_report(
        models.CertificationReport.passed(candidate, sbom, provenance, root=tmp_path),
        report_path,
    )
    signer = signing.EnvHmacSigner(key_id="certifier-1", keys={"certifier-1": b"secret"})
    issued_at = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="finalized"):
        promotion.issue_promotion_receipt(
            report_path,
            root=tmp_path,
            signer=signer,
            tenant_id="tenant-a",
            workspace_id="workspace-a",
            environments=("test", "production"),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(hours=1),
            certification_id="a" * 32,
        )


def test_release_issues_receipt_only_after_finalized_attestation_verification(
    tmp_path, monkeypatch
) -> None:
    promotion = importlib.import_module("release.app_certification.promotion")
    receipt = importlib.import_module("zeroth.service.certifications.receipt")
    models = importlib.import_module("release.app_certification.models")
    evidence = importlib.import_module("release.app_certification.evidence")
    signing = importlib.import_module("zeroth.platform.signing")
    candidate = models.CandidateIdentity(
        app_name="support-agent",
        app_commit=COMMIT,
        zeroth_version="0.23.10.2",
        image_reference="registry.example/support-agent",
        image_digest=IMAGE_DIGEST,
        source_digest=SOURCE_DIGEST,
    )
    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        '{"spdxVersion":"SPDX-2.3","packages":'
        '[{"name":"zeroth-core","versionInfo":"0.23.10.2"}]}\n',
        encoding="utf-8",
    )
    evidence.bind_sbom(sbom, candidate)
    provenance = tmp_path / "provenance.json"
    evidence.write_provenance(
        provenance,
        candidate,
        zeroth_commit=ZEROTH_COMMIT,
        sbom_digest=models.file_digest(sbom),
        build_material_digests={
            "source": SOURCE_DIGEST,
            "image": IMAGE_DIGEST,
            "sbom": models.file_digest(sbom),
            "lock": "sha256:" + "5" * 64,
        },
    )
    report_path = tmp_path / "report.json"
    models.write_report(
        models.CertificationReport.passed(candidate, sbom, provenance, root=tmp_path),
        report_path,
    )
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            {"dsseEnvelope": {"payload": base64.b64encode(provenance.read_bytes()).decode()}}
        ),
        encoding="utf-8",
    )
    verification_calls: list[list[str]] = []

    def verified(argv, **kwargs):
        verification_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(evidence.subprocess, "run", verified)
    trust = {
        "repository": "owner/support-agent",
        "signer_repo": "rrrozhd/zeroth",
        "signer_workflow": "rrrozhd/zeroth/.github/workflows/app-certification.yml",
        "signer_digest": ZEROTH_COMMIT,
    }
    evidence.finalize_attestation(bundle, report_path, tmp_path, **trust)
    signer = signing.EnvHmacSigner(key_id="certifier-1", keys={"certifier-1": b"secret"})
    issued_at = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    signed = promotion.issue_promotion_receipt(
        report_path,
        root=tmp_path,
        signer=signer,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        environments=("test", "production"),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(hours=1),
        certification_id="a" * 32,
        **trust,
    )

    assert receipt.verify_promotion_receipt(signed, signer) is True
    assert signed.payload.app_commit == COMMIT
    assert signed.payload.image_digest == IMAGE_DIGEST
    assert signed.payload.evidence_digest == models.evidence_binding_digest(
        models.CertificationReport.model_validate_json(report_path.read_text()).evidence
    )
    assert signed.payload.report_digest == models.file_digest(report_path)
    assert len(verification_calls) == 2
    assert all("--deny-self-hosted-runners" in call for call in verification_calls)

    with pytest.raises(ValueError, match="self-authored"):
        promotion.issue_promotion_receipt(
            report_path,
            root=tmp_path,
            signer=signer,
            tenant_id="tenant-a",
            environments=("production",),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(hours=1),
            repository="owner/support-agent",
            signer_repo="OWNER/SUPPORT-AGENT",
            signer_workflow="owner/support-agent/.github/workflows/certify.yml",
            signer_digest=ZEROTH_COMMIT,
        )

    provenance.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        promotion.issue_promotion_receipt(
            report_path,
            root=tmp_path,
            signer=signer,
            tenant_id="tenant-a",
            environments=("production",),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(hours=1),
            **trust,
        )


def test_promotion_receipt_fails_closed_for_missing_or_unknown_signature() -> None:
    receipt = importlib.import_module("zeroth.service.certifications.receipt")
    signing = importlib.import_module("zeroth.platform.signing")
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    payload = receipt.PromotionReceiptPayload(
        certification_id="b" * 32,
        tenant_id="tenant-a",
        app_name="support-agent",
        app_commit=COMMIT,
        zeroth_version="0.23.10.2",
        image_reference="registry.example/support-agent",
        image_digest=IMAGE_DIGEST,
        source_digest=SOURCE_DIGEST,
        evidence_digest="sha256:" + "6" * 64,
        report_digest="sha256:" + "7" * 64,
        environments=("production",),
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    signer = signing.EnvHmacSigner(key_id="known", keys={"known": b"secret"})
    signed = receipt.sign_promotion_receipt(payload, signer)

    assert receipt.verify_promotion_receipt(
        signed.model_copy(
            update={
                "signature": None,
                "signing_key_id": None,
                "signing_algorithm": None,
            }
        ),
        signer,
    ) is False
    assert receipt.verify_promotion_receipt(
        signed.model_copy(update={"signing_key_id": "unknown"}), signer
    ) is False
