from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
import release.live_evaluation.provider_free_composed as provider_free_composed

from release.live_evaluation.provider_free_composed import (
    BoundedComposedUiRunner,
    DockerSqliteSnapshotter,
    HttpFixtureClient,
    ITEMS,
    ProviderFreeComposedFixture,
    provision_fixture,
    provision_partial_failure_fixture,
    read_fixture_manifest,
    validate_persistent_snapshot,
    validate_ui_summary,
    write_unsealed_fixture_manifest,
)


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> object:
        return self._payload


def _fixture() -> ProviderFreeComposedFixture:
    return ProviderFreeComposedFixture(
        schema_version=1,
        fixture_id="slice-001",
        child_workflow_id="child-workflow",
        child_graph_version_ref="child-workflow@1",
        child_deployment_ref="provider-free-composed-slice-001-child",
        child_deployment_version=1,
        parent_workflow_id="parent-workflow",
        parent_graph_version_ref="parent-workflow@1",
        parent_deployment_ref="provider-free-composed-slice-001-parent",
        parent_deployment_version=1,
        items=ITEMS,
    )


def _summary() -> dict[str, object]:
    repetitions = []
    for repetition in range(1, 4):
        repetitions.append(
            {
                "repetition": repetition,
                "parent_run_id": f"parent-{repetition}",
                "parent_thread_id": f"parent-thread-{repetition}",
                "terminal_output": {"items": list(ITEMS)},
                "children": [
                    {
                        "run_id": f"child-{repetition}-{index}",
                        "thread_id": f"child-thread-{repetition}-{index}",
                        "parent_run_id": f"parent-{repetition}",
                    }
                    for index in range(8)
                ],
                "economics": {"priced_call_count": 0, "total_cost_usd": 0},
            }
        )
    return {
        "schema_version": 1,
        "health": {
            "deployment_ref": "provider-free-composed-slice-001-parent",
            "graph_version_ref": "parent-workflow@1",
        },
        "repetitions": repetitions,
        "restored_run_id": "parent-3",
        "provider_economics_status": "blocked",
    }


def test_provision_fixture_uses_studio_publish_and_deployment_apis() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    workflow_ids = iter(("child-workflow", "parent-workflow"))

    def request(method: str, path: str, payload: dict[str, Any] | None = None) -> _Response:
        calls.append((method, path, payload))
        if method == "POST" and path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(
                201, {"name": payload["name"], "version": 1, "json_schema": payload["json_schema"]}
            )
        if method == "POST" and path == "/api/studio/v1/workflows":
            workflow_id = next(workflow_ids)
            return _Response(201, {"id": workflow_id, "version": 1, "status": "draft"})
        if method == "PUT" and path.startswith("/api/studio/v1/workflows/"):
            assert payload is not None
            return _Response(
                200, {"id": path.rsplit("/", 1)[-1], "version": 1, "status": "draft", **payload}
            )
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            workflow_id = path.split("/")[-2]
            return _Response(200, {"id": workflow_id, "version": 1, "status": "published"})
        if method == "POST" and path == "/v1/deployments":
            assert payload is not None
            return _Response(
                201,
                {
                    "deployment_ref": payload["deployment_ref"],
                    "version": 1,
                    "graph_version_ref": f"{payload['graph_id']}@1",
                    "status": "active",
                    "serving": False,
                    "created_at": "2026-08-25T00:00:00Z",
                },
            )
        raise AssertionError((method, path, payload))

    fixture = provision_fixture(request=request, fixture_id="slice-001")

    assert fixture.child_workflow_id == "child-workflow"
    assert fixture.parent_workflow_id == "parent-workflow"
    assert fixture.provider_calls_performed == 0
    assert fixture.provider_economics_status == "blocked"
    assert fixture.restart_required is True
    assert fixture.items == ITEMS
    put_payloads = [payload for method, _, payload in calls if method == "PUT"]
    child_nodes = put_payloads[0]["nodes"]
    parent_nodes = put_payloads[1]["nodes"]
    assert [node["type"] for node in child_nodes] == ["entrypoint", "code"]
    # Runtime step limits are checked at the loop head before terminalization,
    # so a two-node child needs one completion cycle beyond its two dispatches.
    assert put_payloads[0]["execution_settings"]["max_total_steps"] == 3
    delay_source = child_nodes[1]["data"]["config"]["inline_source"]
    assert "time.sleep(3.0)" in delay_source
    assert "json.dump(payload, sys.stdout" in delay_source
    assert child_nodes[1]["data"]["config"]["timeout_seconds"] == 10
    assert [node["type"] for node in parent_nodes] == ["entrypoint", "subgraph"]
    assert parent_nodes[0]["data"]["parallel_config"] == {
        "split_path": "items",
        "merge_strategy": "collect",
        "fail_mode": "fail_fast",
        "max_branches": 8,
        "max_concurrency": 4,
        "batch_size": 8,
        "branch_timeout_seconds": 30,
    }
    assert parent_nodes[1]["data"]["config"] == {
        "graph_ref": fixture.child_deployment_ref,
        "version": 1,
        "thread_participation": "isolated",
        "max_depth": 1,
    }
    assert all(
        node["type"] not in {"agent", "retrieval"}
        for payload in put_payloads
        for node in payload["nodes"]
    )
    child_deploy_index = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "POST"
        and call[1] == "/v1/deployments"
        and call[2]["graph_id"] == "child-workflow"
    )
    parent_save_index = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ("PUT", "/api/studio/v1/workflows/parent-workflow")
    )
    assert child_deploy_index < parent_save_index
    assert asdict(fixture)["parent_graph_version_ref"] == "parent-workflow@1"


