from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from release.live_evaluation.evidence import UnsafeEvidenceError
from release.live_evaluation.native_safari_rightsizing_boundary_checkpoint import (
    EXACT_ERROR,
    EXPECTED_D012,
    build_checkpoint,
)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "README.md").write_text("fixture\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    return root


def _planes() -> dict[str, object]:
    return {
        "measured_endpoint": {
            "request_count": 7,
            "last_request_id_sha256": "a" * 64,
        },
        "provider": {
            "call_count": 12,
            "request_ids_sha256": "b" * 64,
        },
        "runs": {"count": 280, "ids_sha256": "c" * 64},
        "audits": {"count": 761, "head_digest": "d" * 64},
        "economics": {
            "cost_event_count": 35,
            "total_cost_usd": "0.01000000",
            "reservation_count": 35,
            "held_cost_usd": "0.00000000",
        },
    }


def _fixture(tmp_path: Path):
    repository = _repository(tmp_path)
    source = tmp_path / "staging"
    (source / "screenshots").mkdir(parents=True)
    (source / "accessibility").mkdir()
    (source / "runtime").mkdir()
    for name in (
        "01-configured-native-safari.png",
        "02-tolerance-101-error-native-safari.png",
    ):
        (source / "screenshots" / name).write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"safe-native-safari-capture" * 64
        )
    configured_ax = "\n".join(
        (
            'Window: "Zeroth Console", App: Safari.',
            "0 standard window Zeroth Console, ID: SafariWindow?IsSecure=false&UUID=fixture",
            "15 HTML content Description: Zeroth Console, URL: 127.0.0.1:3000/console/rightsizing/",
            "47 heading Rightsizing & Efficiency, Value: 1",
            "79 text field (settable) node_id The agent node whose audit history is harvested., Value: research, Placeholder: answer_node",
            "83 text field (settable) incumbent The model it runs today., Value: gpt-4o-mini, Placeholder: gpt-4o",
            "87 text entry area (settable) instruction The agent's system prompt — replayed verbatim during the experiment., Value: Answer only from the provided evidence., Placeholder: Answer the question using only the provided context.",
            "93 radio button vs. incumbent, Value: 1",
            "94 radio button vs. correct answer, Value: 0",
            "98 text field (settable) Tolerance (%) Optional difference allowed., Value: 101, Placeholder: 5",
            "102 text field (settable) Maximum cases Optional replay limit., Value: 20, Placeholder: 20",
            "105 checkbox Candidate needs tools, Value: 1",
            "108 checkbox Candidate needs vision, Value: 0",
            "113 text field (settable) Judge model Optional model used to compare candidate answers., Value: gpt-4o-mini, Placeholder: Provider default",
            "117 text field (settable) Maximum candidates Optional whole number from 1 through 6., Value: 6, Placeholder: 3",
            "121 text field (settable) Minimum cases Optional confirmation floor from 1 through 50., Value: 1, Placeholder: 5",
            "124 button Run experiment",
            "169 Safari",
        )
    )
    (source / "accessibility/01-configured-native-safari.txt").write_text(configured_ax)
    (source / "accessibility/02-tolerance-101-error-native-safari.txt").write_text(
        configured_ax + f"\n125 text {EXACT_ERROR}\n"
    )
    observation = {
        "schema_version": 1,
        "browser": {
            "name": "Safari",
            "version": "19.0",
            "engine": "WebKit",
            "platform": "macOS",
        },
        "route": "/console/rightsizing/",
        "configured_fields": {
            "node_id": "research",
            "incumbent": "gpt-4o-mini",
            "instruction": "Answer only from the provided evidence.",
            "mode": "equivalence",
            "tolerance_pct": "101",
            "max_cases": "20",
            "needs_tools": True,
            "needs_vision": False,
            "judge_model": "gpt-4o-mini",
            "max_candidates": "6",
            "min_cases": "1",
        },
        "submission": {
            "attempted": True,
            "blocked_by": "client_validation",
            "measured_endpoint_request_observed": False,
            "error": EXACT_ERROR,
            "invalid_control": "rightsizing.experiment.tolerance-pct",
            "aria_invalid": True,
        },
    }
    before = {
        "schema_version": 1,
        "tenant_id": "evaluation-studio-v1",
        "captured_at": "2026-08-26T12:00:00Z",
        "planes": _planes(),
    }
    after = deepcopy(before)
    after["captured_at"] = "2026-08-26T12:00:05Z"
    for name, value in (
        ("observation.json", observation),
        ("before.json", before),
        ("after.json", after),
        ("health.json", dict(EXPECTED_D012)),
    ):
        (source / "runtime" / name).write_text(json.dumps(value))
    return repository, source


