from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore


def _module():
    return importlib.import_module("release.live_evaluation.product_surface_inventory_checkpoint")


def _request(path: str, *, method: str = "GET") -> object:
    from tests.live_evaluation.test_native_safari_retention_checkpoint import (
        _request as runtime_request,
    )

    return runtime_request(path, method=method)


def _source(
    tmp_path: Path,
    *,
    uncataloged: bool = False,
    omit_select_exercise: bool = False,
    omit_checkbox_exercise: bool = False,
    omit_radio_exercise: bool = False,
) -> Path:
    module = _module()
    root = tmp_path / "source"
    indexed = root / "indexed"
    report = root / "html-report"
    indexed.mkdir(parents=True)
    report.mkdir(parents=True)
    artifacts: list[dict[str, str]] = []
    criteria: list[dict[str, object]] = []
    for index, route in enumerate(module.ROUTES):
        slug = route.replace("/", "-") or "overview"
        console = f"inventory-{index}.json"
        screenshot = f"route-{index}.png"
        video = f"route-{index}.webm"
        controls: list[dict[str, object]] = [
            {
                "evidence_id": f"{slug}.button.test",
                "capability_ids": []
                if uncataloged and index == 0
                else ["shell-navigation"],
                "tag": "button",
                "type": None,
                "disabled": False,
                "options": [],
            }
        ]
        select_exercises: list[dict[str, object]] = []
        checkbox_exercises: list[dict[str, object]] = []
        radio_exercises: list[dict[str, object]] = []
        if index == 0:
            controls.append(
                {
                    "evidence_id": "overview.select.environment",
                    "capability_ids": ["connection-and-identity"],
                    "tag": "select",
                    "type": None,
                    "disabled": False,
                    "options": [
                        {"value": "local", "disabled": False},
                        {"value": "staging", "disabled": False},
                        {"value": "retired", "disabled": True},
                    ],
                }
            )
            if not omit_select_exercise:
                select_exercises.append(
                    {
                        "evidence_id": "overview.select.environment",
                        "values": ["local", "staging"],
                    }
                )
        if index == 1:
            controls.append(
                {
                    "evidence_id": "approvals.checkbox.test",
                    "capability_ids": ["approvals"],
                    "tag": "input",
                    "type": "checkbox",
                    "disabled": False,
                    "options": [],
                }
            )
            if not omit_checkbox_exercise:
                checkbox_exercises.append(
                    {
                        "evidence_id": "approvals.checkbox.test",
                        "states": [False, True],
                        "appeared": [],
                        "disappeared": [],
                    }
                )
        if index == 2:
            controls.append(
                {
                    "evidence_id": "artifacts.radio.preview-json",
                    "capability_ids": ["artifacts"],
                    "tag": "input",
                    "type": "radio",
                    "role": None,
                    "name": "artifact-preview",
                    "value": "json",
                    "disabled": False,
                    "options": [],
                }
            )
            if not omit_radio_exercise:
                radio_exercises.append(
                    {
                        "evidence_id": "artifacts.radio.preview-json",
                        "name": "artifact-preview",
                        "value": "json",
                    }
                )
        (indexed / console).write_text(
            json.dumps(
                {
                    "controls": controls,
                    "identity_errors": [],
                    "exercised_select_options": select_exercises,
                    "exercised_checkbox_states": checkbox_exercises,
                    "exercised_radio_options": radio_exercises,
                }
            ),
            encoding="utf-8",
        )
        (indexed / screenshot).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
        (indexed / video).write_bytes(b"\x1aE\xdf\xa3safe")
        destinations = [
            f"console/{console}",
            f"screenshots/{screenshot}",
            f"videos/{video}",
        ]
        artifacts.extend(
            {
                "source": f"indexed/{name}",
                "destination": destination,
            }
            for name, destination in zip((console, screenshot, video), destinations, strict=True)
        )
        criteria.append(
            {
                "criterion_id": module.criterion_for_route(route),
                "status": "pass",
                "test_id": f"test-{index}",
                "evidence": destinations,
            }
        )
    (report / "index.html").write_text("<html>safe report</html>", encoding="utf-8")
    artifacts.append(
        {
            "source": "html-report/index.html",
            "destination": "playwright-report/index.html",
        }
    )
    (root / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": True,
                "criteria": criteria,
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_checkpoint_seals_every_published_route_inventory(tmp_path: Path) -> None:
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
    assert len(acceptance["criteria"]) == 21
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["route_count"] == 21
    assert manifest["screenshot_count"] == 21
    assert manifest["control_inventory_count"] == 21
    assert manifest["provider_calls_performed"] == 0


def test_checkpoint_rejects_uncataloged_control(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="uncataloged control"):
        module.build_checkpoint(
            source_root=_source(tmp_path, uncataloged=True),
            destination=tmp_path / "bad",
            request=_request,
        )


def test_checkpoint_rejects_missing_route_screenshot(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    (source / "indexed/route-0.png").unlink()

    with pytest.raises(RuntimeError, match="missing source artifact"):
        module.build_checkpoint(
            source_root=source,
            destination=tmp_path / "bad",
            request=_request,
        )


def test_checkpoint_rejects_unexercised_select_options(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="select options were not exercised"):
        module.build_checkpoint(
            source_root=_source(tmp_path, omit_select_exercise=True),
            destination=tmp_path / "bad",
            request=_request,
        )


def test_checkpoint_rejects_unexercised_checkbox_states(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="checkbox states were not exercised"):
        module.build_checkpoint(
            source_root=_source(tmp_path, omit_checkbox_exercise=True),
            destination=tmp_path / "bad",
            request=_request,
        )


def test_checkpoint_rejects_unexercised_radio_option(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="radio options were not exercised"):
        module.build_checkpoint(
            source_root=_source(tmp_path, omit_radio_exercise=True),
            destination=tmp_path / "bad",
            request=_request,
        )
