"""Shared native Python tools (``ExecutableUnit``s) used by the examples.

Tools in Zeroth aren't free-floating Python functions — they're registered
as :class:`NativeUnitManifest` bindings in an
:class:`ExecutableUnitRegistry`. Agents then declare them via
:class:`ToolAttachmentManifest`, and the orchestrator dispatches calls
through the registry. This file gives the examples a couple of real,
deterministic handlers they can register without pulling in a network or
a subprocess.
"""

from __future__ import annotations

from typing import Any

from examples._contracts import ToolInput, ToolOutput
from zeroth.core.execution_units import (
    EntryPointType,
    ExecutableUnitRegistry,
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
    RuntimeLanguage,
)

# ---- Handlers --------------------------------------------------------------
#
# Every native tool is an ``async def(ctx, data) -> dict`` — ``ctx`` is a
# runtime context object supplied by the runner (we ignore it here), and
# ``data`` is already a validated Pydantic model instance matching the
# manifest's ``input_model``.

async def format_article_handler(_ctx: Any, data: ToolInput) -> dict[str, Any]:
    """Wrap an article body in a title heading. Deterministic, offline."""
    title = data.topic.strip().title()
    body = data.body.strip()
    formatted = f"# {title}\n\n{body}\n"
    return ToolOutput(topic=data.topic, formatted=formatted).model_dump(mode="json")


async def echo_handler(_ctx: Any, data: ToolInput) -> dict[str, Any]:
    """Round-trip the payload unchanged — useful when you want a second node
    in a graph without running any real work.
    """
    return ToolOutput(topic=data.topic, formatted=data.body).model_dump(mode="json")


# ---- Registry builder -----------------------------------------------------

def build_demo_tool_registry() -> ExecutableUnitRegistry:
    """Return an :class:`ExecutableUnitRegistry` pre-populated with the demo tools.

    Registered refs:

    * ``eu://format_article`` — formats a topic+body into a Markdown article.
    * ``eu://echo`` — returns its input unchanged.
    """
    registry = ExecutableUnitRegistry()

    registry.register(
        "eu://format_article",
        NativeUnitManifest(
            unit_id="format-article",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime=RuntimeLanguage.PYTHON,
            artifact_source=PythonModuleArtifactSource(
                ref="examples._tools:format_article_handler",
            ),
            callable_ref="examples._tools:format_article_handler",
            entrypoint_type=EntryPointType.PYTHON_CALLABLE,
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://tool-input",
            output_contract_ref="contract://tool-output",
        ),
        input_model=ToolInput,
        output_model=ToolOutput,
        handler=format_article_handler,
    )

    registry.register(
        "eu://echo",
        NativeUnitManifest(
            unit_id="echo",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime=RuntimeLanguage.PYTHON,
            artifact_source=PythonModuleArtifactSource(
                ref="examples._tools:echo_handler",
            ),
            callable_ref="examples._tools:echo_handler",
            entrypoint_type=EntryPointType.PYTHON_CALLABLE,
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://tool-input",
            output_contract_ref="contract://tool-output",
        ),
        input_model=ToolInput,
        output_model=ToolOutput,
        handler=echo_handler,
    )

    return registry
