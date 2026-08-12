from __future__ import annotations

from enum import StrEnum

import pytest
from pydantic import BaseModel, Field

from zeroth.contracts.governed.app.spec import GovernedFlowSpec, GovernedStepSpec
from zeroth.runtime.agents.tooling.python_tool import tool

from zeroth.contracts.registry import (
    ContractReference,
    ContractRegistry,
    contract_scope_context,
)
from zeroth.contracts.registry.errors import ContractNotFoundError
from zeroth.platform.storage import ScopeContext, TenantWideScopeContext


class Address(BaseModel):
    street: str
    unit: str | None = None


class Color(StrEnum):
    RED = "red"
    GREEN = "green"


class CustomerV1(BaseModel):
    name: str = Field(description="Customer name")
    address: Address
    labels: list[str]
    favorite_color: Color
    nickname: str | None = None


class CustomerV2(BaseModel):
    name: str = Field(description="Customer name")
    address: Address
    labels: list[str]
    favorite_color: Color
    nickname: str | None = None
    active: bool = True


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    upper: str


def _scope(tenant_id: str) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, workspace_id="contracts")


@pytest.mark.parametrize(
    ("tenant_id", "workspace_id", "expected_type"),
    [
        ("tenant-a", "workspace-a", ScopeContext),
        ("tenant-a", None, TenantWideScopeContext),
        ("default", "workspace-a", ScopeContext),
        ("default", None, TenantWideScopeContext),
    ],
)
def test_contract_scope_context_preserves_trusted_owner_shape(
    tenant_id: str,
    workspace_id: str | None,
    expected_type: type[ScopeContext] | type[TenantWideScopeContext],
) -> None:
    context = contract_scope_context(tenant_id, workspace_id)

    assert type(context) is expected_type
    assert context.tenant_id == tenant_id
    if type(context) is ScopeContext:
        assert context.workspace_id == workspace_id


def test_legacy_constructor_remains_reserved_default_compatible(sqlite_db) -> None:
    assert isinstance(ContractRegistry(sqlite_db), ContractRegistry)


def test_scoped_registry_requires_an_exact_scope(sqlite_db) -> None:
    with pytest.raises(TypeError, match="scope_context"):
        ContractRegistry.scoped(sqlite_db, None)  # type: ignore[arg-type]


async def test_contract_names_and_versions_are_tenant_local(sqlite_db) -> None:
    tenant_a = ContractRegistry.scoped(sqlite_db, _scope("tenant-a"))
    tenant_b = ContractRegistry.scoped(sqlite_db, _scope("tenant-b"))

    a_record = await tenant_a.register(CustomerV1, name="customer", version=1)
    b_record = await tenant_b.register(CustomerV2, name="customer", version=1)

    assert a_record.version == b_record.version == 1
    assert await tenant_a.list_names() == ["customer"]
    assert await tenant_b.list_names() == ["customer"]
    assert (await tenant_a.get("customer", 1)).model_path.endswith(":CustomerV1")
    assert (await tenant_b.get("customer", 1)).model_path.endswith(":CustomerV2")
    assert (
        await tenant_a.resolve_model_type(ContractReference(name="customer", version=1))
        is CustomerV1
    )
    assert (
        await tenant_b.resolve_model_type(ContractReference(name="customer", version=1))
        is CustomerV2
    )


async def test_other_tenant_contracts_are_not_enumerated_or_addressable(sqlite_db) -> None:
    tenant_a = ContractRegistry.scoped(sqlite_db, _scope("tenant-a"))
    tenant_b = ContractRegistry.scoped(sqlite_db, _scope("tenant-b"))
    await tenant_a.register(CustomerV1, name="a-only")

    assert await tenant_b.list_names() == []
    assert await tenant_b.latest_version("a-only") == 0
    with pytest.raises(ContractNotFoundError):
        await tenant_b.get("a-only", 1)


@tool(
    name="echo",
    input_model=EchoInput,
    output_model=EchoOutput,
    capabilities=["memory_read"],
    side_effect=False,
)
async def echo_tool(ctx, data):  # noqa: ANN001, ARG001
    return {"message": data.message, "upper": data.message.upper()}


async def test_registry_crud_and_versioning(sqlite_db) -> None:
    registry = ContractRegistry.for_default_compatibility(sqlite_db)

    first = await registry.register(CustomerV1, name="customer", metadata={"owner": "platform"})
    second = await registry.register(CustomerV2, name="customer", metadata={"owner": "platform"})

    assert first.version == 1
    assert second.version == 2
    assert await registry.list_names() == ["customer"]
    assert await registry.latest_version("customer") == 2
    assert [record.version for record in await registry.list_versions("customer")] == [1, 2]
    assert (await registry.get("customer", 1)).metadata == {"owner": "platform"}
    assert (await registry.resolve(ContractReference(name="customer"))).version == 2
    assert (
        await registry.resolve_model_type(ContractReference(name="customer", version=1))
    ) is CustomerV1

    await registry.delete("customer", 1)

    assert [record.version for record in await registry.list_versions("customer")] == [2]
    assert (await registry.get("customer")).version == 2
    assert await registry.latest_version("customer") == 2

    await registry.delete("customer")

    assert await registry.list_names() == []
    assert await registry.latest_version("customer") == 0


async def test_registry_supports_nested_optional_enum_and_array_schema(sqlite_db) -> None:
    registry = ContractRegistry.for_default_compatibility(sqlite_db)

    record = await registry.register(CustomerV1, name="customer")

    schema = record.json_schema
    customer_properties = schema["properties"]

    assert schema["$defs"]["Address"]["properties"]["street"]["type"] == "string"
    assert customer_properties["labels"]["type"] == "array"
    assert customer_properties["favorite_color"]["$ref"] == "#/$defs/Color"
    assert schema["$defs"]["Color"]["enum"] == ["red", "green"]
    assert customer_properties["name"]["description"] == "Customer name"
    assert "nickname" not in schema["required"]
    assert any(option.get("type") == "null" for option in customer_properties["nickname"]["anyOf"])


async def test_registry_raises_for_missing_versions(sqlite_db) -> None:
    registry = ContractRegistry.for_default_compatibility(sqlite_db)

    await registry.register(CustomerV1, name="customer")

    try:
        await registry.get("customer", 2)
    except ContractNotFoundError:
        pass
    else:  # pragma: no cover - defensive test guard
        raise AssertionError("missing contract version should raise")


async def test_registry_binds_governai_tool_and_step_specs(sqlite_db) -> None:
    registry = ContractRegistry.for_default_compatibility(sqlite_db)
    step = GovernedStepSpec(name="echo_step", tool=echo_tool)
    flow = GovernedFlowSpec(name="demo_flow", steps=[step], entry_step="echo_step")

    binding = await registry.register_tool(echo_tool, metadata={"flow_name": flow.name})
    step_binding = registry.bind_step(
        step,
        flow_name=flow.name,
        input_contract=binding.input_contract,
        output_contract=binding.output_contract,
    )

    assert binding.tool_name == "echo"
    assert binding.remote_name == "echo"
    assert binding.executor_type == "python"
    assert binding.capabilities == ["memory_read"]
    assert (await registry.resolve_model_type(binding.input_contract)) is EchoInput
    assert (await registry.resolve_model_type(binding.output_contract)) is EchoOutput
    assert step_binding.flow_name == "demo_flow"
    assert step_binding.step_name == "echo_step"
    assert step_binding.tool_name == "echo"
    assert flow.entry_step == "echo_step"
