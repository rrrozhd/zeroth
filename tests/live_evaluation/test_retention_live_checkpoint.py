from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore


def _module():
    return importlib.import_module("release.live_evaluation.retention_live_checkpoint")


def _request(path: str, *, method: str = "GET") -> object:
    from tests.live_evaluation.test_native_safari_retention_checkpoint import (
        _request as runtime_request,
    )

    return runtime_request(path, method=method)


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    indexed = root / "indexed"
    report = root / "html-report"
    indexed.mkdir(parents=True)
    report.mkdir(parents=True)

    policy_result = "policy-result.json"
    hold_result = "hold-result.json"
    (indexed / policy_result).write_text(
        json.dumps(
            {
                "zero_rejected": True,
                "non_numeric_rejected": True,
                "minimum_days_persisted": 1,
                "representative_large_days_persisted": 36500,
                "disabled_state_persisted": True,
                "original_policy_restored": True,
            }
        ),
        encoding="utf-8",
    )
    (indexed / hold_result).write_text(
        json.dumps(
            {
                "run_scoped_hold_persisted": True,
                "tenant_wide_hold_persisted": True,
                "both_released": True,
                "baseline_hold_ids_preserved": ["8d452480319d4578895007cc8a36c8f0"],
            }
        ),
        encoding="utf-8",
    )
    screenshots = [f"shot-{index}.png" for index in range(7)]
    videos = [f"video-{index}.webm" for index in range(2)]
    for name in screenshots:
        (indexed / name).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
    for name in videos:
        (indexed / name).write_bytes(b"\x1aE\xdf\xa3safe")
    (report / "index.html").write_text("<html>safe report</html>", encoding="utf-8")

    artifacts = [
        {"source": f"indexed/{policy_result}", "destination": f"console/{policy_result}"},
        {"source": f"indexed/{hold_result}", "destination": f"console/{hold_result}"},
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"},
        *[
            {"source": f"indexed/{name}", "destination": f"screenshots/{name}"}
            for name in screenshots
        ],
        *[{"source": f"indexed/{name}", "destination": f"videos/{name}"} for name in videos],
    ]
    destinations = [item["destination"] for item in artifacts]
    criteria = [
        "fields.legal-hold",
        "fields.retention-policy",
        "retention-and-erasure.boundary",
        "retention-and-erasure.held",
        "retention-and-erasure.persistence",
    ]
    (root / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": True,
                "criteria": [
                    {
                        "criterion_id": criterion,
                        "status": "pass",
                        "test_id": f"test-{index}",
                        "evidence": destinations,
                    }
                    for index, criterion in enumerate(criteria)
                ],
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_checkpoint_seals_reversible_retention_live_results(tmp_path: Path) -> None:
    module = _module()
    destination = tmp_path / "sealed"

    result = module.build_checkpoint(
        source_root=_source(tmp_path),
        destination=destination,
        request=_request,
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(module.ACCEPTED_CRITERIA)
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["provider_calls_performed"] == 0
    assert manifest["mutations_restored"] is True
    assert manifest["screenshot_count"] == 7
    assert (destination / "runtime/retention-policy-after.json").is_file()
    assert (destination / "commands/0001-retention-live-playwright.json").is_file()


def test_checkpoint_rejects_missing_declared_artifact(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    (source / "indexed/shot-0.png").unlink()

    with pytest.raises(RuntimeError, match="missing source artifact"):
        module.build_checkpoint(
            source_root=source,
            destination=tmp_path / "bad",
            request=_request,
        )


def test_checkpoint_rejects_unrestored_policy(tmp_path: Path) -> None:
    module = _module()

    def unrestored(path: str, *, method: str = "GET") -> object:
        value = _request(path, method=method)
        if path == "/v1/retention/policy":
            return {**value, "enabled": False}  # type: ignore[arg-type]
        return value

    with pytest.raises(RuntimeError, match="restored no-expiry state"):
        module.build_checkpoint(
            source_root=_source(tmp_path),
            destination=tmp_path / "bad",
            request=unrestored,
        )
