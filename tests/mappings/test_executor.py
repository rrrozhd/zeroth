from __future__ import annotations

from zeroth.mappings.executor import MappingExecutor
from zeroth.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
)


def test_mapping_executor_applies_nested_operations() -> None:
    executor = MappingExecutor()
    mapping = EdgeMapping(
        operations=[
            PassthroughMappingOperation(
                source_path="payload.user.name",
                target_path="request.user.name",
            ),
            RenameMappingOperation(
                source_path="payload.user.id",
                target_path="request.user.identifier",
            ),
            ConstantMappingOperation(
                target_path="request.source",
                value="zeroth",
            ),
            DefaultMappingOperation(
                source_path="payload.user.locale",
                target_path="request.user.locale",
                default_value="en-US",
            ),
        ]
    )

    output = executor.execute(
        {"payload": {"user": {"name": "Ada", "id": 7}}},
        mapping,
    )

    assert output == {
        "request": {
            "source": "zeroth",
            "user": {
                "identifier": 7,
                "locale": "en-US",
                "name": "Ada",
            },
        }
    }


def test_mapping_executor_uses_source_value_before_default() -> None:
    executor = MappingExecutor()
    mapping = EdgeMapping(
        operations=[
            DefaultMappingOperation(
                source_path="payload.user.locale",
                target_path="request.user.locale",
                default_value="en-US",
            )
        ]
    )

    output = executor.execute(
        {"payload": {"user": {"locale": "pt-BR"}}},
        mapping,
    )

    assert output == {"request": {"user": {"locale": "pt-BR"}}}
