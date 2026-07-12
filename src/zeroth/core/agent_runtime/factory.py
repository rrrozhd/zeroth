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

from typing import TYPE_CHECKING, Any

from zeroth.core.agent_runtime.models import AgentConfig, ModelParams, PromptConfig, RetryPolicy
from zeroth.core.agent_runtime.provider import LiteLLMProviderAdapter, ProviderAdapter
from zeroth.core.agent_runtime.runner import AgentRunner
from zeroth.core.agent_runtime.tools import ToolAttachmentManifest
from zeroth.core.contracts.registry import ContractReference, ContractRegistry
from zeroth.core.graph.models import AgentNode, AgentToolBinding, ExecutableUnitNode, Graph, Node
from zeroth.core.graph.serialization import deserialize_graph
from zeroth.core.policy.models import Capability

if TYPE_CHECKING:
    from zeroth.core.secrets import SecretProvider
    from zeroth.core.storage.database import AsyncDatabase


class AgentRunnerFactoryError(RuntimeError):
    """A runner could not be built from a graph's agent node."""


def _capability_from_ref(ref: str) -> Capability | None:
    """Map a capability_binding ref to a Capability, or None if not a known value.

    The served ref scheme is the capability value itself (e.g. ``"memory_read"``).
    Non-value refs (bespoke ``capability://...`` schemes) return None; they cannot
    be granted by the default guard anyway, so they contribute nothing to a
    required set. Validation flags a real grant/require mismatch separately.
    """
    try:
        return Capability(ref)
    except ValueError:
        return None


def tool_required_capabilities(
    binding: AgentToolBinding,
    node_map: dict[str, Node],
) -> tuple[Capability, ...]:
    """Authoritative required-capability set for a tool attachment (WS-C).

    The set is the target executable-unit node's own declared
    ``capability_bindings`` (what the unit is authorized to do — network,
    secrets, process spawn, ...), UNIONED with any author-declared
    ``AgentToolBinding.required_capabilities``. The graph validator enforces the
    identical source, so author-time validation and runtime enforcement never
    diverge. ``mcp://`` tools have no graph node and are handled separately (at
    MCP discovery), so they never reach here.
    """
    caps: set[Capability] = set(binding.required_capabilities)
    target = node_map.get(binding.target_node_id)
    if isinstance(target, ExecutableUnitNode):
        for ref in target.capability_bindings:
            capability = _capability_from_ref(ref)
            if capability is not None:
                caps.add(capability)
    return tuple(sorted(caps, key=lambda cap: cap.value))


async def build_agent_runners(
    graph: Graph,
    contract_registry: ContractRegistry,
    *,
    provider: ProviderAdapter | None = None,
    secret_provider: SecretProvider | None = None,
    tenant_id: str | None = None,
    allow_env_fallback: bool = True,
    llm_key_map: dict[str, str] | None = None,
) -> dict[str, AgentRunner]:
    """Build one AgentRunner per agent node, keyed by node_id.

    Contract refs are resolved to their registered Pydantic classes; a
    missing or unresolvable contract raises AgentRunnerFactoryError naming
    the node so startup fails with an actionable message instead of a
    per-run dispatch error.

    When ``provider`` is not supplied, the default :class:`LiteLLMProviderAdapter`
    is constructed with the given ``secret_provider`` / ``tenant_id`` so LLM
    keys resolve through the secret backend instead of process env (WS-F).
    """
    shared_provider = provider or LiteLLMProviderAdapter(
        secret_provider=secret_provider,
        tenant_id=tenant_id,
        allow_env_fallback=allow_env_fallback,
        llm_key_map=llm_key_map,
    )
    runners: dict[str, AgentRunner] = {}
    node_map: dict[str, Node] = {node.node_id: node for node in graph.nodes}
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
                required_capabilities=tool_required_capabilities(binding, node_map),
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

        extra_config: dict[str, Any] = {}
        if data.max_tool_calls is not None:
            extra_config["max_tool_calls"] = data.max_tool_calls
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
            **extra_config,
        )
        runners[node.node_id] = AgentRunner(config, shared_provider)
    return runners


async def build_runners_for_deployment(
    database: AsyncDatabase,
    deployment_ref: str,
    *,
    provider: ProviderAdapter | None = None,
    secret_provider: SecretProvider | None = None,
    allow_env_fallback: bool = True,
    llm_key_map: dict[str, str] | None = None,
) -> dict[str, AgentRunner] | None:
    """Build runners for the graph behind a deployment ref.

    Returns None when the deployment does not exist — bootstrap_service
    raises its own, clearer error for that case.

    ``tenant_id`` for key resolution is taken from ``deployment.tenant_id`` —
    one value per deployment under the single-tenant model.
    """
    from zeroth.core.deployments import SQLiteDeploymentRepository

    deployment = await SQLiteDeploymentRepository(database).get(deployment_ref)
    if deployment is None:
        return None
    graph = deserialize_graph(deployment.serialized_graph)
    registry = ContractRegistry(database)
    return await build_agent_runners(
        graph,
        registry,
        provider=provider,
        secret_provider=secret_provider,
        tenant_id=deployment.tenant_id,
        allow_env_fallback=allow_env_fallback,
        llm_key_map=llm_key_map,
    )
