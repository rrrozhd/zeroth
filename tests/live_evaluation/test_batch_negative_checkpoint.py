from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from release.live_evaluation.batch_negative_checkpoint import build_checkpoint
from release.live_evaluation.provider_free_composed import ITEMS, ProviderFreeComposedFixture


PNG = b"\x89PNG\r\n\x1a\nfixture"
WEBM = b"\x1aE\xdf\xa3fixture"

CONTRACT_CRITERIA = {
    "batching.malformed-item",
    "workflow2.negative-empty-batch",
    "workflow2.negative-over-24-batch",
}
RUNTIME_CRITERIA = {"batching.active-refresh-restoration", "runs.cancel"}


def _fixture() -> ProviderFreeComposedFixture:
    return ProviderFreeComposedFixture(
        schema_version=1,
        fixture_id="batchnegative-test",
        child_workflow_id="child-workflow",
        child_graph_version_ref="child-workflow@1",
        child_deployment_ref="batchnegative-child",
        child_deployment_version=1,
        parent_workflow_id="parent-workflow",
        parent_graph_version_ref="parent-workflow@1",
        parent_deployment_ref="batchnegative-parent",
        parent_deployment_version=1,
        items=ITEMS,
    )


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return path


def _browser_root(root: Path, *, kind: str) -> Path:
    root.mkdir(parents=True)
    if kind == "contract":
        criteria = CONTRACT_CRITERIA
        summary_name = "batch-contract-rejection-summary.json"
        summary = {
            "schema_version": 1,
            "health": {
                "status": "ok",
                "deployment_ref": "published-batch-parent",
                "graph_version_ref": "published-batch-parent@3",
            },
            "observations": [
                {"id": "empty", "status": 422, "validation_type": "too_short"},
                {"id": "over-24", "status": 422, "validation_type": "too_long"},
                {"id": "malformed-item", "status": 422, "validation_type": "missing"},
            ],
            "run_count_before": 50,
            "run_count_after": 50,
            "run_identities_unchanged": True,
            "tenant_cost_unchanged": True,
            "expected_validation_console_errors": 3,
            "unexpected_console_errors": 0,
            "page_errors": 0,
            "provider_calls_performed": 0,
        }
        console = [
            {"type": "error", "url": "http://127.0.0.1:8122/v1/runs"}
            for _ in range(3)
        ]
        responses = [
            {
                "url": "http://127.0.0.1:8122/v1/runs",
                "status": 422,
                "resource_type": "fetch",
            }
            for _ in range(3)
        ]
    else:
        criteria = RUNTIME_CRITERIA
        summary_name = "batch-runtime-negative-summary.json"
        summary = {
            "schema_version": 1,
            "health": {
                "status": "ok",
                "deployment_ref": _fixture().parent_deployment_ref,
                "graph_version_ref": _fixture().parent_graph_version_ref,
            },
            "active_refresh": {
                "run_id": "active-parent",
                "terminal_status": "succeeded",
                "restored_while_active": True,
                "audit_count": 9,
            },
            "cancellation": {
                "run_id": "cancel-parent",
                "terminal_status": "failed",
                "failure_reason": "operator_cancelled",
                "child_count": 4,
                "child_statuses": ["failed"] * 4,
                "child_identities_stable_after_refresh": True,
                "audit_count": 1,
            },
            "provider_calls_performed": 0,
        }
        console = [{"type": "info", "url": None}]
        responses = [
            {
                "url": "http://127.0.0.1:8122/v1/runs/active-parent",
                "status": 200,
                "resource_type": "fetch",
            }
        ]

    indexed = root / "indexed"
    _write_json(indexed / summary_name, summary)
    _write_json(indexed / "sanitized-console.json", console)
    _write_json(indexed / "sanitized-network.json", {"requests": [], "responses": responses})
    _write_json(indexed / "response-identities.json", [])
    screenshot_names = (
        (
            "batch-empty-configured.png",
            "batch-empty-rejected.png",
            "batch-over-24-configured.png",
            "batch-over-24-rejected.png",
            "batch-malformed-item-configured.png",
            "batch-malformed-item-rejected.png",
            "batch-contract-rejections-refresh-restored.png",
        )
        if kind == "contract"
        else (
            "batch-active-before-refresh.png",
            "batch-active-refresh-restored.png",
            "batch-cancel-configured.png",
            "batch-cancelled-refresh-restored.png",
        )
    )
    for screenshot_name in screenshot_names:
        (indexed / screenshot_name).write_bytes(PNG)
    (indexed / "video.webm").write_bytes(WEBM)
    (root / "html-report/data").mkdir(parents=True)
    (root / "html-report/index.html").write_text("<html><body>report</body></html>")
    (root / "html-report/data/result.png").write_bytes(PNG)
    (root / "html-report/data/video.webm").write_bytes(WEBM)
    artifacts = [
        {"source": f"indexed/{summary_name}", "destination": f"console/{summary_name}"},
        {"source": "indexed/sanitized-console.json", "destination": "console/sanitized-console.json"},
        {"source": "indexed/sanitized-network.json", "destination": "network/sanitized-network.json"},
        {"source": "indexed/response-identities.json", "destination": "console/response-identities.json"},
        *(
            {
                "source": f"indexed/{screenshot_name}",
                "destination": f"screenshots/{screenshot_name}",
            }
            for screenshot_name in screenshot_names
        ),
        {"source": "indexed/video.webm", "destination": "videos/video.webm"},
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"},
    ]
    _write_json(
        root / "results.json",
        {
            "schema_version": 1,
            "completed": True,
            "criteria": [
                {
                    "criterion_id": criterion,
                    "status": "pass",
                    "test_id": f"test-{kind}",
                    "evidence": [item["destination"] for item in artifacts],
                }
                for criterion in sorted(criteria)
            ],
            "artifacts": artifacts,
        },
    )
    return root