def test_seals_exact_safari_client_boundary_with_zero_side_effect_delta(tmp_path: Path) -> None:
    repository, source = _fixture(tmp_path)
    destination = tmp_path / "accepted"

    result = build_checkpoint(
        source_root=source,
        destination=destination,
        repository_root=repository,
    )

    assert result == destination
    expected = {
        "manifest.json",
        "events.ndjson",
        "acceptance.json",
        "report.md",
        "SHA256SUMS",
        "runtime/observation.json",
        "runtime/before.json",
        "runtime/after.json",
        "runtime/health.json",
        "runtime/delta.json",
        "screenshots/01-configured-native-safari.png",
        "screenshots/02-tolerance-101-error-native-safari.png",
        "accessibility/01-configured-native-safari.txt",
        "accessibility/02-tolerance-101-error-native-safari.txt",
    }
    assert expected == {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert {row["status"] for row in acceptance["criteria"]} == {"pass"}
    assert json.loads((destination / "runtime/delta.json").read_text()) == {
        "audit_delta": 0,
        "cost_event_delta": 0,
        "measured_endpoint_request_delta": 0,
        "provider_call_delta": 0,
        "reservation_delta": 0,
        "run_delta": 0,
        "total_cost_usd_delta": "0.00000000",
    }
    checksums = (destination / "SHA256SUMS").read_text().splitlines()
    assert len(checksums) == len(expected) - 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("required_field", "configured fields"),
        ("error", "client validation"),
        ("safari", "Safari identity"),
        ("health", "D-012"),
        ("measured_request", "zero side-effect"),
        ("provider", "zero side-effect"),
        ("run", "zero side-effect"),
        ("audit", "zero side-effect"),
        ("cost", "zero side-effect"),
        ("reservation", "zero side-effect"),
    ),
)
def test_fails_closed_on_identity_validation_or_any_side_effect_delta(
    tmp_path: Path, mutation: str, message: str
) -> None:
    repository, source = _fixture(tmp_path)
    observation_path = source / "runtime/observation.json"
    observation = json.loads(observation_path.read_text())
    after_path = source / "runtime/after.json"
    after = json.loads(after_path.read_text())
    if mutation == "required_field":
        observation["configured_fields"]["node_id"] = ""
        observation_path.write_text(json.dumps(observation))
    elif mutation == "error":
        observation["submission"]["error"] = "wrong"
        observation_path.write_text(json.dumps(observation))
    elif mutation == "safari":
        observation["browser"]["name"] = "Chrome"
        observation_path.write_text(json.dumps(observation))
    elif mutation == "health":
        health_path = source / "runtime/health.json"
        health = json.loads(health_path.read_text())
        health["graph_version_ref"] = "wrong@1"
        health_path.write_text(json.dumps(health))
    else:
        plane, field = {
            "measured_request": ("measured_endpoint", "request_count"),
            "provider": ("provider", "call_count"),
            "run": ("runs", "count"),
            "audit": ("audits", "count"),
            "cost": ("economics", "cost_event_count"),
            "reservation": ("economics", "reservation_count"),
        }[mutation]
        after["planes"][plane][field] += 1
        after_path.write_text(json.dumps(after))

    destination = tmp_path / "accepted"
    with pytest.raises(RuntimeError, match=message):
        build_checkpoint(
            source_root=source,
            destination=destination,
            repository_root=repository,
        )
    assert not destination.exists()


def test_rejects_ax_drift_and_secret_shaped_staging_before_bundle_creation(
    tmp_path: Path,
) -> None:
    repository, source = _fixture(tmp_path)
    destination = tmp_path / "accepted"
    ax = source / "accessibility/02-tolerance-101-error-native-safari.txt"
    ax.write_text(ax.read_text().replace(EXACT_ERROR, "wrong"))
    with pytest.raises(RuntimeError, match="accessibility"):
        build_checkpoint(
            source_root=source,
            destination=destination,
            repository_root=repository,
        )
    assert not destination.exists()

    ax.write_text("Authorization: Bearer unsafe-secret-value-1234567890\n")
    with pytest.raises(UnsafeEvidenceError):
        build_checkpoint(
            source_root=source,
            destination=destination,
            repository_root=repository,
        )
    assert not destination.exists()


def test_rejects_raw_safari_ax_when_run_experiment_is_disabled(tmp_path: Path) -> None:
    repository, source = _fixture(tmp_path)
    for name in (
        "01-configured-native-safari.txt",
        "02-tolerance-101-error-native-safari.txt",
    ):
        path = source / "accessibility" / name
        path.write_text(
            path.read_text().replace(
                "124 button Run experiment", "124 button (disabled) Run experiment"
            )
        )

    with pytest.raises(RuntimeError, match="accessibility"):
        build_checkpoint(
            source_root=source,
            destination=tmp_path / "accepted",
            repository_root=repository,
        )
