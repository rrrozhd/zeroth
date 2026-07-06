from __future__ import annotations

from enum import StrEnum

from governai.app.spec import GovernedFlowSpec, GovernedStepSpec
from governai.tools.python_tool import tool
from pydantic import BaseModel, Field

from zeroth.core.contracts import ContractReference, ContractRegistry
from zeroth.core.contracts.errors import ContractNotFoundError


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
    registry = ContractRegistry(sqlite_db)

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
    registry = ContractRegistry(sqlite_db)

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
    registry = ContractRegistry(sqlite_db)

    await registry.register(CustomerV1, name="customer")

    try:
        await registry.get("customer", 2)
    except ContractNotFoundError:
        pass
    else:  # pragma: no cover - defensive test guard
        raise AssertionError("missing contract version should raise")


async def test_registry_binds_governai_tool_and_step_specs(sqlite_db) -> None:
    registry = ContractRegistry(sqlite_db)
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
