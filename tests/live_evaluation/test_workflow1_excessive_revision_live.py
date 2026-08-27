from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from release.live_evaluation.workflow1_excessive_revision_live import (
    DockerComposeServingController,
    ExactServingRestoreController,
    ServingIdentity,
    provision_excessive_revision_fixture,
    stage_validated_summary,
    validate_excessive_revision_summary,
)


DEPLOYMENT = "provider-free-w1-excessive-revision-w1-revision-20260826a"
GRAPH = "workflow-1-revision@1"
RUN_ID = "a" * 32


def _summary() -> dict[str, object]:
    return {
        "schema_version": 1,
        "health": {
            "status": "ok",
            "campaign_id": "evaluation-studio-v1",
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
        },
        "run": {
            "run_id": RUN_ID,
            "thread_id": RUN_ID,
            "status": "terminated_by_loop_guard",
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "failure_reason": "max_total_steps",
            "research_visit_count": 2,
            "node_visit_counts": {"request": 1, "research": 2, "revision-loop": 1},
        },
        "timeline": {
            "node_ids": ["request", "research", "revision-loop", "research"],
            "research_visit_count": 2,
        },
        "audit": {
            "verified": True,
            "signature_verified": True,
            "record_count": 4,
            "unsigned_record_count": 0,
            "audit_ids": [f"audit-{index}" for index in range(1, 5)],
            "research_audit_ids": ["audit-2", "audit-4"],
        },
        "economics": {
            "provider_calls_performed": 0,
            "provider_request_ids": [],
            "cost_event_ids": [],
            "priced_call_count": 0,
            "cost_event_count": 0,
            "total_cost_usd": 0.0,
            "cost_identity_state": "not_applicable_no_priced_call",
            "reconciliation_state": "reconciled_zero_activity",
        },
        "refresh": {
            "before_run_id": RUN_ID,
            "restored_run_id": RUN_ID,
            "restored_status": "terminated_by_loop_guard",
            "restored_failure_reason": "max_total_steps",
            "restored_research_visit_count": 2,
        },
        "d012_restore": {
            "before": {
                "status": "ok",
                "campaign_id": "evaluation-studio-v1",
                "deployment_ref": "provider-free-child-approval-d012-live-2-parent",
                "deployment_version": 1,
                "graph_version_ref": "d012-parent@1",
            },
            "after": {
                "status": "ok",
                "campaign_id": "evaluation-studio-v1",
                "deployment_ref": "provider-free-child-approval-d012-live-2-parent",
                "deployment_version": 1,
                "graph_version_ref": "d012-parent@1",
            },
            "exact": True,
        },
    }


def test_validator_accepts_exact_two_visit_signed_zero_activity_loop_guard() -> None:
    validated = validate_excessive_revision_summary(
        _summary(), expected_deployment_ref=DEPLOYMENT, expected_graph_version_ref=GRAPH
    )

    assert validated == {
        "run_id": RUN_ID,
        "research_visit_count": 2,
        "audit_record_count": 4,
        "d012_restored": True,
        "provider_calls_performed": 0,
        "total_cost_usd": 0.0,
    }


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("run", "research_visit_count"), 3, "exactly two research visits"),
        (("run", "failure_reason"), "max_visits_per_node", "max_total_steps"),
        (("audit", "signature_verified"), False, "signed audit chain"),
        (("economics", "provider_request_ids"), ["provider-1"], "provider activity"),
        (("refresh", "restored_run_id"), "b" * 32, "refresh"),
        (("d012_restore", "exact"), False, "D-012"),
    ],
)
def test_validator_rejects_relabelled_or_unreconciled_proof(
    path: tuple[str, str], value: object, message: str
) -> None:
    summary = deepcopy(_summary())
    parent = summary[path[0]]
    assert isinstance(parent, dict)
    parent[path[1]] = value

    with pytest.raises(RuntimeError, match=message):
        validate_excessive_revision_summary(
            summary,
            expected_deployment_ref=DEPLOYMENT,
            expected_graph_version_ref=GRAPH,
        )


class _Response:
    def __init__(self, status_code: int, value: object) -> None:
        self.status_code = status_code
        self.value = value
        self.text = "safe"

    def json(self) -> object:
        return self.value


def test_fixture_publishes_a_local_two_research_visit_loop() -> None:
    puts: list[dict[str, object]] = []

    def request(method: str, path: str, payload: dict[str, object] | None) -> _Response:
        if method == "POST" and path == "/api/studio/v1/contracts":
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": "workflow-1-revision"})
        if method == "PUT" and path == "/api/studio/v1/workflows/workflow-1-revision":
            assert payload is not None
            puts.append(payload)
            return _Response(200, {"id": "workflow-1-revision"})
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if method == "POST" and path == "/v1/deployments":
            return _Response(
                201,
                {
                    "deployment_ref": DEPLOYMENT,
                    "version": 1,
                    "graph_version_ref": GRAPH,
                },
            )
        raise AssertionError((method, path))

    fixture = provision_excessive_revision_fixture(
        request=request, fixture_id="w1-revision-20260826a"
    )

    assert fixture.deployment_ref == DEPLOYMENT
    assert fixture.graph_version_ref == GRAPH
    assert fixture.provider_calls_performed == 0
    assert puts[0]["execution_settings"] == {
        "max_total_steps": 4,
        "max_total_runtime_seconds": 30,
        "max_visits_per_node": 3,
        "max_visits_per_edge": 3,
        "default_timeout_seconds": 10,
    }
    nodes = puts[0]["nodes"]
    assert isinstance(nodes, list)
    assert [node["id"] for node in nodes] == ["request", "research", "revision-loop"]
    research = nodes[1]
    assert research["type"] == "code"
    assert "model_provider" not in research["data"]["config"]
    revision_loop = nodes[2]
    assert revision_loop["data"]["config"] == {
        "until": "payload.revision_required != True",
        "max_retries": 1,
    }
    edges = puts[0]["edges"]
    assert isinstance(edges, list)
    assert edges[2]["condition"] == {
        "expression": "payload.zeroth_loop['revision-loop'].route == 'repeat'",
        "branch_rule": "expression",
        "allow_cycle_traversal": True,
        "metadata": {"loop_route": "repeat"},
    }


