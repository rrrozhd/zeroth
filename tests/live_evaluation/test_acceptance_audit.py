from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.acceptance_audit import (
    AcceptanceAuditEntry,
    AcceptanceAuditResult,
    AcceptanceSourceMap,
    audit_acceptance,
    write_gap_audit,
)
from release.live_evaluation.evidence import AcceptanceCriterion


def _write_results(root: Path, *, completed: bool, criterion_id: str, status: str) -> None:
    root.mkdir(parents=True)
    (root / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": completed,
                "criteria": [
                    {
                        "criterion_id": criterion_id,
                        "status": status,
                        "evidence": ["screenshots/checkpoint.png"],
                    }
                ],
            }
        )
    )
    (root / "screenshots").mkdir()
    (root / "screenshots/checkpoint.png").write_bytes(
        b"\x89PNG\r\n\x1a\nsafe screenshot fixture"
    )


def test_audit_requires_explicit_completed_passing_source_mapping(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    _write_results(
        evidence_root / "studio-authoring",
        completed=True,
        criterion_id="ui.node-placement",
        status="pass",
    )
    catalog = (
        AcceptanceCriterion("ui.node-placement", "not_run"),
        AcceptanceCriterion("control.provider-credential-valid", "not_run"),
        AcceptanceCriterion("workflow2.happy-1", "not_run"),
    )
    source_map = AcceptanceSourceMap.model_validate(
        {
            "schema_version": 1,
            "sources": {
                "studio": {
                    "root": "studio-authoring",
                    "record": "results.json",
                }
            },
            "mappings": [
                {
                    "criterion_id": "ui.node-placement",
                    "source": "studio",
                    "source_criterion_id": "ui.node-placement",
                }
            ],
            "blocked": {
                "control.provider-credential-valid": "rotated external credential required"
            },
        }
    )

    result = audit_acceptance(catalog, evidence_root=evidence_root, source_map=source_map)
    by_id = {item.criterion_id: item for item in result.criteria}

    assert result.counts == {"pass": 1, "fail": 0, "blocked": 1, "not_run": 1}
    assert by_id["ui.node-placement"].status == "pass"
    assert by_id["ui.node-placement"].source_root == "studio-authoring"
    assert by_id["control.provider-credential-valid"].status == "blocked"
    assert by_id["workflow2.happy-1"].status == "not_run"


@pytest.mark.parametrize("completed,status", [(False, "pass"), (True, "fail")])
def test_audit_fails_closed_on_incomplete_or_nonpassing_source(
    tmp_path: Path,
    completed: bool,
    status: str,
) -> None:
    evidence_root = tmp_path / "evidence"
    _write_results(
        evidence_root / "source",
        completed=completed,
        criterion_id="source.assertion",
        status=status,
    )
    source_map = AcceptanceSourceMap.model_validate(
        {
            "schema_version": 1,
            "sources": {"source": {"root": "source", "record": "results.json"}},
            "mappings": [
                {
                    "criterion_id": "target.criterion",
                    "source": "source",
                    "source_criterion_id": "source.assertion",
                }
            ],
        }
    )

    result = audit_acceptance(
        (AcceptanceCriterion("target.criterion", "not_run"),),
        evidence_root=evidence_root,
        source_map=source_map,
    )

    assert result.criteria[0].status == "fail"
    assert "source assertion is not an accepted pass" in result.criteria[0].note


def test_source_map_rejects_duplicate_or_unknown_targets(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    _write_results(
        evidence_root / "source",
        completed=True,
        criterion_id="source.assertion",
        status="pass",
    )
    source_map = AcceptanceSourceMap.model_validate(
        {
            "schema_version": 1,
            "sources": {"source": {"root": "source", "record": "results.json"}},
            "mappings": [
                {
                    "criterion_id": "missing.target",
                    "source": "source",
                    "source_criterion_id": "source.assertion",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="unknown criterion"):
        audit_acceptance(
            (AcceptanceCriterion("known.target", "not_run"),),
            evidence_root=evidence_root,
            source_map=source_map,
        )


def test_mapping_can_require_multiple_assertions_and_sealed_files(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    source_root = evidence_root / "sealed-source"
    source_root.mkdir(parents=True)
    (source_root / "manifest.json").write_text('{"campaign_id":"safe"}\n')
    (source_root / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": True,
                "criteria": [
                    {"criterion_id": "cap.tenant", "status": "pass"},
                    {"criterion_id": "cap.run", "status": "pass"},
                ],
            }
        )
    )
    source_map = AcceptanceSourceMap.model_validate(
        {
            "schema_version": 1,
            "sources": {
                "sealed": {"root": "sealed-source", "record": "results.json"}
            },
            "mappings": [
                {
                    "criterion_id": "economics.campaign-and-run-caps",
                    "source": "sealed",
                    "source_criterion_ids": ["cap.tenant", "cap.run"],
                    "files": ["manifest.json"],
                }
            ],
        }
    )

    result = audit_acceptance(
        (AcceptanceCriterion("economics.campaign-and-run-caps", "not_run"),),
        evidence_root=evidence_root,
        source_map=source_map,
    )

    assert result.criteria[0].status == "pass"
    assert result.criteria[0].source_criterion_ids == ("cap.tenant", "cap.run")
    assert result.criteria[0].files == ("manifest.json",)


def test_gap_audit_writer_is_durable_and_never_claims_final_acceptance(
    tmp_path: Path,
) -> None:
    result = AcceptanceAuditResult(
        (
            AcceptanceAuditEntry(
                "ui.node-placement",
                "pass",
                source_root="studio-authoring",
                source_criterion_ids=("ui.node-placement",),
                files=("screenshots/checkpoint.png",),
            ),
            AcceptanceAuditEntry(
                "workflow1.happy-1",
                "blocked",
                note="rotated external credential required",
            ),
            AcceptanceAuditEntry(
                "workflow2.happy-1",
                "not_run",
                note="no explicit accepted-source mapping",
            ),
        )
    )

    json_path, report_path = write_gap_audit(result, output_root=tmp_path / "gap")

    document = json.loads(json_path.read_text())
    assert document["kind"] == "interim_acceptance_gap_audit"
    assert document["counts"] == {
        "pass": 1,
        "fail": 0,
        "blocked": 1,
        "not_run": 1,
    }
    assert document["criteria"][0]["source_criterion_ids"] == ["ui.node-placement"]
    assert "not final campaign acceptance" in report_path.read_text().lower()
    assert not (tmp_path / "gap" / "acceptance.json").exists()
