from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
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
from release.app_certification.wheel_installation import (
    RUNTIME_BOOTSTRAP,
    TRUSTED_RUNTIME_IMAGE,
    _runtime_configuration,
)
from release.app_certification.workflow_finalizer import write_workflow_evidence
from tests.app_certification.test_hardening import _write_archive, identity
from tests.app_certification.workflow_fixtures import (
    successful_cleanup_document,
    successful_workflow_stages,
)


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_archive(tmp_path: Path) -> tuple[Path, Path, str]:
    repository = tmp_path / "app-repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    (repository / "app.py").write_bytes(b"committed source")
    _git(repository, "add", "app.py")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    app_commit = _git(repository, "rev-parse", "HEAD")
    source = tmp_path / "source.tar"
    _git(repository, "archive", "--format=tar", f"--output={source}", app_commit)
    return source, repository, app_commit


def _candidate_archives(
    tmp_path: Path,
) -> tuple[Path, Path, Path, CandidateIdentity]:
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
    source, repository, app_commit = _source_archive(tmp_path)
    candidate = identity().model_copy(
        update={
            "app_commit": app_commit,
            "image_digest": image_digest,
            "source_digest": file_digest(source),
        }
    )
    return image, source, repository, candidate


def _runtime_evidence(
    tmp_path: Path, candidate: CandidateIdentity
) -> tuple[Path, Path, Path, Path]:
    wheel = tmp_path / "zeroth-core.whl"
    requirements = tmp_path / "requirements-image.txt"
    installation = tmp_path / "installed-wheel.json"
    image_config = tmp_path / "image-config.json"
    wheel.write_bytes(b"trusted wheel")
    requirements.write_text("zeroth-core==0.23.9.9\n", encoding="utf-8")
    image_config.write_text(
        json.dumps(
            {
                "Cmd": [
                    "/usr/local/bin/python",
                    "-I",
                    "-S",
                    "-c",
                    RUNTIME_BOOTSTRAP,
                    "run-certified-runtime",
                    "/usr/local/lib/python3.12/site-packages",
                    "/opt/app",
                    "app",
                ],
                "Entrypoint": None,
                "Env": [],
                "Labels": {"dev.zeroth.certification.runtime-base": TRUSTED_RUNTIME_IMAGE},
            }
        ),
        encoding="utf-8",
    )
    installation.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "package": "zeroth-core",
                "version": candidate.zeroth_version,
                "wheel_sha256": file_digest(wheel),
                "installed_files": {"zeroth/__init__.py": "sha256:" + "a" * 64},
                "runtime": _runtime_configuration(image_config, candidate.image_digest),
            }
        ),
        encoding="utf-8",
    )
    return wheel, requirements, installation, image_config


def _candidate_evidence(
    tmp_path: Path, candidate: CandidateIdentity
) -> tuple[Path, Path, Path, Path, Path, Path, Path, Path]:
    root, report, verdict = tmp_path / "root", tmp_path / "report.json", tmp_path / "verdict.json"
    sbom, provenance = root / "evidence/app.spdx.json", root / "evidence/provenance.json"
    wheel, requirements, installation, image_config = _runtime_evidence(tmp_path, candidate)
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
            "wheel_installation": file_digest(installation),
            "image_config": file_digest(image_config),
        },
    )
    write_report(CertificationReport.passed(candidate, sbom, provenance, root=root), report)
    _write_workflow_inputs(tmp_path, report)
    bundle = _write_bundle(tmp_path, provenance)
    return root, report, verdict, bundle, wheel, requirements, installation, image_config


def _write_workflow_inputs(tmp_path: Path, report: Path) -> None:
    cleanup = tmp_path / "cleanup.json"
    stages = tmp_path / "workflow-stages.json"
    cleanup.write_text(
        json.dumps(successful_cleanup_document()) + "\n",
        encoding="utf-8",
    )
    stages.write_text(
        json.dumps(successful_workflow_stages()) + "\n",
        encoding="utf-8",
    )
    write_workflow_evidence(
        tmp_path / "workflow-evidence.json",
        cleanup=cleanup,
        report=report,
        workflow_stages=stages,
    )


def _write_bundle(tmp_path: Path, provenance: Path) -> Path:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            {"dsseEnvelope": {"payload": base64.b64encode(provenance.read_bytes()).decode()}}
        )
    )
    return bundle


def _handoff_args(
    *,
    report: Path,
    root: Path,
    image: Path,
    source: Path,
    app_repository: Path,
    candidate: CandidateIdentity,
    wheel: Path,
    requirements: Path,
    installation: Path,
    image_config: Path,
    verdict: Path,
) -> list[str]:
    return [
        "--report",
        str(report),
        "--root",
        str(root),
        "--workflow-evidence",
        str(report.parent / "workflow-evidence.json"),
        "--workflow-stages",
        str(report.parent / "workflow-stages.json"),
        "--cleanup",
        str(report.parent / "cleanup.json"),
        "--image-archive",
        str(image),
        "--source-archive",
        str(source),
        "--app-repository",
        str(app_repository),
        "--app-commit",
        candidate.app_commit,
        "--zeroth-version",
        candidate.zeroth_version,
        "--zeroth-commit",
        "d" * 40,
        "--certifier-wheel",
        str(wheel),
        "--requirements-lock",
        str(requirements),
        "--wheel-installation",
        str(installation),
        "--image-config",
        str(image_config),
        "--verdict",
        str(verdict),
    ]


def _attestation_args(bundle: Path, handoff: list[str]) -> list[str]:
    return [
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


def test_finalize_attestation_reissues_digest_bound_handoff_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    image, source, app_repository, candidate = _candidate_archives(tmp_path)
    (
        root,
        report,
        verdict,
        bundle,
        wheel,
        requirements,
        installation,
        image_config,
    ) = _candidate_evidence(tmp_path, candidate)
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\necho '[]'\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    handoff = _handoff_args(
        report=report,
        root=root,
        image=image,
        source=source,
        app_repository=app_repository,
        candidate=candidate,
        wheel=wheel,
        requirements=requirements,
        installation=installation,
        image_config=image_config,
        verdict=verdict,
    )
    assert app_certification_main(["validate-handoff", *handoff]) == 0
    unsigned_verdict = json.loads(verdict.read_text())
    unsigned_digest = unsigned_verdict["report_sha256"]
    assert unsigned_verdict["app_tree"] == _git(
        app_repository, "rev-parse", f"{candidate.app_commit}^{{tree}}"
    )
    assert app_certification_main(_attestation_args(bundle, handoff)) == 0
    assert json.loads(verdict.read_text())["report_sha256"] == file_digest(report)
    assert file_digest(report) != unsigned_digest