def test_restore_controller_requires_the_exact_prejourney_identity() -> None:
    observations = iter(
        (
            {
                "status": "ok",
                "campaign_id": "evaluation-studio-v1",
                "deployment_ref": "provider-free-child-approval-d012-live-2-parent",
                "deployment_version": 1,
                "graph_version_ref": "d012-parent@1",
            },
            {
                "status": "ok",
                "campaign_id": "evaluation-studio-v1",
                "deployment_ref": "provider-free-child-approval-d012-live-2-parent",
                "deployment_version": 1,
                "graph_version_ref": "d012-parent@1",
            },
        )
    )

    def request(method: str, path: str, payload: dict[str, object] | None) -> _Response:
        assert (method, path, payload) == ("GET", "/health", None)
        return _Response(200, next(observations))

    controller = ExactServingRestoreController(request=request)
    before = controller.capture()

    assert controller.assert_restored(before) == {
        "before": {
            "status": "ok",
            "campaign_id": "evaluation-studio-v1",
            "deployment_ref": before.deployment_ref,
            "deployment_version": before.deployment_version,
            "graph_version_ref": before.graph_version_ref,
        },
        "after": {
            "status": "ok",
            "campaign_id": "evaluation-studio-v1",
            "deployment_ref": before.deployment_ref,
            "deployment_version": before.deployment_version,
            "graph_version_ref": before.graph_version_ref,
        },
        "exact": True,
    }


def test_stager_joins_playwright_summary_to_exact_restore_without_relabelling(
    tmp_path: Path,
) -> None:
    browser = tmp_path / "browser"
    attachment = browser / "indexed/exact-workflow1-excessive-revision-summary.json"
    attachment.parent.mkdir(parents=True)
    attachment.write_text(json.dumps(_summary() | {"d012_restore": None}), encoding="utf-8")
    (browser / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": True,
                "artifacts": [
                    {
                        "source": "indexed/exact-workflow1-excessive-revision-summary.json",
                        "destination": "console/exact-workflow1-excessive-revision-summary.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    restore = _summary()["d012_restore"]
    assert isinstance(restore, dict)
    destination = tmp_path / "runtime/summary.json"

    staged = stage_validated_summary(
        browser_root=browser,
        destination=destination,
        restore=restore,
        expected_deployment_ref=DEPLOYMENT,
        expected_graph_version_ref=GRAPH,
    )

    assert staged == destination
    assert json.loads(destination.read_text())["d012_restore"] == restore
    with pytest.raises(FileExistsError):
        stage_validated_summary(
            browser_root=browser,
            destination=destination,
            restore=restore,
            expected_deployment_ref=DEPLOYMENT,
            expected_graph_version_ref=GRAPH,
        )


def test_live_controller_selects_bounded_docker_recreate_for_this_environment(
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.dev.yml").write_text("services: {}\n", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], Path, str]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def command_runner(argv, *, cwd, env, capture_output, text, check, timeout):
        assert (capture_output, text, check, timeout) == (True, True, False, 120)
        calls.append((tuple(argv), cwd, env["ZEROTH_DEV_DEPLOYMENT_REF"]))
        return _Completed()

    def request(method: str, path: str, payload: dict[str, object] | None) -> _Response:
        assert (method, path, payload) == ("GET", "/health", None)
        return _Response(
            200,
            {
                "status": "ok",
                "campaign_id": "evaluation-studio-v1",
                "deployment_ref": DEPLOYMENT,
                "deployment_version": 1,
                "graph_version_ref": GRAPH,
            },
        )

    controller = DockerComposeServingController(
        request=request,
        workspace=tmp_path,
        compose_file=tmp_path / "compose.dev.yml",
        command_runner=command_runner,
        sleep=lambda _: None,
    )

    served = controller.serve(
        deployment_ref=DEPLOYMENT,
        deployment_version=1,
        graph_version_ref=GRAPH,
    )

    assert served == ServingIdentity(DEPLOYMENT, 1, GRAPH)
    assert calls == [
        (
            (
                "docker",
                "compose",
                "-f",
                str(tmp_path / "compose.dev.yml"),
                "up",
                "-d",
                "--force-recreate",
                "backend",
            ),
            tmp_path,
            DEPLOYMENT,
        )
    ]
