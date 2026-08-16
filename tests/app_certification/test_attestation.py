from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

from release.app_certification import (
    CandidateIdentity,
    CertificationReport,
    bind_sbom,
    file_digest,
    write_provenance,
    write_report,
)
from release.app_certification.cli import main as app_certification_main
from tests.app_certification.test_hardening import COMMIT, _write_archive, identity


def _candidate_archives(tmp_path: Path) -> tuple[Path, Path, CandidateIdentity]:
    layer = b"candidate-layer"
    layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    config = json.dumps({"rootfs": {"diff_ids": [layer_digest]}}).encode()
    image_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    image = tmp_path / "image.tar"
    _write_archive(
        image,
        {
            "manifest.json": json.dumps(
                [
                    {
                        "Config": "config.json",
                        "RepoTags": [identity().image_reference],
                        "Layers": ["layer.tar"],
                    }
                ]
            ).encode(),
            "config.json": config,
            "layer.tar": layer,
        },
    )
    source = tmp_path / "source.tar"
    with tarfile.open(
        source, "w", format=tarfile.PAX_FORMAT, pax_headers={"comment": COMMIT}
    ) as archive:
        content = b"committed source"
        info = tarfile.TarInfo("app.py")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    candidate = identity().model_copy(
        update={"image_digest": image_digest, "source_digest": file_digest(source)}
    )
    return image, source, candidate


def _candidate_evidence(
    tmp_path: Path, candidate: CandidateIdentity
) -> tuple[Path, Path, Path, Path, Path, Path]:
    root, report, verdict = tmp_path / "root", tmp_path / "report.json", tmp_path / "verdict.json"
    sbom, provenance = root / "evidence/app.spdx.json", root / "evidence/provenance.json"
    wheel = tmp_path / "zeroth-core.whl"
    requirements = tmp_path / "requirements-image.txt"
    wheel.write_bytes(b"trusted wheel")
    requirements.write_text("zeroth-core==0.23.9.8.1\n", encoding="utf-8")
    sbom.parent.mkdir(parents=True)
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
    write_provenance(
        provenance,
        candidate,
        zeroth_commit="d" * 40,
        sbom_digest=file_digest(sbom),
        build_material_digests={
            "source": candidate.source_digest,
            "image": candidate.image_digest,
            "sbom": file_digest(sbom),
            "zeroth_wheel": file_digest(wheel),
            "requirements_lock": file_digest(requirements),
        },
    )
    write_report(CertificationReport.passed(candidate, sbom, provenance, root=root), report)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            {"dsseEnvelope": {"payload": base64.b64encode(provenance.read_bytes()).decode()}}
        )
    )
    return root, report, verdict, bundle, wheel, requirements


def _handoff_args(
    *,
    report: Path,
    root: Path,
    image: Path,
    source: Path,
    candidate: CandidateIdentity,
    wheel: Path,
    requirements: Path,
    verdict: Path,
) -> list[str]:
    return [
        "--report",
        str(report),
        "--root",
        str(root),
        "--image-archive",
        str(image),
        "--source-archive",
        str(source),
        "--app-commit",
        COMMIT,
        "--zeroth-version",
        candidate.zeroth_version,
        "--zeroth-commit",
        "d" * 40,
        "--certifier-wheel",
        str(wheel),
        "--requirements-lock",
        str(requirements),
        "--verdict",
        str(verdict),
    ]


def test_finalize_attestation_reissues_digest_bound_handoff_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    image, source, candidate = _candidate_archives(tmp_path)
    root, report, verdict, bundle, wheel, requirements = _candidate_evidence(tmp_path, candidate)
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\necho '[]'\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    handoff = _handoff_args(
        report=report,
        root=root,
        image=image,
        source=source,
        candidate=candidate,
        wheel=wheel,
        requirements=requirements,
        verdict=verdict,
    )
    assert app_certification_main(["validate-handoff", *handoff]) == 0
    unsigned_digest = json.loads(verdict.read_text())["report_sha256"]
    assert (
        app_certification_main(
            [
                "finalize-attestation",
                "--bundle",
                str(bundle),
                "--repository",
                "owner/reference-app",
                "--signer-repo",
                "rrrozhd/zeroth",
                "--signer-workflow",
                "rrrozhd/zeroth/.github/workflows/app-certification.yml",
                "--signer-digest",
                "d" * 40,
                *handoff,
            ]
        )
        == 0
    )
    assert json.loads(verdict.read_text())["report_sha256"] == file_digest(report)
    assert file_digest(report) != unsigned_digest
