"""Schema-only contracts — authored as raw JSON Schema, no Python class.

Covers the synthesized runtime model (exact jsonschema validation, schema
round-trip), registry registration/resolution/versioning, and the studio
endpoint that lets the console register them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_studio_publish_deploy import _make_env
from zeroth.core.contracts.registry import ContractReference, ContractRegistry
from zeroth.core.contracts.schema_model import model_from_json_schema
from zeroth.core.service.bootstrap import run_migrations
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "pattern": "^ord-"},
        "quantity": {"type": "integer", "minimum": 1},
    },
    "required": ["order_id"],
    "additionalProperties": False,
}


def _registry() -> ContractRegistry:
    tmp = Path(tempfile.mkdtemp())
    run_migrations(f"sqlite:///{tmp / 'contracts.db'}")
    return ContractRegistry(AsyncSQLiteDatabase(str(tmp / "contracts.db")))


# ---- synthesized model -------------------------------------------------------


def test_schema_model_validates_arbitrary_keywords_exactly() -> None:
    model = model_from_json_schema("contract://order", ORDER_SCHEMA)

    ok = model.model_validate({"order_id": "ord-7", "quantity": 2})
    assert ok.model_dump() == {"order_id": "ord-7", "quantity": 2}

    with pytest.raises(ValidationError, match="does not match"):
        model.model_validate({"order_id": "seven"})
    with pytest.raises(ValidationError, match="less than the minimum"):
        model.model_validate({"order_id": "ord-7", "quantity": 0})
    with pytest.raises(ValidationError, match="Additional properties"):
        model.model_validate({"order_id": "ord-7", "extra": True})


def test_schema_validation_errors_serialize_for_http_422() -> None:
    """Value-error entries carry the raw exception in ctx — the run API must be
    able to JSON-serialize what it puts in the 422 detail (else it 500s)."""
    import json

    model = model_from_json_schema("contract://order", ORDER_SCHEMA)
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate({"order_id": "wrong"})
    json.dumps(excinfo.value.errors(include_url=False, include_context=False))


def test_schema_model_round_trips_the_authored_schema() -> None:
    model = model_from_json_schema("contract://order", ORDER_SCHEMA)
    assert model.model_json_schema() == ORDER_SCHEMA


# ---- registry ----------------------------------------------------------------


async def test_register_schema_resolves_and_versions() -> None:
    registry = _registry()
    first = await registry.register_schema("contract://order", ORDER_SCHEMA)
    assert first.version == 1
    assert first.model_path == ""

    model = await registry.resolve_model_type(ContractReference(name="contract://order"))
    assert model.model_validate({"order_id": "ord-1"}).model_dump() == {"order_id": "ord-1"}

    # Re-registering the same name creates the next version.
    second = await registry.register_schema(
        "contract://order", {**ORDER_SCHEMA, "required": []}
    )
    assert second.version == 2


async def test_register_schema_rejects_malformed_schema() -> None:
    from jsonschema.exceptions import SchemaError

    registry = _registry()
    with pytest.raises(SchemaError):
        await registry.register_schema("contract://bad", {"type": "not-a-type"})


# ---- studio endpoint -----------------------------------------------------------


async def test_studio_creates_contract_and_picker_lists_it() -> None:
    app, _registry_obj = _make_env()

    with TestClient(app) as client:
        created = client.post(
            "/api/studio/v1/contracts",
            json={"name": "contract://order", "json_schema": ORDER_SCHEMA},
        )
        assert created.status_code == 201, created.text
        assert created.json()["version"] == 1

        listing = client.get("/api/studio/v1/contracts")
        names = {c["name"] for c in listing.json()}
        assert "contract://order" in names

        bad = client.post(
            "/api/studio/v1/contracts",
            json={"name": "contract://bad", "json_schema": {"type": "not-a-type"}},
        )
        assert bad.status_code == 422
        assert "invalid JSON Schema" in bad.json()["detail"]

        spaced = client.post(
            "/api/studio/v1/contracts",
            json={"name": "has spaces", "json_schema": ORDER_SCHEMA},
        )
        assert spaced.status_code == 422
