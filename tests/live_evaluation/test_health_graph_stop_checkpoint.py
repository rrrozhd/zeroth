from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from release.live_evaluation.health_graph_stop_checkpoint import build_checkpoint


EXPECTED_HEALTH = {
    "workflow1": {
        "campaign_id": "evaluation-studio-v1",
        "deployment_ref": "evaluation-studio-v1-grounded-researcher-v1",
        "deployment_version": 5,
        "graph_version_ref": "evaluation-studio-v1-grounded-researcher@3",
        "status": "ok",
    },
    "workflow2": {
        "deployment_ref": "evaluation-studio-v1-batched-investigation-parent-v1",
        "deployment_version": 2,
        "graph_version_ref": "evaluation-studio-v1-batched-investigation-parent@2",
        "status": "ok",
    },
    "workflow3": {
        "campaign_id": "evaluation-studio-v1",
        "deployment_ref": "evaluation-studio-v1-governed-remediation-v2",
        "deployment_version": 3,
        "graph_version_ref": "evaluation-studio-v1-governed-remediation@4",
        "status": "ok",
    },
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _seal_source(root: Path) -> None:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "SHA256SUMS":
            continue
        rows.append(f"{sha256(path.read_bytes()).hexdigest()}  {relative}")
    (root / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _source_roots(tmp_path: Path) -> dict[str, Path]:
    roots = {name: tmp_path / name for name in EXPECTED_HEALTH}

    for workflow in ("workflow1", "workflow2"):
        event_id = f"{workflow}-health-event"
        root = roots[workflow]
        root.mkdir()
        _write_json(
            root / "results.json",
            {
                "criteria": [
                    {
                        "criterion_id": f"{workflow}.health-exact-graph-version",
                        "status": "pass",
                        "evidence": [f"events.ndjson#{event_id}"],
                    }
                ]
            },
        )
        (root / "events.ndjson").write_text(
            json.dumps(
                {
                    "event_id": event_id,
                    "type": "campaign.deployment.health_verified",
                    "data": EXPECTED_HEALTH[workflow],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _seal_source(root)

    workflow3 = roots["workflow3"]
    workflow3.mkdir()
    _write_json(
        workflow3 / "results.json",
        {
            "criteria": [
                {
                    "criterion_id": "workflow3.health-exact-graph-version",
                    "status": "pass",
                    "evidence": [
                        "console/health-v4.json",
                        "commands/0004-backend-docker-restart-v4.json",
                    ],
                }
            ]
        },
    )
    _write_json(workflow3 / "console/health-v4.json", EXPECTED_HEALTH["workflow3"])
    _write_json(
        workflow3 / "commands/0004-backend-docker-restart-v4.json",
        {
            "argv": [
                "docker",
                "compose",
                "-f",
                "compose.dev.yml",
                "up",
                "-d",
                "--force-recreate",
                "backend",
            ],
            "exit_code": 0,
            "name": "backend-docker-restart-v4",
        },
    )
    _seal_source(workflow3)
    return roots


def test_checkpoint_seals_one_derived_pass_only_after_all_health_sources_validate(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "checkpoint"
    build_checkpoint(destination=destination, source_roots=_source_roots(tmp_path))

    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert acceptance == {
        "criteria": [
            {
                "criterion_id": "stop.health-matches-graph",
                "evidence": [
                    "sources/workflow1.json",
                    "sources/workflow2.json",
                    "sources/workflow3.json",
                ],
                "note": None,
                "status": "pass",
            }
        ],
        "schema_version": 1,
    }
    assert (destination / "SHA256SUMS").is_file()


def test_checkpoint_rejects_a_tampered_source_before_creating_destination(
    tmp_path: Path,
) -> None:
    roots = _source_roots(tmp_path)
    with (roots["workflow2"] / "events.ndjson").open("a", encoding="utf-8") as handle:
        handle.write("{}\n")
    destination = tmp_path / "checkpoint"

    with pytest.raises(RuntimeError, match="checksum"):
        build_checkpoint(destination=destination, source_roots=roots)

    assert not destination.exists()


def test_checkpoint_rejects_a_nonexact_health_assertion_before_creating_destination(
    tmp_path: Path,
) -> None:
    roots = _source_roots(tmp_path)
    event_path = roots["workflow1"] / "events.ndjson"
    event = json.loads(event_path.read_text())
    event["data"]["graph_version_ref"] = "evaluation-studio-v1-grounded-researcher@4"
    event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    _seal_source(roots["workflow1"])
    destination = tmp_path / "checkpoint"

    with pytest.raises(RuntimeError, match="exact health"):
        build_checkpoint(destination=destination, source_roots=roots)

    assert not destination.exists()
