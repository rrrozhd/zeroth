from __future__ import annotations

import base64
import hashlib
import inspect
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from release.app_certification import (
    MANDATORY_CHECKS,
    CandidateIdentity,
    CertificationReport,
    CheckResult,
    bind_sbom,
    file_digest,
    finalize_attestation,
    validate_image_archive,
    write_provenance,
    write_report,
)
from release.app_certification.cli import main as certification_main
from release.app_certification.evidence import validate_evidence_subject
from tests.app_certification.test_engine import COMMIT, DIGEST, SOURCE_DIGEST

ZEROTH_COMMIT = "d" * 40
ZEROTH_SOURCE_DIGEST = "sha256:" + "e" * 64
PREDICATE_TYPE = "https://zeroth.dev/app-certification/provenance/v1"


def _identity(image_digest: str = DIGEST) -> CandidateIdentity:
    return CandidateIdentity(
        app_name="reference-app",
        app_commit=COMMIT,
        zeroth_version="0.23.9.9",
        image_reference="reference-app:certification",
        image_digest=image_digest,
        source_digest=SOURCE_DIGEST,
    )


def _materials(sbom: Path, candidate: CandidateIdentity) -> dict[str, str]:
    return {
        "source": candidate.source_digest,
        "image": candidate.image_digest,
        "sbom": file_digest(sbom),
        "zeroth": ZEROTH_SOURCE_DIGEST,
    }


def _bound_report(tmp_path: Path) -> tuple[CandidateIdentity, Path, Path]:
    candidate = _identity()
    sbom = tmp_path / "evidence/app.spdx.json"
    provenance = tmp_path / "evidence/provenance.json"
    sbom.parent.mkdir()
    sbom.write_text(
        json.dumps(
            {
                "spdxVersion": "SPDX-2.3",
                "packages": [{"name": "zeroth-core", "versionInfo": candidate.zeroth_version}],
            }
        ),
        encoding="utf-8",
    )
    bind_sbom(sbom, candidate)
    kwargs = {
        "zeroth_commit": ZEROTH_COMMIT,
        "sbom_digest": file_digest(sbom),
        "build_material_digests": _materials(sbom, candidate),
    }
    supported = inspect.signature(write_provenance).parameters
    write_provenance(provenance, candidate, **{k: v for k, v in kwargs.items() if k in supported})
    report = tmp_path / "report.json"
    write_report(CertificationReport.passed(candidate, sbom, provenance, root=tmp_path), report)
    return candidate, provenance, report


def _bundle(path: Path, provenance: Path, *, signed: bool) -> None:
    envelope: dict[str, object] = {"payload": base64.b64encode(provenance.read_bytes()).decode()}
    if signed:
        envelope["signatures"] = [{"sig": "opaque-signature"}]
    document: dict[str, object] = {"dsseEnvelope": envelope}
    if signed:
        document["verificationMaterial"] = {"certificate": {"rawBytes": "opaque"}}
    path.write_text(json.dumps(document), encoding="utf-8")


def _finalize_kwargs() -> dict[str, str]:
    return {
        "repository": "owner/reference-app",
        "signer_repo": "rrrozhd/zeroth",
        "signer_workflow": "rrrozhd/zeroth/.github/workflows/app-certification.yml",
        "signer_digest": ZEROTH_COMMIT,
    }


def test_finalization_invokes_bound_gh_attestation_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, provenance, report = _bound_report(tmp_path)
    bundle, capture = tmp_path / "bundle.json", tmp_path / "gh-argv.json"
    _bundle(bundle, provenance, signed=True)
    gh = tmp_path / "bin/gh"
    gh.parent.mkdir()
    gh.write_text(
        "#!/usr/bin/env python3\nimport json,sys\n"
        f"json.dump(sys.argv[1:], open({str(capture)!r}, 'w'))\nprint('[]')\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    finalize_attestation(bundle, report, tmp_path, **_finalize_kwargs())
    argv = json.loads(capture.read_text(encoding="utf-8"))
    joined = " ".join(argv)
    assert argv[:2] == ["attestation", "verify"]
    assert candidate.image_reference in joined and candidate.image_digest in joined
    expected = {
        "--bundle": str(bundle),
        "--predicate-type": PREDICATE_TYPE,
        "--cert-oidc-issuer": "https://token.actions.githubusercontent.com",
        "--repo": "owner/reference-app",
        "--signer-repo": "rrrozhd/zeroth",
        "--signer-workflow": _finalize_kwargs()["signer_workflow"],
        "--signer-digest": ZEROTH_COMMIT,
        "--source-digest": candidate.app_commit,
    }
    assert all(
        flag in argv and argv[argv.index(flag) + 1] == value for flag, value in expected.items()
    )


def test_finalization_rejects_payload_only_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, provenance, report = _bound_report(tmp_path)
    bundle = tmp_path / "payload-only.json"
    _bundle(bundle, provenance, signed=False)
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\necho 'no cryptographic signature' >&2\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    with pytest.raises(ValueError, match="signature|verif"):
        finalize_attestation(bundle, report, tmp_path, **_finalize_kwargs())


@pytest.mark.parametrize(
    "packages",
    [
        None,
        [],
        [{"name": "other", "versionInfo": "1"}],
        [{"name": "zeroth-core", "versionInfo": "0.0"}],
    ],
)
def test_sbom_requires_zeroth_package_inventory(tmp_path: Path, packages) -> None:
    candidate = _identity()
    document: dict[str, object] = {"spdxVersion": "SPDX-2.3"}
    if packages is not None:
        document["packages"] = packages
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps(document), encoding="utf-8")
    bind_sbom(sbom, candidate)
    with pytest.raises(ValueError, match="package|zeroth-core|inventory"):
        validate_evidence_subject("sbom", sbom, candidate)


