from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

from release.app_certification import (
    CertificationReport,
    bind_sbom,
    file_digest,
    write_provenance,
    write_report,
)
from release.app_certification.cli import main as app_certification_main
from tests.app_certification.test_hardening import COMMIT, _write_archive, identity


def test_finalize_attestation_reissues_digest_bound_handoff_verdict(tmp_path: Path) -> None:
    layer = b"candidate-layer"
    layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    config = json.dumps({"rootfs": {"diff_ids": [layer_digest]}}).encode()
    image_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    image = tmp_path / "image.tar"
    _write_archive(
        image,
        {
            "manifest.json": json.dumps(
                [{"Config": "config.json", "Layers": ["layer.tar"]}]
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
    root, report, verdict = tmp_path / "root", tmp_path / "report.json", tmp_path / "verdict.json"
    sbom, provenance = root / "evidence/app.spdx.json", root / "evidence/provenance.json"
    sbom.parent.mkdir(parents=True)
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    bind_sbom(sbom, candidate)
    write_provenance(provenance, candidate)
    write_report(CertificationReport.passed(candidate, sbom, provenance, root=root), report)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps({"dsseEnvelope": {"payload": base64.b64encode(provenance.read_bytes()).decode()}})
    )
    handoff = [
        "--report", str(report), "--root", str(root), "--image-archive", str(image),
        "--source-archive", str(source), "--app-commit", COMMIT, "--zeroth-version",
        candidate.zeroth_version, "--zeroth-commit", "d" * 40, "--verdict", str(verdict),
    ]
    assert app_certification_main(["validate-handoff", *handoff]) == 0
    unsigned_digest = json.loads(verdict.read_text())["report_sha256"]
    assert app_certification_main(["finalize-attestation", "--bundle", str(bundle), *handoff]) == 0
    assert json.loads(verdict.read_text())["report_sha256"] == file_digest(report)
    assert file_digest(report) != unsigned_digest
