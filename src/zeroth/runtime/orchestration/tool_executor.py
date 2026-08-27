"""Governed executable-unit invocation for the orchestration runtime.

:class:`RuntimeToolExecutor` owns every way the runtime runs an executable
unit — as a graph step by manifest ref, as a Studio code node whose source
travels in the graph, and as a tool an agent calls mid-loop. Routing all three
through one collaborator is what keeps their enforcement identical: the calling
node's enforcement context is threaded into every one of them, so an
agent-invoked unit is sandboxed exactly like a directly dispatched one.
"""

from __future__ import annotations

import hashlib
import inspect
import itertools
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from zeroth.contracts.graph import ExecutableUnitNode, Graph, Node, OperationIdentity
from zeroth.runtime.agents.tooling.tool_calls import (
    SYNTHETIC_CALL_ID_PREFIX,
    canonical_json,
)
from zeroth.runtime.orchestration.errors import NodeDispatcherError


def _key_component(value: str) -> str:
    """Escape one part of the key material so the joined string cannot be re-cut.

    The operation key is derived from ``#``-joined parts, and two of them are
    authored elsewhere: a graph's ``manifest_ref`` and a provider's tool-call
    id. Raw concatenation let either move the boundary. A ref reading
    ``unit://x#branch:B`` dispatched *outside* a fan-out produced the very
    string ``unit://x`` produces inside branch ``B`` (``NodeDispatcher`` appends
    ``#branch:<id>`` to this result), so two unrelated operations shared one
    durable record -- and the same trick re-cut the ref/call-id boundary.

    Escaping ``%`` before ``#`` keeps the mapping injective. A component
    containing neither character -- every ordinary ref, id and digest -- is
    returned unchanged, so this closes the alias without re-keying a single
    in-flight operation.
    """
    return value.replace("%", "%25").replace("#", "%23")


def _joined_ref(*parts: str) -> str:
    """Join key-material parts under an unambiguous separator."""
    return "#".join(_key_component(part) for part in parts)


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


class OperationAwareToolOutput(dict[str, Any]):
    """Tool output carrying the durable operation facts for agent-call audit."""

    def __init__(self, output: Mapping[str, Any], operation_audit: Mapping[str, Any]) -> None:
        super().__init__(output)
        self.operation_audit = dict(operation_audit)


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
        timeout_seconds: int | None = None,
        operation_identity: OperationIdentity | None = None,
    ) -> Any:
        """Call executable-unit runners with the node's bounded timeout enforced.

        Registered manifests carry their own default timeout, but Studio nodes
        may intentionally choose a tighter deadline.  The runner already
        resolves ``timeout_override_seconds`` as the minimum of manifest and
        policy bounds, so the node deadline joins that same fail-closed path
        instead of being silently ignored for manifest-backed units.
        """
        context = dict(enforcement_context)
        if timeout_seconds is not None:
            policy_timeout = context.get("timeout_override_seconds")
            context["timeout_override_seconds"] = (
                min(timeout_seconds, policy_timeout)
                if isinstance(policy_timeout, (int, float)) and policy_timeout > 0
                else timeout_seconds
            )
        parameters = inspect.signature(self.executable_unit_runner.run).parameters
        optional = _supported_kwargs(
            parameters,
            enforcement_context=context,
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
        operation_guard: Callable[
            [OperationIdentity, Callable[[], Awaitable[Any]]],
            Awaitable[tuple[Any, dict[str, Any]]],
        ]
        | None = None,
        side_effect_free: Callable[[ExecutableUnitNode], bool] | None = None,
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

        async def execute(
            binding: Any,
            arguments: Mapping[str, Any] | None,
            tool_call_id: str | None = None,
        ) -> Any:
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
            # Prefer the provider's own tool-call id over the positional
            # counter. The counter is process-local and restarts whenever the
            # executor is rebuilt, so after recovery a *different* first call to
            # the same target inherited the previous call's key and could be
            # suppressed as a duplicate. Wrongly suppressing real work is worse
            # than missing a suppression, so distinctness wins here.
            #
            # When an id exists it distinguishes calls together with the
            # ARGUMENT digest: mixing the counter back in re-broke recovery
            # (the same call replayed at a different position minted a
            # different key), while the id alone trusted providers never to
            # reuse one — a reused id on a different call would inherit the
            # earlier call's key and be wrongly suppressed. Identical
            # arguments under a reused id are indistinguishable from a retry,
            # which is exactly when suppression is correct. The counter is key
            # material only for executors that pass no id, and it is not
            # consumed otherwise, so mixed streams stay stable.
            #
            # A SYNTHETIC id is not such an id. When the provider names no call,
            # the extraction site mints a name from the call's own content --
            # which is stable under replay, and therefore identical for the
            # agent's second turn requesting the same tool with the same
            # arguments. Trusting it here read that second, real request as a
            # replay of the first: the guard returned the first call's stored
            # output and the effect never ran. A content name cannot carry
            # occurrence, so it is only pairing material, and the discriminator
            # falls back to the counter -- which is exactly the right scope,
            # since ``build`` runs once per node dispatch and a replayed
            # dispatch re-runs the agent's turns from the first one.
            provider_issued = bool(tool_call_id) and not tool_call_id.startswith(
                SYNTHETIC_CALL_ID_PREFIX
            )
            if provider_issued:
                args_digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()[:16]
                keyed_ref = _joined_ref(target_ref, str(tool_call_id), args_digest)
                ordinal = 0
            else:
                keyed_ref = _joined_ref(target_ref)
                ordinal = next(call_ordinal)
            identity = (
                None
                if operation_identity_factory is None
                else operation_identity_factory(keyed_ref, ordinal)
            )

            async def invoke() -> Any:
                if inline:
                    return await self.run_inline(
                        target,
                        payload,
                        enforcement_context=context,
                        operation_identity=identity,
                    )
                return await self.run_unit(
                    target.executable_unit.manifest_ref,
                    payload,
                    enforcement_context=context,
                    timeout_seconds=target.executable_unit.timeout_seconds,
                    operation_identity=identity,
                )

            guarded = (
                identity is not None
                and operation_guard is not None
                and not (side_effect_free is not None and side_effect_free(target))
            )
            if not guarded:
                return (await invoke()).output_data

            result, operation_audit = await operation_guard(identity, invoke)
            if result is None:
                output = operation_audit.pop("replayed_output", {})
            else:
                output = result.output_data
            return OperationAwareToolOutput(output, operation_audit)

        return execute
