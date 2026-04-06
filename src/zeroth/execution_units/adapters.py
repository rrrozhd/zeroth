"""Runtime adapters that turn manifest descriptions into runnable GovernAI tools.

Each adapter knows how to take a manifest (a description of what to run) and
produce a GovernAI Tool object that can actually be executed. Think of adapters
as translators between Zeroth's manifest format and GovernAI's tool system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Protocol, TypeVar

from governai import Tool
from governai.tools.python_tool import PythonHandler, PythonTool

from zeroth.execution_units.errors import UnsupportedRuntimeAdapterError
from zeroth.execution_units.models import (
    ExecutableUnitManifest,
    NativeUnitManifest,
    ProjectUnitManifest,
    WrappedCommandUnitManifest,
)
from zeroth.execution_units.validator import ExecutableUnitValidator

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class RuntimeAdapter(Protocol[InputT, OutputT]):
    """Interface that all runtime adapters must follow.

    Any class that implements this protocol can convert a manifest into a
    runnable GovernAI Tool. The `supports` method checks compatibility, and
    `materialize` does the actual conversion.
    """

    runtime: str

    def supports(self, manifest: ExecutableUnitManifest) -> bool:
        """Return True if this adapter can handle the given manifest."""
        ...

    def materialize(
        self,
        manifest: ExecutableUnitManifest,
        *,
        input_model: type[InputT],
        output_model: type[OutputT],
        handler: PythonHandler | None = None,
    ) -> Tool[InputT, OutputT]:
        """Convert a manifest into a ready-to-run GovernAI Tool."""
        ...


class BaseRuntimeAdapter(ABC):
    """Base class that handles validation before building a tool.

    Subclasses only need to implement `_build_tool`. This class takes care of
    validating the manifest and checking that the adapter supports it before
    the subclass builds the actual tool.
    """

    runtime: str

    def __init__(self, validator: ExecutableUnitValidator | None = None):
        self._validator = validator or ExecutableUnitValidator()

    def supports(self, manifest: ExecutableUnitManifest) -> bool:
        """Check if this adapter's runtime matches the manifest's runtime."""
        return self.runtime == manifest.runtime.value

    @abstractmethod
    def _build_tool(
        self,
        manifest: ExecutableUnitManifest,
        *,
        input_model: type[InputT],
        output_model: type[OutputT],
        handler: PythonHandler | None = None,
    ) -> Tool[InputT, OutputT]:
        """Build a GovernAI Tool from a validated manifest. Subclasses must implement this."""
        raise NotImplementedError

    def materialize(
        self,
        manifest: ExecutableUnitManifest,
        *,
        input_model: type[InputT],
        output_model: type[OutputT],
        handler: PythonHandler | None = None,
    ) -> Tool[InputT, OutputT]:
        """Validate the manifest, then build and return a GovernAI Tool."""
        # Validate first so we fail fast before doing any real work
        self._validator.validate_or_raise(manifest)
        if not self.supports(manifest):
            raise UnsupportedRuntimeAdapterError(
                f"adapter {self.__class__.__name__} does not support {manifest.runtime.value}"
            )
        return self._build_tool(
            manifest,
            input_model=input_model,
            output_model=output_model,
            handler=handler,
        )


class PythonRuntimeAdapter(BaseRuntimeAdapter):
    """Adapter for running Python functions directly as GovernAI tools.

    Use this when your executable unit is a native Python callable (a function
    or method) rather than a command-line program.
    """

    runtime = "python"

    def _build_tool(
        self,
        manifest: ExecutableUnitManifest,
        *,
        input_model: type[InputT],
        output_model: type[OutputT],
        handler: PythonHandler | None = None,
    ) -> Tool[InputT, OutputT]:
        """Build a PythonTool from a native manifest and its handler function."""
        if not isinstance(manifest, NativeUnitManifest):
            raise UnsupportedRuntimeAdapterError(
                "PythonRuntimeAdapter only supports native manifests"
            )
        if handler is None:
            raise UnsupportedRuntimeAdapterError("PythonRuntimeAdapter requires a handler callable")
        return PythonTool(
            name=manifest.unit_id,
            handler=handler,
            input_model=input_model,
            output_model=output_model,
            capabilities=list(manifest.capability_requests),
            side_effect=manifest.side_effect,
            timeout_seconds=manifest.timeout_seconds,
            # Approval is handled at a higher layer, not per-tool
            requires_approval=False,
            tags=[manifest.onboarding_mode.value, "python"],
            execution_placement=manifest.execution_placement,
            remote_name=manifest.callable_ref,
        )


class CommandRuntimeAdapter(BaseRuntimeAdapter):
    """Adapter for running command-line programs as GovernAI tools.

    Use this when your executable unit is a shell command or CLI program
    rather than a Python function.
    """

    runtime = "command"

    def _command_for(self, manifest: ExecutableUnitManifest) -> Sequence[str]:
        """Figure out what command to run based on the manifest's config."""
        if isinstance(manifest, WrappedCommandUnitManifest):
            if manifest.run_config.command:
                return manifest.run_config.command
            return [manifest.artifact_source.ref]
        if isinstance(manifest, ProjectUnitManifest):
            if manifest.run_config.command:
                return manifest.run_config.command
            return [manifest.artifact_source.ref]
        raise UnsupportedRuntimeAdapterError(
            "CommandRuntimeAdapter only supports command manifests"
        )

    def _build_tool(
        self,
        manifest: ExecutableUnitManifest,
        *,
        input_model: type[InputT],
        output_model: type[OutputT],
        handler: PythonHandler | None = None,
    ) -> Tool[InputT, OutputT]:
        """Build a CLI-based GovernAI Tool that sends JSON via stdin and reads JSON from stdout."""
        command = list(self._command_for(manifest))
        return Tool.from_cli(
            name=manifest.unit_id,
            command=command,
            input_model=input_model,
            output_model=output_model,
            input_mode="json-stdin",
            output_mode="json-stdout",
            description=manifest.metadata.get("description", ""),
            capabilities=list(manifest.capability_requests),
            side_effect=manifest.side_effect,
            timeout_seconds=manifest.timeout_seconds,
            requires_approval=False,
            tags=[manifest.onboarding_mode.value, "command"],
            execution_placement=manifest.execution_placement,
            remote_name=command[0],
        )
