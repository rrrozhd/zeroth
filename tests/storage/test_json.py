from __future__ import annotations

from pydantic import BaseModel, Field

from zeroth.storage.json import from_json_value, load_model, load_typed_value, to_json_value


class NestedPayload(BaseModel):
    enabled: bool = True


class ExamplePayload(BaseModel):
    name: str
    count: int = Field(ge=0)
    nested: NestedPayload


def test_to_json_value_round_trips_model() -> None:
    payload = ExamplePayload(name="demo", count=2, nested=NestedPayload())

    encoded = to_json_value(payload)

    assert from_json_value(encoded) == {
        "count": 2,
        "name": "demo",
        "nested": {"enabled": True},
    }


def test_load_model_round_trips_structured_payload() -> None:
    encoded = '{"count":1,"name":"run","nested":{"enabled":false}}'

    decoded = load_model(encoded, ExamplePayload)

    assert decoded == ExamplePayload(
        name="run",
        count=1,
        nested=NestedPayload(enabled=False),
    )


def test_load_typed_value_supports_generic_annotations() -> None:
    encoded = '{"count":1,"name":"run","nested":{"enabled":false}}'

    decoded = load_typed_value(encoded, dict[str, object])

    assert decoded == {
        "count": 1,
        "name": "run",
        "nested": {"enabled": False},
    }