def test_partial_failure_fixture_is_provider_free_best_effort_with_one_controlled_child_failure() -> (
    None
):
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    workflow_ids = iter(("failure-child", "failure-parent"))

    def request(method: str, path: str, payload: dict[str, Any] | None = None) -> _Response:
        calls.append((method, path, payload))
        if method == "POST" and path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": next(workflow_ids), "version": 1, "status": "draft"})
        if method == "PUT" and path.startswith("/api/studio/v1/workflows/"):
            assert payload is not None
            return _Response(200, {"id": path.rsplit("/", 1)[-1], "status": "draft"})
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if method == "POST" and path == "/v1/deployments":
            assert payload is not None
            return _Response(
                201,
                {
                    "deployment_ref": payload["deployment_ref"],
                    "version": 1,
                    "graph_version_ref": f"{payload['graph_id']}@1",
                },
            )
        raise AssertionError((method, path, payload))

    fixture = provision_partial_failure_fixture(request=request, fixture_id="failure-001")

    saves = [payload for method, path, payload in calls if method == "PUT"]
    assert len(saves) == 2
    child_source = saves[0]["nodes"][1]["data"]["config"]["inline_source"]
    assert "payload['index'] == 3" in child_source
    assert "controlled child failure at branch 3" in child_source
    assert saves[1]["nodes"][0]["data"]["parallel_config"]["fail_mode"] == "best_effort"
    assert fixture.provider_calls_performed == 0
    assert fixture.provider_economics_status == "blocked"


def test_fixture_publishes_and_deploys_through_real_studio_apis() -> None:
    from tests.test_studio_publish_deploy import _make_env

    app, _ = _make_env()

    with TestClient(app) as client:

        def request(
            method: str, path: str, payload: dict[str, Any] | None = None
        ) -> httpx.Response:
            route = "/deployments" if path == "/v1/deployments" else path
            return client.request(method, route, json=payload)

        fixture = provision_fixture(request=request, fixture_id="real-api")
        deployments = client.get("/deployments")

    assert fixture.child_graph_version_ref.endswith("@1")
    assert fixture.parent_graph_version_ref.endswith("@1")
    assert deployments.status_code == 200
    deployed_refs = {item["deployment_ref"] for item in deployments.json()}
    assert deployed_refs == {
        fixture.child_deployment_ref,
        fixture.parent_deployment_ref,
    }


def test_fixture_manifest_round_trip_stays_explicitly_unsealed(tmp_path: Path) -> None:
    destination = tmp_path / "provider-free-composed-staging.json"

    write_unsealed_fixture_manifest(destination, _fixture())

    payload = json.loads(destination.read_text())
    assert payload["sealed"] is False
    assert payload["evidence_status"] == "staging"
    assert payload["provider_economics_status"] == "blocked"
    assert read_fixture_manifest(destination) == _fixture()

    import pytest

    with pytest.raises(FileExistsError):
        write_unsealed_fixture_manifest(destination, _fixture())


