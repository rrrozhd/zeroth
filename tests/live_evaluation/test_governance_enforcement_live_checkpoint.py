from __future__ import annotations

import importlib
import json
from collections import Counter

import pytest


def _module():
    return importlib.import_module("release.live_evaluation.governance_enforcement_live_checkpoint")


def _runtime_rows() -> list[dict[str, object]]:
    module = _module()
    rows: list[dict[str, object]] = []
    for case in module.CASES:
        own_status = 200 if case.can_read else 403
        cross_status = 404 if case.can_read else 403
        rows.append(
            {
                "tenant_id": case.tenant_id,
                "service": case.service,
                "role": case.role,
                "identity_status": 200,
                "identity_tenant_id": case.tenant_id,
                "identity_roles": [case.role],
                "capabilities_status": own_status,
                "capability_rows": case.capability_rows if case.can_read else None,
                "enforcement_status": own_status,
                "enforcement_rows": case.enforcement_rows if case.can_read else None,
                "cross_identity_status": 404,
                "cross_capabilities_status": cross_status,
                "cross_enforcement_status": cross_status,
            }
        )
    return rows


def _authorization_rows() -> list[dict[str, object]]:
    module = _module()
    rows: list[dict[str, object]] = []
    for _project in module.PROJECTS:
        for case in module.CASES:
            protected = (
                [
                    "/v1/econ/regulus/registry/capabilities",
                    "/v1/econ/regulus/registry/capabilities",
                    "/v1/econ/regulus/enforcement/actions",
                    "/v1/econ/regulus/enforcement/actions",
                ]
                if case.can_read
                else []
            )
            rows.append(
                {
                    "tenant_id": case.tenant_id,
                    "role": case.role,
                    "capabilities_read_allowed": case.can_read,
                    "enforcement_decision_allowed": case.can_mutate,
                    "actual_capability_rows": case.capability_rows,
                    "actual_enforcement_rows": case.enforcement_rows,
                    "protected_reads_issued": protected,
                }
            )
    return rows


def test_runtime_matrix_requires_exact_identity_scope_and_cross_tenant_concealment() -> None:
    module = _module()
    rows = _runtime_rows()

    module.validate_runtime_rows(rows)

    rows[0]["cross_identity_status"] = 403
    with pytest.raises(RuntimeError, match="runtime matrix"):
        module.validate_runtime_rows(rows)


def test_authorization_matrix_requires_all_seven_cases_in_both_projects() -> None:
    module = _module()
    rows = _authorization_rows()

    module.validate_authorization_rows(rows)

    with pytest.raises(RuntimeError, match="authorization matrix"):
        module.validate_authorization_rows(rows[:-1])


def test_artifact_inventory_requires_every_checkpoint_category() -> None:
    module = _module()

    module.validate_artifact_counts(Counter(module.TOP_LEVEL_COUNTS))

    incomplete = Counter(module.TOP_LEVEL_COUNTS)
    incomplete["screenshots"] -= 1
    with pytest.raises(RuntimeError, match="artifact inventory"):
        module.validate_artifact_counts(incomplete)


def test_playwright_summary_requires_fourteen_clean_tests_across_both_engines() -> None:
    module = _module()
    tests = []
    for project in module.PROJECTS:
        for case in module.CASES:
            tests.append(
                {
                    "projectName": project,
                    "title": (f"live {case.tenant_id} {case.role} governance surfaces"),
                    "ok": True,
                    "outcome": "expected",
                    "testId": f"{project}-{case.tenant_id}-{case.role}",
                }
            )
    summary = {
        "projectNames": list(module.PROJECTS),
        "errors": [],
        "stats": {
            "total": 14,
            "expected": 14,
            "unexpected": 0,
            "flaky": 0,
            "skipped": 0,
            "ok": True,
        },
        "files": [
            {
                "fileName": module.TEST_FILE,
                "stats": {
                    "total": 14,
                    "expected": 14,
                    "unexpected": 0,
                    "flaky": 0,
                    "skipped": 0,
                    "ok": True,
                },
                "tests": tests,
            }
        ],
    }

    module.validate_playwright_summary(summary)

    summary["stats"]["unexpected"] = 1
    with pytest.raises(RuntimeError, match="Playwright summary"):
        module.validate_playwright_summary(summary)


def test_update_evidence_index_promotes_only_exact_blocked_governance_entry() -> None:
    module = _module()
    index = {
        "entries": [
            {
                "capability_id": "governance-and-enforcement",
                "source_root": "regulus-enforcement-ui-20260824-2",
                "supplemental_source_roots": ["identity-isolation-live-checkpoint-20260825-1"],
                "status": "blocked",
                "passed_checkpoints": ["configured_capability_and_enforcement_action"],
                "remaining_checkpoints": [
                    "operator_reviewer_admin_platform_admin_enforcement_denial_matrix",
                    "cross_tenant_enforcement_isolation",
                ],
                "evidence_criteria": ["economics-and-rightsizing.enforcement-approval"],
            }
        ]
    }

    updated = module.updated_evidence_index(index, source_root="sealed-root")

    entry = updated["entries"][0]
    assert entry["source_root"] == "sealed-root"
    assert entry["status"] == "pass"
    assert entry["remaining_checkpoints"] == []
    assert entry["passed_checkpoints"][-2:] == [
        "operator_reviewer_admin_platform_admin_enforcement_denial_matrix",
        "cross_tenant_enforcement_isolation",
    ]
    assert json.dumps(index) != json.dumps(updated)

    entry["status"] = "blocked"
    with pytest.raises(RuntimeError, match="governance evidence index"):
        module.updated_evidence_index(updated, source_root="another-root")
