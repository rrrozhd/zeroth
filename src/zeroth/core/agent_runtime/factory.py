"""Build agent runners from graph definitions.

Turns each ``AgentNode`` in a graph into a ready-to-dispatch
:class:`~zeroth.core.agent_runtime.runner.AgentRunner` by resolving the
node's contract refs against the :class:`ContractRegistry` and reading the
model configuration (``model_provider``, ``model_params``, timeouts,
memory refs) straight from :class:`AgentNodeData`.

This is what lets a graph authored declaratively (studio canvas, JSON,
or plain Python without custom runner wiring) execute on the stock
service entrypoint: the graph itself carries enough information to
construct its runners.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.core.agent_runtime.models import AgentConfig, ModelParams, PromptConfig, RetryPolicy
from zeroth.core.agent_runtime.provider import LiteLLMProviderAdapter, ProviderAdapter
from zeroth.core.agent_runtime.runner import AgentRunner
from zeroth.core.agent_runtime.tools import ToolAttachmentManifest
from zeroth.core.contracts.registry import ContractReference, ContractRegistry
from zeroth.core.graph.models import AgentNode, Graph
from zeroth.core.graph.serialization import deserialize_graph

if TYPE_CHECKING:
    from zeroth.core.storage.database import AsyncDatabase


class AgentRunnerFactoryError(RuntimeError):
    """A runner could not be built from a graph's agent node."""


async def build_agent_runners(
    graph: Graph,
    contract_registry: ContractRegistry,
    *,
    provider: ProviderAdapter | None = None,
) -> dict[str, AgentRunner]:
    """Build one AgentRunner per agent node, keyed by node_id.

    Contract refs are resolved to their registered Pydantic classes; a
    missing or unresolvable contract raises AgentRunnerFactoryError naming
    the node so startup fails with an actionable message instead of a
    per-run dispatch error.
    """
    shared_provider = provider or LiteLLMProviderAdapter()
    runners: dict[str, AgentRunner] = {}
    for node in graph.nodes:
        if not isinstance(node, AgentNode):
            continue
        try:
            input_model = await contract_registry.resolve_model_type(
                ContractReference(name=node.input_contract_ref)
            )
            output_model = await contract_registry.resolve_model_type(
                ContractReference(name=node.output_contract_ref)
            )
        except Exception as exc:
            msg = (
                f"cannot build runner for agent node {node.node_id!r}: "
                f"contract resolution failed ({exc}). Register the node's "
                f"input/output contracts before serving this graph."
            )
            raise AgentRunnerFactoryError(msg) from exc

        data = node.agent
        if not data.model_provider:
            msg = f"cannot build runner for agent node {node.node_id!r}: model_provider is empty"
            raise AgentRunnerFactoryError(msg)

        # Tool-edge attachments: each binding becomes a declared tool manifest
        # whose alias/description/parameters the author wrote on the canvas.
        # The node:// ref routes the call back to the graph node at dispatch.
        tool_attachments = [
            ToolAttachmentManifest(
                alias=binding.name,
                executable_unit_ref=f"node://{binding.target_node_id}",
                description=binding.description,
                parameters_schema=binding.parameters_schema(),
            )
            for binding in data.tool_bindings
        ]

        prompt_config = PromptConfig()
        if data.input_messages_key:
            prompt_config = PromptConfig(
                messages_key=data.input_messages_key,
                persist_conversation=data.persist_conversation,
                conversation_max_turns=data.conversation_max_turns,
            )

        config = AgentConfig(
            name=node.node_id,
            description=node.display.title or "",
            instruction=data.instruction,
            model_name=data.model_provider,
            input_model=input_model,
            output_model=output_model,
            tool_attachments=tool_attachments,
            memory_refs=list(data.memory_refs),
            prompt_config=prompt_config,
            retry_policy=RetryPolicy(**data.retry_policy) if data.retry_policy else RetryPolicy(),
            timeout_seconds=float(data.timeout_seconds) if data.timeout_seconds else None,
            model_params=ModelParams(**data.model_params) if data.model_params else None,
        )
        runners[node.node_id] = AgentRunner(config, shared_provider)
    return runners


async def build_runners_for_deployment(
    database: AsyncDatabase,
    deployment_ref: str,
    *,
    provider: ProviderAdapter | None = None,
) -> dict[str, AgentRunner] | None:
    """Build runners for the graph behind a deployment ref.

    Returns None when the deployment does not exist — bootstrap_service
    raises its own, clearer error for that case.
    """
    from zeroth.core.deployments import SQLiteDeploymentRepository

    deployment = await SQLiteDeploymentRepository(database).get(deployment_ref)
    if deployment is None:
        return None
    graph = deserialize_graph(deployment.serialized_graph)
    registry = ContractRegistry(database)
    return await build_agent_runners(graph, registry, provider=provider)
