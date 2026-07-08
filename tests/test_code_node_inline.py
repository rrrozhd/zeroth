"""Inline executable units — the Studio code node.

Covers the three layers of the feature: the content-addressed inline manifest
and node-data invariants, sandboxed execution of authored source through the
ExecutableUnitRunner, and the studio round trip (canvas type "code") with the
publish syntax gate.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_studio_publish_deploy import _make_env, _register_contracts
from zeroth.core.execution_units import (
    ExecutableUnitRunner,
    build_inline_binding,
    build_inline_manifest,
    inline_source_digest,
)
from zeroth.core.graph.models import ExecutableUnitNodeData

TRANSFORM_SOURCE = """
import json
import sys

data = json.load(sys.stdin)
json.dump({"answer": data["question"].upper(), "length": len(data["question"])}, sys.stdout)
""".strip()


# ---- manifest + node-data invariants ---------------------------------------


def test_inline_manifest_is_content_addressed() -> None:
    manifest = build_inline_manifest("step", TRANSFORM_SOURCE)
    assert manifest.artifact_source.ref == inline_source_digest(TRANSFORM_SOURCE)
    assert manifest.artifact_source.ref.startswith("sha256:")
    # Same source, same identity; different source, different identity.
    assert build_inline_manifest("other", TRANSFORM_SOURCE).artifact_source.ref == (
        manifest.artifact_source.ref
    )
    assert (
        build_inline_manifest("step", TRANSFORM_SOURCE + "\n# edited").artifact_source.ref
        != manifest.artifact_source.ref
    )


def test_inline_manifest_rejects_empty_source() -> None:
    with pytest.raises(ValidationError):
        build_inline_manifest("step", "   \n")


def test_node_data_requires_exactly_one_code_source() -> None:
    with pytest.raises(ValidationError):
        ExecutableUnitNodeData(
            manifest_ref="eu://x",
            execution_mode="native",
            inline_source="print('hi')",
        )
    with pytest.raises(ValidationError):
        ExecutableUnitNodeData(execution_mode="inline")  # inline mode, no source
    with pytest.raises(ValidationError):
        ExecutableUnitNodeData(execution_mode="native", inline_source="print('hi')")
    data = ExecutableUnitNodeData(execution_mode="inline", inline_source="print('hi')")
    assert data.manifest_ref == ""


# ---- sandboxed execution ----------------------------------------------------


@pytest.mark.asyncio
async def test_inline_unit_executes_authored_source() -> None:
    runner = ExecutableUnitRunner()
    binding = build_inline_binding("transform", TRANSFORM_SOURCE)

    result = await runner.run_binding(binding, {"question": "what is zeroth"})

    assert result.output_data == {"answer": "WHAT IS ZEROTH", "length": 14}
    assert result.audit_record["sandboxed"] is True
    assert result.audit_record["execution_mode"] == "inline"
    assert result.manifest_ref.startswith("inline://transform/sha256:")


@pytest.mark.asyncio
async def test_inline_unit_failure_surfaces_stderr() -> None:
    runner = ExecutableUnitRunner()
    binding = build_inline_binding("boom", "raise RuntimeError('kaput')")

    with pytest.raises(Exception, match="kaput"):
        await runner.run_binding(binding, {"question": "x"})


# ---- studio round trip + publish gate ----------------------------------------


def _code_node_payload(source: str, node_id: str = "transform") -> dict:
    return {
        "id": node_id,
        "type": "code",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "Transform",
            "input_contract_ref": "contract://q",
            "output_contract_ref": "contract://a",
            "config": {"inline_source": source, "execution_mode": "inline"},
        },
    }


def _entry_node_payload() -> dict:
    return {
        "id": "start",
        "type": "entrypoint",
        "position": {"x": -200, "y": 0},
        "data": {
            "label": "Start",
            "input_contract_ref": "contract://q",
            "output_contract_ref": "contract://q",
            "config": {},
        },
    }


async def test_code_node_round_trips_and_publish_gates_on_syntax() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        wf = f"/api/studio/v1/workflows/{created['id']}"

        # Draft with a syntax error saves fine — publish is the gate.
        updated = client.put(
            wf,
            json={
                "nodes": [_entry_node_payload(), _code_node_payload("def broken(:\n  pass")],
                "edges": [{"id": "e1", "source": "start", "target": "transform"}],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["entry_step"] == "start"  # derived from the entrypoint node
        node = next(n for n in updated.json()["nodes"] if n["id"] == "transform")
        assert node["type"] == "code"  # round-trips as the canvas type
        assert node["data"]["config"]["inline_source"].startswith("def broken")

        published = client.post(f"{wf}/publish")
        assert published.status_code == 422
        issues = published.json()["detail"]["issues"]
        assert any(
            i["code"] == "invalid_inline_source" and i["node_id"] == "transform" for i in issues
        ), issues

        # Valid source publishes, and stays typed "code" when read back.
        client.put(
            wf,
            json={
                "nodes": [_entry_node_payload(), _code_node_payload(TRANSFORM_SOURCE)],
                "edges": [{"id": "e1", "source": "start", "target": "transform"}],
            },
        )
        published = client.post(f"{wf}/publish")
        assert published.status_code == 200, published.text
        types = {n["id"]: n["type"] for n in published.json()["nodes"]}
        assert types["transform"] == "code"
        assert types["start"] == "entrypoint"


async def test_code_node_missing_source_key_still_saves_as_draft() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        payload = _code_node_payload("", node_id="empty")
        del payload["data"]["config"]["inline_source"]  # cleared editor drops the key
        resp = client.put(
            f"/api/studio/v1/workflows/{created['id']}",
            json={"nodes": [payload], "edges": []},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["nodes"][0]["data"]["config"]["inline_source"] == ""
