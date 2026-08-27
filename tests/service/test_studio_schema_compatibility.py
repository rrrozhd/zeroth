from __future__ import annotations

from types import SimpleNamespace

from zeroth.service.api import studio_schemas
from zeroth.service.app import create_app


def _legacy_workflow_detail() -> dict[str, object]:
    return {
        "id": "workflow-legacy",
        "name": "Legacy workflow",
        "version": 1,
        "status": "draft",
        "entry_step": None,
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "updated_at": "2026-08-26T00:00:00Z",
    }


def test_workflow_detail_defaults_execution_settings_for_legacy_bodies() -> None:
    detail = studio_schemas.WorkflowDetailResponse.model_validate(_legacy_workflow_detail())

    assert detail.execution_settings == studio_schemas.StudioExecutionSettings()


def test_studio_edge_models_accept_the_legacy_wire_shape() -> None:
    assert hasattr(studio_schemas, "StudioEdgeInput")
    legacy = {"id": "edge-legacy", "source": "start", "target": "finish"}

    request_edge = studio_schemas.StudioEdgeInput.model_validate(legacy)
    response_edge = studio_schemas.StudioEdgeResponse.model_validate(legacy)

    assert request_edge.id == response_edge.id == "edge-legacy"
    assert request_edge.kind == response_edge.kind == "data"


def test_openapi_keeps_explicit_studio_edge_request_and_response_refs() -> None:
    schema = create_app(SimpleNamespace(regulus_client=None)).openapi()
    schemas = schema["components"]["schemas"]

    request_ref = schemas["UpdateWorkflowRequest"]["properties"]["edges"]["anyOf"][0]["items"][
        "$ref"
    ]
    response_ref = schemas["WorkflowDetailResponse"]["properties"]["edges"]["items"]["$ref"]

    assert request_ref == "#/components/schemas/StudioEdgeInput"
    assert response_ref == "#/components/schemas/StudioEdgeResponse"
    assert "StudioEdgeResponse-Input" not in schemas
    assert "StudioEdgeResponse-Output" not in schemas
