from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta


COMMIT = "1" * 40
IMAGE_DIGEST = "sha256:" + "2" * 64
SOURCE_DIGEST = "sha256:" + "3" * 64
ZEROTH_COMMIT = "4" * 40


def test_release_issues_a_signed_exact_promotion_receipt(tmp_path) -> None:
    promotion = importlib.import_module("release.app_certification.promotion")
    receipt = importlib.import_module("zeroth.service.certifications.receipt")
    models = importlib.import_module("release.app_certification.models")
    evidence = importlib.import_module("release.app_certification.evidence")
    signing = importlib.import_module("zeroth.platform.signing")

    candidate = models.CandidateIdentity(
        app_name="support-agent",
        app_commit=COMMIT,
        zeroth_version="0.23.10",
        image_reference="registry.example/support-agent",
        image_digest=IMAGE_DIGEST,
        source_digest=SOURCE_DIGEST,
    )
    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        '{"spdxVersion":"SPDX-2.3","packages":'
        '[{"name":"zeroth-core","versionInfo":"0.23.10"}]}\n',
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
    )

    assert receipt.verify_promotion_receipt(signed, signer) is True
    assert signed.payload.app_commit == COMMIT
    assert signed.payload.image_digest == IMAGE_DIGEST
    assert signed.payload.evidence_digest == models.evidence_binding_digest(
        models.CertificationReport.model_validate_json(report_path.read_text()).evidence
    )
    assert signed.payload.report_digest == models.file_digest(report_path)
    assert signed.payload.environments == ("test", "production")

    tampered = signed.model_copy(
        update={"payload": signed.payload.model_copy(update={"image_digest": "sha256:" + "9" * 64})}
    )
    assert receipt.verify_promotion_receipt(tampered, signer) is False


def test_promotion_receipt_fails_closed_for_missing_or_unknown_signature() -> None:
    receipt = importlib.import_module("zeroth.service.certifications.receipt")
    signing = importlib.import_module("zeroth.platform.signing")
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    payload = receipt.PromotionReceiptPayload(
        certification_id="b" * 32,
        tenant_id="tenant-a",
        app_name="support-agent",
        app_commit=COMMIT,
        zeroth_version="0.23.10",
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
