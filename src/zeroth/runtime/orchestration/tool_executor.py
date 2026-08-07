"""Governed executable-unit invocation for the orchestration runtime.

:class:`RuntimeToolExecutor` owns every way the runtime runs an executable
unit — as a graph step by manifest ref, as a Studio code node whose source
travels in the graph, and as a tool an agent calls mid-loop. Routing all three
through one collaborator is what keeps their enforcement identical: the calling
node's enforcement context is threaded into every one of them, so an
agent-invoked unit is sandboxed exactly like a directly dispatched one.
"""

from __future__ import annotations

import inspect
import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from zeroth.contracts.graph import ExecutableUnitNode, Graph, Node, OperationIdentity
from zeroth.runtime.orchestration.errors import NodeDispatcherError


def _supported_kwargs(parameters: Mapping[str, Any], **candidates: Any) -> dict[str, Any]:
    """Keep only the keyword arguments this runner actually declares.

    Executable-unit runners are a third-party extension point, so optional
    kwargs are offered rather than imposed -- the same capability-sniffing that
    let ``enforcement_context`` be added without breaking existing runners.
    """
    return {name: value for name, value in candidates.items() if name in parameters}


def node_by_id(graph: Graph, node_id: str) -> Node:
    """Find a node in the graph by its ID. Raises KeyError if not found."""
    for node in graph.nodes:
        if node.node_id == node_id:
            return node
    raise KeyError(node_id)


@dataclass(frozen=True, slots=True)
class RuntimeToolExecutor:
    """Runs executable units on behalf of graph steps and agent tool calls."""

    executable_unit_runner: Any

    async def run_unit(
        self,
        manifest_ref: str,
        input_payload: Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any],
        operation_identity: OperationIdentity | None = None,
    ) -> Any:
        """Call executable-unit runners with enforcement context when supported."""
        parameters = inspect.signature(self.executable_unit_runner.run).parameters
        optional = _supported_kwargs(
            parameters,
            enforcement_context=enforcement_context,
            operation_identity=operation_identity,
        )
        return await self.executable_unit_runner.run(manifest_ref, input_payload, **optional)

    async def run_inline(
        self,
        node: ExecutableUnitNode,
        input_payload: Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any],
        operation_identity: OperationIdentity | None = None,
    ) -> Any:
        """Run a code node whose source travels in the graph.

        Studio code node: the source travels in the graph, so the runner
        synthesizes the binding on demand rather than looking it up in the
        registry; the ``run_inline_source`` seam keeps the binding synthesis
        inside the execution integrations package. Runs through the same
        sandboxed subprocess path as a registered unit.
        """
        optional = _supported_kwargs(
            inspect.signature(self.executable_unit_runner.run_inline_source).parameters,
            operation_identity=operation_identity,
        )
        return await self.executable_unit_runner.run_inline_source(
            node.node_id,
            node.executable_unit.inline_source,
            input_payload,
            timeout_seconds=node.executable_unit.timeout_seconds,
            enforcement_context=enforcement_context,
            **optional,
        )

    def build(
        self,
        graph: Graph,
        enforcement_context: Mapping[str, Any] | None = None,
        operation_identity_factory: Callable[[str, int], OperationIdentity] | None = None,
    ) -> Any:
        """Build the executor that runs an agent's attached tool nodes.

        The AgentRunner's tool-call loop hands it the resolved binding (whose
        ``executable_unit_ref`` is ``node://<node_id>`` for graph attachments)
        and the model-supplied arguments; the target node runs exactly like it
        would as a graph step — inline source through the sandboxed subprocess
        path, manifest refs through the registry.

        WS-C: ``enforcement_context`` (the calling agent's) is threaded into the
        unit run so the sandbox applies the same network/secret enforcement it
        would for a direct node dispatch. Passing it unconditionally (even when
        empty) closes the prior bypass where agent-invoked units ran ungated.
        """
        context: Mapping[str, Any] = enforcement_context or {}
        # An agent turn can call several tools. Each call is its own logical
        # operation, so the ordinal advances per call -- one shared identity
        # would make the second call look like a duplicate of the first.
        call_ordinal = itertools.count()

        async def execute(binding: Any, arguments: Mapping[str, Any] | None) -> Any:
            target_node_id = str(binding.executable_unit_ref).removeprefix("node://")
            target = node_by_id(graph, target_node_id)
            if not isinstance(target, ExecutableUnitNode):
                raise NodeDispatcherError(
                    f"tool {binding.alias!r} targets {target_node_id!r}, "
                    "which is not an executable unit node"
                )
            payload = dict(arguments or {})
            inline = target.executable_unit.inline_source is not None
            target_ref = (
                f"node://{target_node_id}" if inline else target.executable_unit.manifest_ref
            )
            identity = (
                None
                if operation_identity_factory is None
                else operation_identity_factory(target_ref, next(call_ordinal))
            )
            if inline:
                result = await self.run_inline(
                    target,
                    payload,
                    enforcement_context=context,
                    operation_identity=identity,
                )
            else:
                result = await self.run_unit(
                    target.executable_unit.manifest_ref,
                    payload,
                    enforcement_context=context,
                    operation_identity=identity,
                )
            return result.output_data

        return execute
