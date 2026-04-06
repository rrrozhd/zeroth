"""Versioned contract registry backed by async database.

This module is the heart of the contracts system. It lets you register Pydantic
models as named, versioned "contracts" and store them in a database.
Later, you can look up a contract by name (and optionally version) to get its
schema, metadata, or even the original Python class back.

It also provides helper data classes for referencing contracts and for binding
contracts to GovernAI tools and workflow steps.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from typing import Any, TypeVar, cast

from governai.app.spec import GovernedStepSpec
from governai.tools.base import ExecutionPlacement, Tool
from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.errors import (
    ContractNotFoundError,
    ContractTypeResolutionError,
    ContractVersionExistsError,
)
from zeroth.storage import AsyncDatabase
from zeroth.storage.json import from_json_value, to_json_value

ModelT = TypeVar("ModelT", bound=BaseModel)


class ContractReference(BaseModel):
    """A lightweight pointer to a contract by name and optional version.

    Use this when you want to say "I need the contract called X" without
    loading the full contract record. If version is None, the latest version
    is assumed when the reference is resolved.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: int | None = None


class ContractVersion(BaseModel):
    """The full record for a single version of a registered contract.

    This is what you get back when you register or look up a contract. It
    contains the contract's name, version number, where to find the Python
    class, the JSON schema, any extra metadata, and when it was created.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: int
    model_path: str
    json_schema: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ToolContractBinding(BaseModel):
    """Describes how a GovernAI tool connects to its input and output contracts.

    When a tool is registered, this binding captures all the important details:
    which contracts define its input/output data shapes, how the tool should be
    executed, what capabilities it needs, and various behavioral flags like
    whether it has side effects or needs human approval.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    remote_name: str
    description: str
    input_contract: ContractReference
    output_contract: ContractReference
    executor_type: str
    execution_placement: ExecutionPlacement
    capabilities: list[str] = Field(default_factory=list)
    side_effect: bool = False
    requires_approval: bool = False
    timeout_seconds: float | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StepContractBinding(BaseModel):
    """Describes how a workflow step connects to its input and output contracts.

    Similar to ToolContractBinding but for workflow steps. It records which
    contracts a step expects as input and output, along with which tool or
    agent (if any) the step uses.
    """

    model_config = ConfigDict(frozen=True)

    flow_name: str | None = None
    step_name: str
    tool_name: str | None = None
    agent_name: str | None = None
    input_contract: ContractReference
    output_contract: ContractReference
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContractRegistry:
    """The main registry for storing and retrieving versioned contracts.

    Backed by an async database, this class lets you register Pydantic models as
    named contracts with automatic version numbering, look them up later, and even
    resolve them back to the original Python class. It also provides helpers
    for registering GovernAI tools and binding workflow steps to contracts.
    """

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database
        # Cache of (name, version) -> Python class so we don't re-import every time
        self._runtime_types: dict[tuple[str, int], type[BaseModel]] = {}

    async def register(
        self,
        model_type: type[ModelT],
        *,
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        version: int | None = None,
    ) -> ContractVersion:
        """Save a Pydantic model as a new contract version in the database.

        If you don't provide a name, the model's class name is used. If you
        don't provide a version, it automatically picks the next one.
        Raises ContractVersionExistsError if that exact name+version already exists.
        """
        contract_name = name or model_type.__name__
        # Auto-increment starts at 1 because latest_version() returns 0 for new contracts.
        resolved_version = (
            version if version is not None else await self.latest_version(contract_name) + 1
        )
        record = ContractVersion(
            name=contract_name,
            version=resolved_version,
            model_path=self._model_path(model_type),
            json_schema=model_type.model_json_schema(),
            metadata=dict(metadata or {}),
            created_at=await self._now(),
        )
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(
                """
                SELECT 1
                FROM contract_versions
                WHERE contract_name = ? AND version = ?
                """,
                (record.name, record.version),
            )
            if existing is not None:
                raise ContractVersionExistsError(
                    f"contract {record.name!r} version {record.version} already exists"
                )
            await connection.execute(
                """
                INSERT INTO contract_versions (
                    contract_name,
                    version,
                    model_path,
                    schema_json,
                    metadata_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.name,
                    record.version,
                    record.model_path,
                    to_json_value(record.json_schema),
                    to_json_value(record.metadata),
                    record.created_at,
                ),
            )
        self._runtime_types[(record.name, record.version)] = model_type
        return record

    async def register_tool(
        self,
        tool: Tool[Any, Any],
        *,
        input_name: str | None = None,
        output_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolContractBinding:
        """Register the typed contracts backing a GovernAI tool."""
        payload = dict(metadata or {})
        input_contract = await self.register(
            tool.input_model,
            name=input_name or f"{tool.name}.input",
            metadata={
                **payload,
                "governai_kind": "tool_input",
                "tool_name": tool.name,
                "remote_name": tool.remote_name,
            },
        )
        output_contract = await self.register(
            tool.output_model,
            name=output_name or f"{tool.name}.output",
            metadata={
                **payload,
                "governai_kind": "tool_output",
                "tool_name": tool.name,
                "remote_name": tool.remote_name,
            },
        )
        return ToolContractBinding(
            tool_name=tool.name,
            remote_name=tool.remote_name,
            description=tool.description,
            input_contract=ContractReference(
                name=input_contract.name,
                version=input_contract.version,
            ),
            output_contract=ContractReference(
                name=output_contract.name,
                version=output_contract.version,
            ),
            executor_type=tool.executor_type,
            execution_placement=tool.execution_placement,
            capabilities=list(tool.capabilities),
            side_effect=tool.side_effect,
            requires_approval=tool.requires_approval,
            timeout_seconds=tool.timeout_seconds,
            tags=list(tool.tags),
            metadata=payload,
        )

    def bind_step(
        self,
        step: GovernedStepSpec,
        *,
        input_contract: ContractReference,
        output_contract: ContractReference,
        flow_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> StepContractBinding:
        """Bind a GovernAI step spec to registered contracts."""
        payload = dict(metadata or {})
        step_tool = getattr(step, "tool", None)
        step_agent = getattr(step, "agent", None)
        tool_name = getattr(step_tool, "name", None) if step_tool is not None else None
        agent_name = getattr(step_agent, "name", None) if step_agent is not None else None
        return StepContractBinding(
            flow_name=flow_name,
            step_name=step.name,
            tool_name=tool_name,
            agent_name=agent_name,
            input_contract=input_contract,
            output_contract=output_contract,
            metadata=payload,
        )

    async def get(
        self,
        name: str | ContractReference,
        version: int | None = None,
    ) -> ContractVersion:
        """Look up a contract by name and optional version.

        If no version is given, returns the latest version. Raises
        ContractNotFoundError if nothing matches.
        """
        reference = self._normalize_reference(name, version)
        if reference.version is None:
            reference = ContractReference(
                name=reference.name,
                version=await self.latest_version(reference.name),
            )
        row = await self._fetch_row(reference.name, reference.version)
        if row is None:
            raise ContractNotFoundError(
                f"contract {reference.name!r} version {reference.version} does not exist"
            )
        return self._row_to_record(row)

    async def resolve(self, reference: ContractReference) -> ContractVersion:
        """Look up a contract from a ContractReference. Shorthand for get()."""
        return await self.get(reference)

    async def resolve_model_type(self, reference: ContractReference) -> type[BaseModel]:
        """Get the actual Python class for a contract reference.

        Looks up the contract, then imports and returns the original Pydantic
        model class. Results are cached so repeated calls are fast.
        Raises ContractTypeResolutionError if the class can't be imported.
        """
        record = await self.resolve(reference)
        cached = self._runtime_types.get((record.name, record.version))
        if cached is not None:
            return cached
        resolved = self._import_model_type(record.model_path)
        self._runtime_types[(record.name, record.version)] = resolved
        return resolved

    async def list_versions(self, name: str) -> list[ContractVersion]:
        """Return all registered versions of a contract, oldest first."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                """
                SELECT contract_name, version, model_path, schema_json, metadata_json, created_at
                FROM contract_versions
                WHERE contract_name = ?
                ORDER BY version ASC
                """,
                (name,),
            )
        return [self._row_to_record(row) for row in rows]

    async def list_names(self) -> list[str]:
        """Return the names of all contracts in the registry, sorted alphabetically."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                """
                SELECT DISTINCT contract_name
                FROM contract_versions
                ORDER BY contract_name ASC
                """
            )
        return [str(row["contract_name"]) for row in rows]

    async def latest_version(self, name: str) -> int:
        """Return the highest version number for a contract, or 0 if it doesn't exist yet."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                """
                SELECT MAX(version) AS version
                FROM contract_versions
                WHERE contract_name = ?
                """,
                (name,),
            )
        if row is None or row["version"] is None:
            return 0
        return int(row["version"])

    async def delete(self, name: str, version: int | None = None) -> None:
        """Remove a contract from the registry.

        If version is given, only that specific version is deleted.
        If version is None, all versions of the named contract are deleted.
        """
        async with self._database.transaction() as connection:
            if version is None:
                await connection.execute(
                    "DELETE FROM contract_versions WHERE contract_name = ?",
                    (name,),
                )
                keys_to_remove = [key for key in self._runtime_types if key[0] == name]
            else:
                await connection.execute(
                    """
                    DELETE FROM contract_versions
                    WHERE contract_name = ? AND version = ?
                    """,
                    (name, version),
                )
                keys_to_remove = [(name, version)]
        for key in keys_to_remove:
            self._runtime_types.pop(key, None)

    async def _fetch_row(self, name: str, version: int | None) -> Any:
        """Fetch a single contract row from the database, or None if not found."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                """
                SELECT contract_name, version, model_path, schema_json, metadata_json, created_at
                FROM contract_versions
                WHERE contract_name = ? AND version = ?
                """,
                (name, version),
            )
        return row

    def _row_to_record(self, row: Any) -> ContractVersion:
        """Convert a raw database row into a ContractVersion object."""
        return ContractVersion(
            name=str(row["contract_name"]),
            version=int(row["version"]),
            model_path=str(row["model_path"]),
            json_schema=cast(dict[str, Any], from_json_value(row["schema_json"]) or {}),
            metadata=cast(dict[str, Any], from_json_value(row["metadata_json"]) or {}),
            created_at=str(row["created_at"]),
        )

    def _normalize_reference(
        self,
        name: str | ContractReference,
        version: int | None,
    ) -> ContractReference:
        """Turn a name string or ContractReference into a consistent ContractReference."""
        if isinstance(name, ContractReference):
            if version is not None:
                return ContractReference(name=name.name, version=version)
            return name
        return ContractReference(name=name, version=version)

    def _model_path(self, model_type: type[BaseModel]) -> str:
        """Build a 'module:ClassName' string so we can re-import the class later."""
        return f"{model_type.__module__}:{model_type.__qualname__}"

    def _import_model_type(self, model_path: str) -> type[BaseModel]:
        """Import and return the Pydantic model class from a 'module:ClassName' path.

        Raises ContractTypeResolutionError if the path is malformed or the
        target isn't a Pydantic BaseModel subclass.
        """
        module_name, separator, qualname = model_path.partition(":")
        if not separator:
            raise ContractTypeResolutionError(f"invalid model path {model_path!r}")
        module = import_module(module_name)
        target: Any = module
        for attr in qualname.split("."):
            target = getattr(target, attr)
        if not isinstance(target, type) or not issubclass(target, BaseModel):
            raise ContractTypeResolutionError(
                f"resolved object {model_path!r} is not a Pydantic model"
            )
        return cast(type[BaseModel], target)

    async def _now(self) -> str:
        """Get the current timestamp from the database (ensures consistency with the DB)."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one("SELECT CURRENT_TIMESTAMP AS now")
        return str(row["now"])