def _fixture_manifest(path: Path) -> Path:
    value = {
        **asdict(_fixture()),
        "items": list(ITEMS),
        "sealed": False,
        "evidence_status": "staging",
    }
    return _write_json(path, value)


def _database(path: Path, *, populated: bool) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT NOT NULL,
            parent_run_id TEXT,
            thread_id TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            final_output TEXT,
            failure_state TEXT,
            metadata TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            lease_worker_id TEXT,
            lease_acquired_at TEXT,
            lease_expires_at TEXT
        );
        CREATE TABLE node_audits (
            audit_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            thread_id TEXT,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            record_json TEXT NOT NULL,
            cost_usd REAL,
            cost_event_id TEXT,
            chain_sequence INTEGER
        );
        """
    )
    if populated:
        fixture = _fixture()
        parents = (
            (
                "active-parent",
                "active-parent",
                "COMPLETED",
                json.dumps({"items": list(ITEMS)}),
                None,
            ),
            (
                "cancel-parent",
                "active-parent",
                "FAILED",
                None,
                json.dumps({"reason": "operator_cancelled", "message": "cancelled"}),
            ),
        )
        for run_id, thread_id, status, output, failure in parents:
            connection.execute(
                "INSERT INTO runs VALUES (?,NULL,?,?,?,?,?,?,?,'evaluation-studio-v1',NULL,NULL,NULL,NULL)",
                (
                    run_id,
                    thread_id,
                    fixture.parent_deployment_ref,
                    fixture.parent_graph_version_ref,
                    status,
                    output,
                    failure,
                    "{}",
                ),
            )
        for parent_id, count, status in (
            ("active-parent", 8, "COMPLETED"),
            ("cancel-parent", 4, "FAILED"),
        ):
            for index in range(count):
                child_id = f"{parent_id}-child-{index}"
                connection.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,'evaluation-studio-v1',NULL,NULL,NULL,NULL)",
                    (
                        child_id,
                        parent_id,
                        child_id,
                        fixture.child_deployment_ref,
                        fixture.child_graph_version_ref,
                        status,
                        json.dumps(ITEMS[index]) if status == "COMPLETED" else None,
                        None if status == "COMPLETED" else json.dumps({"reason": "operator_cancelled"}),
                        json.dumps(
                            {"total_cost_usd": 0.0, "total_estimated_cost_usd": 0.0}
                            if status == "COMPLETED"
                            else {}
                        ),
                    ),
                )
        run_rows = connection.execute(
            "SELECT run_id,thread_id,deployment_ref,graph_version_ref FROM runs"
        ).fetchall()
        for run_id, thread_id, deployment_ref, graph_version_ref in run_rows:
            count = 9 if run_id == "active-parent" else 1 if run_id == "cancel-parent" else 2
            for sequence in range(1, count + 1):
                record = {
                    "audit_id": f"{run_id}:audit:{sequence}",
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "tenant_id": "evaluation-studio-v1",
                    "workspace_id": None,
                    "deployment_ref": deployment_ref,
                    "graph_version_ref": graph_version_ref,
                    "record_digest": f"digest-{run_id}-{sequence}",
                    "previous_record_digest": (
                        None if sequence == 1 else f"digest-{run_id}-{sequence - 1}"
                    ),
                    "record_signature": f"signature-{run_id}-{sequence}",
                    "signing_key_id": "local-evaluation-key",
                    "signing_algorithm": "hmac-sha256",
                    "token_usage": None,
                    "cost_event_id": None,
                    "cost_usd": 0.0,
                    "estimated_cost_usd": 0.0,
                }
                connection.execute(
                    "INSERT INTO node_audits VALUES (?,?,?,?,?,?,?,NULL,?,?,NULL,?)",
                    (
                        record["audit_id"],
                        run_id,
                        "run.control.cancelled" if run_id == "cancel-parent" else "fixture.node",
                        thread_id,
                        graph_version_ref,
                        deployment_ref,
                        "evaluation-studio-v1",
                        json.dumps(record),
                        0.0,
                        sequence,
                    ),
                )
    connection.commit()
    connection.close()
    return path


def _build(tmp_path: Path) -> Path:
    return build_checkpoint(
        contract_root=_browser_root(tmp_path / "contract", kind="contract"),
        runtime_root=_browser_root(tmp_path / "runtime", kind="runtime"),
        pre_snapshot=_database(tmp_path / "pre.sqlite3", populated=False),
        post_snapshot=_database(tmp_path / "post.sqlite3", populated=True),
        fixture_manifest=_fixture_manifest(tmp_path / "fixture.json"),
        destination=tmp_path / "sealed",
        repository_root=Path(__file__).resolve().parents[2],
        tenant_id="evaluation-studio-v1",
    )


def test_build_checkpoint_seals_exact_negative_matrix_and_full_html_report(tmp_path: Path) -> None:
    root = _build(tmp_path)

    acceptance = json.loads((root / "acceptance.json").read_text())
    statuses = {row["criterion_id"]: row["status"] for row in acceptance["criteria"]}
    assert statuses == {
        **{criterion: "pass" for criterion in CONTRACT_CRITERIA | RUNTIME_CRITERIA},
        "batching.provider-economics": "blocked",
    }
    attestations = json.loads((root / "reconciliation/snapshot-attestations.json").read_text())
    assert attestations["pre"]["quick_check"] == "ok"
    assert attestations["post"]["quick_check"] == "ok"
    assert attestations["post"]["size_bytes"] > 0
    assert len(attestations["post"]["sha256"]) == 64
    assert attestations["raw_snapshots_in_sealed_bundle"] is False
    assert not list(root.rglob("*.sqlite3"))
    assert (root / "playwright-report/contract/data/result.png").is_file()
    assert (root / "playwright-report/runtime/data/video.webm").is_file()
    assert (root / "SHA256SUMS").is_file()
    for line in (root / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected


def test_checkpoint_rejects_missing_browser_criterion(tmp_path: Path) -> None:
    contract = _browser_root(tmp_path / "contract", kind="contract")
    results = json.loads((contract / "results.json").read_text())
    results["criteria"].pop()
    _write_json(contract / "results.json", results)

    with pytest.raises(RuntimeError, match="exact criteria"):
        build_checkpoint(
            contract_root=contract,
            runtime_root=_browser_root(tmp_path / "runtime", kind="runtime"),
            pre_snapshot=_database(tmp_path / "pre.sqlite3", populated=False),
            post_snapshot=_database(tmp_path / "post.sqlite3", populated=True),
            fixture_manifest=_fixture_manifest(tmp_path / "fixture.json"),
            destination=tmp_path / "sealed",
            repository_root=Path(__file__).resolve().parents[2],
            tenant_id="evaluation-studio-v1",
        )


def test_checkpoint_rejects_unsigned_involved_audit(tmp_path: Path) -> None:
    post = _database(tmp_path / "post.sqlite3", populated=True)
    with sqlite3.connect(post) as connection:
        record = json.loads(
            connection.execute(
                "SELECT record_json FROM node_audits WHERE audit_id='cancel-parent:audit:1'"
            ).fetchone()[0]
        )
        record["record_signature"] = None
        connection.execute(
            "UPDATE node_audits SET record_json=? WHERE audit_id='cancel-parent:audit:1'",
            (json.dumps(record),),
        )

    with pytest.raises(RuntimeError, match="signed"):
        build_checkpoint(
            contract_root=_browser_root(tmp_path / "contract", kind="contract"),
            runtime_root=_browser_root(tmp_path / "runtime", kind="runtime"),
            pre_snapshot=_database(tmp_path / "pre.sqlite3", populated=False),
            post_snapshot=post,
            fixture_manifest=_fixture_manifest(tmp_path / "fixture.json"),
            destination=tmp_path / "sealed",
            repository_root=Path(__file__).resolve().parents[2],
            tenant_id="evaluation-studio-v1",
        )


def test_checkpoint_rejects_contract_side_effect_claim(tmp_path: Path) -> None:
    contract = _browser_root(tmp_path / "contract", kind="contract")
    summary = contract / "indexed/batch-contract-rejection-summary.json"
    value = json.loads(summary.read_text())
    value["tenant_cost_unchanged"] = False
    _write_json(summary, value)

    with pytest.raises(RuntimeError, match="side effects"):
        build_checkpoint(
            contract_root=contract,
            runtime_root=_browser_root(tmp_path / "runtime", kind="runtime"),
            pre_snapshot=_database(tmp_path / "pre.sqlite3", populated=False),
            post_snapshot=_database(tmp_path / "post.sqlite3", populated=True),
            fixture_manifest=_fixture_manifest(tmp_path / "fixture.json"),
            destination=tmp_path / "sealed",
            repository_root=Path(__file__).resolve().parents[2],
            tenant_id="evaluation-studio-v1",
        )