def test_http_fixture_client_targets_real_api_with_scoped_auth_headers() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            {
                "method": request.method,
                "url": str(request.url),
                "api_key": request.headers.get("X-API-Key"),
                "tenant": request.headers.get("X-Tenant-ID"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(201, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        fixture_client = HttpFixtureClient(
            base_url="http://127.0.0.1:8122",
            api_key="service-key",
            tenant_id="evaluation-studio-v1",
            client=client,
        )
        response = fixture_client("POST", "/api/studio/v1/workflows", {"name": "fixture"})

    assert response.status_code == 201
    assert captured == {
        "method": "POST",
        "url": "http://127.0.0.1:8122/api/studio/v1/workflows",
        "api_key": "service-key",
        "tenant": "evaluation-studio-v1",
        "body": {"name": "fixture"},
    }


def test_ui_summary_requires_three_exact_ordered_zero_priced_repetitions() -> None:
    result = validate_ui_summary(
        _summary(),
        expected_deployment_ref="provider-free-composed-slice-001-parent",
        expected_graph_version_ref="parent-workflow@1",
    )

    assert result["parent_run_ids"] == ["parent-1", "parent-2", "parent-3"]
    assert result["child_run_count"] == 24
    assert result["aggregate_priced_call_count"] == 0
    assert result["aggregate_cost_usd"] == 0


def test_ui_summary_rejects_relabelled_lineage_and_nonzero_priced_calls() -> None:
    bad = {
        "schema_version": 1,
        "health": {"deployment_ref": "parent-ref", "graph_version_ref": "parent@1"},
        "repetitions": [
            {
                "repetition": repetition,
                "parent_run_id": f"parent-{repetition}",
                "parent_thread_id": f"thread-{repetition}",
                "terminal_output": {"items": list(ITEMS)},
                "children": [
                    {
                        "run_id": f"child-{repetition}-{index}",
                        "thread_id": f"child-thread-{repetition}-{index}",
                        "parent_run_id": "wrong-parent" if index == 7 else f"parent-{repetition}",
                    }
                    for index in range(8)
                ],
                "economics": {
                    "priced_call_count": 1 if repetition == 3 else 0,
                    "total_cost_usd": 0,
                },
            }
            for repetition in range(1, 4)
        ],
        "restored_run_id": "parent-3",
        "provider_economics_status": "blocked",
    }

    import pytest

    with pytest.raises(RuntimeError, match="lineage|priced"):
        validate_ui_summary(
            bad,
            expected_deployment_ref="parent-ref",
            expected_graph_version_ref="parent@1",
        )


def test_bounded_ui_runner_uses_fixed_playwright_target_and_excludes_provider_keys(
    tmp_path: Path, monkeypatch
) -> None:
    frontend = tmp_path / "frontend"
    spec = frontend / "e2e/provider-free-composed-live.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// trusted fixed live spec")
    attachment = base64.b64encode(json.dumps(_summary()).encode()).decode()
    report = {
        "suites": [
            {
                "specs": [
                    {
                        "title": "provider-free composed fixture runs three times with durable lineage",
                        "tests": [
                            {
                                "results": [
                                    {
                                        "status": "passed",
                                        "attachments": [
                                            {
                                                "name": "provider-free-composed-summary",
                                                "body": attachment,
                                                "contentType": "application/json",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ]
    }
    captured: dict[str, object] = {}

    def run(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setattr(subprocess, "run", run)

    snapshot = tmp_path / "post-ui.sqlite3"

    class _Snapshotter:
        def capture(self) -> Path:
            captured["snapshot_captured"] = True
            return snapshot

    def validate_snapshot(path, **kwargs):
        captured["snapshot_validated"] = {"path": path, **kwargs}
        return {"child_run_count": 24, "snapshot": "consistent"}

    monkeypatch.setattr(provider_free_composed, "validate_persistent_snapshot", validate_snapshot)
    runner = BoundedComposedUiRunner(
        frontend_root=frontend,
        environment={
            "ZEROTH_EVALUATION_API_KEY": "service-key",
            "OPENAI_API_KEY": "must-not-reach-child",
        },
        snapshotter=_Snapshotter(),
    )

    result = runner.run(_fixture())

    assert captured["argv"] == (
        "npm",
        "exec",
        "--",
        "playwright",
        "test",
        "e2e/provider-free-composed-live.spec.ts",
        "--project=desktop-1440",
        "--grep",
        "provider-free composed fixture runs three times with durable lineage",
        "--reporter=json",
    )
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"]["PLAYWRIGHT_NO_SERVER"] == "1"
    assert captured["env"]["ZEROTH_EVALUATION_COMPOSED_PARENT_WORKFLOW_ID"] == "parent-workflow"
    assert captured["snapshot_captured"] is True
    assert captured["snapshot_validated"]["path"] == snapshot
    assert result["persistent"]["snapshot"] == "consistent"
    assert result["child_run_count"] == 24
    assert result["provider_economics_status"] == "blocked"


def test_persistent_run_validation_proves_ordered_children_lineage_and_zero_cost(
    tmp_path: Path,
) -> None:
    database = tmp_path / "zeroth.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE runs (
            tenant_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            parent_run_id TEXT,
            thread_id TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            final_output TEXT,
            metadata TEXT NOT NULL,
            execution_history TEXT NOT NULL
        )
        """
    )
    for repetition in range(1, 4):
        parent_id = f"parent-{repetition}"
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evaluation-studio-v1",
                parent_id,
                None,
                f"parent-thread-{repetition}",
                _fixture().parent_deployment_ref,
                "COMPLETED",
                json.dumps({"items": list(ITEMS)}),
                "{}",
                "[]",
            ),
        )
        for index, item in enumerate(ITEMS):
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "evaluation-studio-v1",
                    f"child-{repetition}-{index}",
                    parent_id,
                    f"child-thread-{repetition}-{index}",
                    _fixture().child_deployment_ref,
                    "COMPLETED",
                    json.dumps(item),
                    json.dumps(
                        {
                            "total_cost_usd": 0.0,
                            "total_estimated_cost_usd": 0.0,
                        }
                    ),
                    json.dumps(
                        [
                            {
                                "node_id": (
                                    f"branch:{index}:subgraph:"
                                    f"{_fixture().child_deployment_ref}:1:deterministic-delay"
                                ),
                                "started_at": (
                                    "2026-08-25T00:00:00.000000+00:00"
                                    if index < 4
                                    else "2026-08-25T00:00:00.160000+00:00"
                                ),
                                "completed_at": (
                                    "2026-08-25T00:00:00.150000+00:00"
                                    if index < 4
                                    else "2026-08-25T00:00:00.310000+00:00"
                                ),
                            }
                        ]
                    ),
                ),
            )
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "foreign-tenant",
            "foreign-child",
            "parent-1",
            "foreign-thread",
            _fixture().child_deployment_ref,
            "completed",
            json.dumps(ITEMS[0]),
            json.dumps({"total_cost_usd": 0, "total_estimated_cost_usd": 0}),
            json.dumps([{"node_id": "branch:0:subgraph:foreign:1:child-entry"}]),
        ),
    )
    connection.commit()
    connection.close()

    result = validate_persistent_snapshot(
        database,
        tenant_id="evaluation-studio-v1",
        fixture=_fixture(),
        parent_run_ids=("parent-1", "parent-2", "parent-3"),
    )

    assert result == {
        "parent_run_count": 3,
        "child_run_count": 24,
        "distinct_child_thread_count": 24,
        "ordered_branch_indices": [list(range(8))] * 3,
        "measured_peak_concurrency": [4, 4, 4],
        "aggregate_cost_usd": 0.0,
        "aggregate_estimated_cost_usd": 0.0,
        "provider_economics_status": "blocked",
    }


def test_persistent_validation_refuses_a_live_wal_database(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runs (run_id TEXT)")
    database.with_name(f"{database.name}-wal").write_bytes(b"live-wal")

    import pytest

    with pytest.raises(RuntimeError, match="consistent snapshot"):
        validate_persistent_snapshot(
            database,
            tenant_id="evaluation-studio-v1",
            fixture=_fixture(),
            parent_run_ids=("parent-1", "parent-2", "parent-3"),
        )


def test_docker_snapshotter_uses_sqlite_online_backup_inside_service_domain(
    tmp_path: Path, monkeypatch
) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text("services: {}\n")
    snapshot = tmp_path / "evidence" / "composed.sqlite3"
    snapshot.parent.mkdir()
    captured: dict[str, object] = {}

    def run(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        with sqlite3.connect(snapshot) as connection:
            connection.execute("CREATE TABLE snapshot_marker (value TEXT)")
            connection.execute("INSERT INTO snapshot_marker VALUES ('consistent')")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    snapshotter = DockerSqliteSnapshotter(
        compose_file=compose,
        compose_project="zeroth-dev",
        service="backend",
        container_database=Path("/state/zeroth.db"),
        container_snapshot=Path("/state/evidence/composed.sqlite3"),
        host_snapshot=snapshot,
    )

    assert snapshotter.capture() == snapshot.resolve()
    argv = captured["argv"]
    assert argv[:6] == (
        "docker",
        "compose",
        "-p",
        "zeroth-dev",
        "-f",
        str(compose.resolve()),
    )
    assert argv[6:10] == ("exec", "-T", "backend", "python")
    assert "source.backup(destination)" in argv[11]
    assert argv[-2:] == ("/state/zeroth.db", "/state/evidence/composed.sqlite3")
    assert not snapshot.with_name(f"{snapshot.name}-wal").exists()