def _archive(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


@pytest.mark.parametrize("kind", ["classic", "oci"])
def test_saved_image_name_matches_candidate_reference(tmp_path: Path, kind: str) -> None:
    config = b'{"rootfs":{"diff_ids":[]}}'
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    path = tmp_path / f"{kind}.tar"
    if kind == "classic":
        manifest = [{"Config": "config.json", "RepoTags": ["other:tag"], "Layers": []}]
        _archive(path, {"manifest.json": json.dumps(manifest).encode(), "config.json": config})
        candidate = _identity(config_digest)
    else:
        config_descriptor = {"digest": config_digest, "size": len(config)}
        manifest = json.dumps(
            {"schemaVersion": 2, "config": config_descriptor, "layers": []},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        descriptor = {
            "digest": digest,
            "size": len(manifest),
            "annotations": {"org.opencontainers.image.ref.name": "other:tag"},
        }
        _archive(
            path,
            {
                "index.json": json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode(),
                f"blobs/sha256/{digest[7:]}": manifest,
                f"blobs/sha256/{config_digest[7:]}": config,
            },
        )
        candidate = _identity(digest)
    with pytest.raises(ValueError, match="name|reference|tag"):
        validate_image_archive(path, candidate)


def test_provenance_round_trips_all_material_bindings(tmp_path: Path) -> None:
    candidate = _identity()
    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        json.dumps(
            {
                "spdxVersion": "SPDX-2.3",
                "packages": [{"name": "zeroth-core", "versionInfo": candidate.zeroth_version}],
            }
        ),
        encoding="utf-8",
    )
    bind_sbom(sbom, candidate)
    provenance = tmp_path / "provenance.json"
    materials = _materials(sbom, candidate)
    write_provenance(
        provenance,
        candidate,
        zeroth_commit=ZEROTH_COMMIT,
        sbom_digest=file_digest(sbom),
        build_material_digests=materials,
    )
    statement = json.loads(provenance.read_text(encoding="utf-8"))
    predicate = statement["predicate"]
    assert statement["subject"] == [
        {"name": candidate.image_reference, "digest": {"sha256": DIGEST[7:]}}
    ]
    assert predicate["source_digest"] == SOURCE_DIGEST
    assert predicate["sbom_digest"] == file_digest(sbom)
    assert predicate["zeroth_version"] == candidate.zeroth_version
    assert predicate["zeroth_commit"] == ZEROTH_COMMIT
    assert predicate["build_material_digests"] == materials
    expected = {
        "zeroth_commit": ZEROTH_COMMIT,
        "sbom_digest": file_digest(sbom),
        "build_material_digests": materials,
    }
    validate_evidence_subject("provenance", provenance, candidate, **expected)
    predicate["build_material_digests"]["zeroth"] = "sha256:" + "0" * 64
    provenance.write_text(json.dumps(statement), encoding="utf-8")
    with pytest.raises(ValueError, match="material|provenance"):
        validate_evidence_subject("provenance", provenance, candidate, **expected)


def test_finalizer_replaces_candidate_authored_report_on_early_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "handoff"
    root.mkdir()
    (root / "report.json").write_text(
        '{"status":"passed","candidate":"untrusted"}', encoding="utf-8"
    )
    monkeypatch.setenv("APP_CHECKOUT", "success")
    monkeypatch.setenv("CERTIFIER_CHECKOUT", "failure")
    assert certification_main(["finalize-workflow", "--root", str(root)]) == 0
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed" and report["candidate"] is None
    assert [item["name"] for item in report["checks"]] == list(MANDATORY_CHECKS)


def test_report_write_avoids_direct_target_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = CertificationReport(
        status="failed",
        candidate=None,
        evidence=None,
        checks=[
            CheckResult(name=name, status="failed", detail="expected failure")
            for name in MANDATORY_CHECKS
        ],
    )
    target = tmp_path / "report.json"
    target.write_text("previous complete report", encoding="utf-8")
    original_open = Path.open

    def guarded_open(self, mode="r", *args, **kwargs):
        if self == target and any(flag in mode for flag in "wax+"):
            raise AssertionError("report target was opened for direct overwrite")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    write_report(report, target)
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "failed"
