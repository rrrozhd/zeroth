"""Build agent runners from graph definitions.

Turns each ``AgentNode`` in a graph into a ready-to-dispatch
:class:`~zeroth.runtime.agents.runner.AgentRunner` by resolving the
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

from zeroth.contracts.graph.models import (
    AgentNode,
    AgentToolBinding,
    ExecutableUnitNode,
    Graph,
    MCPToolNode,
    Node,
)
from zeroth.contracts.registry import ContractReference, ContractRegistry
from zeroth.governance.policy.models import Capability
from zeroth.runtime.agents.factory_markers import MCP_AT_LEAST_ONCE
from zeroth.runtime.agents.models import (
    AgentConfig,
    ModelParams,
    PromptConfig,
    RetryPolicy,
    ThreadStateStore,
    ToolOutputSafetyConfig,
)
from zeroth.runtime.agents.provider import LiteLLMProviderAdapter, ProviderAdapter
from zeroth.runtime.agents.runner import AgentRunner
from zeroth.runtime.agents.sanitization import (
    ToolDeclarationSafetyError,
    screen_tool_declaration,
)
from zeroth.runtime.agents.tools import ToolAttachmentManifest

if TYPE_CHECKING:
    from zeroth.platform.secrets import SecretProvider


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
    ``AgentToolBinding.required_capabilities``.

    ``graph_validation.validate_tool_grants`` reads the identical source, and
    that identity is what stops a tool's requirement being *invisible* at
    publish. It is not a promise that a graph passing publish cannot be denied
    here: the two sides compare different subjects. Publish measures the agent's
    author-declared ``capability_bindings``; this set is measured against the
    effective set ``PolicyGuard`` yields, which a policy bound in the same graph
    can make smaller. "Never diverge" was the wording here, and it was an
    over-claim in exactly the direction that hides a run-time denial.

    An ``mcp_tool`` target contributes its own ``capability_bindings`` the same
    way. It used to be true that MCP tools had no graph node and were handled
    at discovery; now that they do, skipping them here would silently drop the
    per-tool capabilities the publish-time ceiling was checked against — the
    node would declare a capability, pass validation on it, and then be enforced
    as if it had declared nothing.
    """
    caps: set[Capability] = set(binding.required_capabilities)
    target = node_map.get(binding.target_node_id)
    if isinstance(target, ExecutableUnitNode | MCPToolNode):
        for ref in target.capability_bindings:
            capability = _capability_from_ref(ref)
            if capability is not None:
                caps.add(capability)
    return tuple(sorted(caps, key=lambda cap: cap.value))



def _tool_manifest(
    agent_node_id: str,
    binding: AgentToolBinding,
    node_map: dict[str, Node],
    safety: ToolOutputSafetyConfig,
) -> ToolAttachmentManifest:
    """Build the declared tool manifest one tool edge exposes to the model.

    Two kinds of target produce two different contracts.

    An ``ExecutableUnitNode`` target is described entirely by the author: the
    binding's ``arguments`` compile into the JSON Schema the model sees, and
    that is the whole contract.

    An ``MCPToolNode`` target is not. Its contract was pinned from the server at
    import, and ``AgentToolBinding.arguments`` is empty for it — an import has
    no ``ToolArgument`` list to write, and translating MCP JSON Schema into one
    would be a lossy copy of the very thing the pin exists to hold exact. So the
    pinned ``input_schema`` *is* the manifest's ``parameters_schema``. Compiling
    from the (empty) argument list instead handed a real model
    ``{"properties": {}, "additionalProperties": false}`` for a tool whose
    pinned schema demands real arguments — a contract that forbids the model
    from supplying anything, for a tool that cannot run without it. The pin was
    read only by validation and by the drift digest; nothing carried it to the
    model, which is the one place a pinned contract is *for*.

    That schema and the binding's description are external text: the server
    chose both, and ``mcp_import`` copies them verbatim so the graph stores what
    the server said. They land in the model's *instruction* surface on every
    step, so they go through the same screening the deprecated inline discovery
    path applies (see ``sanitization.screen_tool_declaration``). Without it this
    node kind — the surface that deprecates inline ``mcp_servers`` — was
    strictly less safe than the thing it replaces.

    The screened copy is for model exposure only: ``schema_hash`` is taken over
    the raw declaration on both sides of the pin, so a transform that reached
    the node would break drift detection for every published graph. Today
    ``wrap_schema_descriptions`` rebuilds the whole structure and never mutates
    its input, so ``dict(...)`` is defence in depth rather than the thing
    holding that property up -- it costs nothing and stops a future in-place
    transform from silently rewriting a pin.
    """
    target = node_map.get(binding.target_node_id)
    is_mcp = isinstance(target, MCPToolNode)
    manifest = ToolAttachmentManifest(
        alias=binding.name,
        executable_unit_ref=f"node://{binding.target_node_id}",
        description=binding.description,
        parameters_schema=(
            dict(target.mcp_tool.input_schema) if is_mcp else binding.parameters_schema()
        ),
        required_capabilities=tool_required_capabilities(binding, node_map),
        # Stamped here because this is the only layer that can see both
        # the binding and the node it targets. The runner receives
        # manifests, not graph nodes, so it cannot work out on its own
        # that a call bypasses the side-effect boundary -- and it used
        # to infer that from an ``mcp://`` ref that tool edges no longer
        # mint, leaving MCP calls audited as though the operation
        # guarantee applied to them.
        #
        # Stamped BEFORE screening, and screening carries ``metadata``
        # through: building the marker afterwards, or letting the transform
        # return a fresh metadata dict, drops it silently and every MCP call
        # goes back to being audited as though it carried a receipt.
        metadata={MCP_AT_LEAST_ONCE: True} if is_mcp else {},
    )
    if not is_mcp:
        return manifest
    try:
        return screen_tool_declaration(manifest, safety)
    except ToolDeclarationSafetyError as exc:
        # The inline discovery path logs and skips an unboundable declaration,
        # because there the tool set is whatever a server happened to advertise
        # this morning and the graph never named it. Here the graph does name
        # it: the tool is a published node with an edge to this agent, so
        # dropping it would serve an agent quietly missing a capability its
        # own definition declares. Import screens the same declaration and
        # refuses (``mcp_import``), so reaching this is already a graph that
        # bypassed that check -- fail where a message can be read.
        raise AgentRunnerFactoryError(
            f"cannot build runner for agent node {agent_node_id!r}: tool "
            f"{binding.name!r} targets MCP tool node {binding.target_node_id!r}, "
            f"whose pinned declaration cannot be bounded safely for model "
            f"exposure ({exc})"
        ) from exc


def _author_mcp_servers(node_id: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse an operator-only ceiling written into an author-controlled entry.

    ``AgentNodeData.mcp_servers`` is a raw dict the graph author controls, and it
    is coerced straight into ``MCPServerConfig``. That class deliberately does
    NOT declare ``grants`` -- the ceiling lives on ``RegisteredMCPServerConfig``,
    which only the registry resolver builds -- so ``extra="forbid"`` already
    rejects an author-written ``grants`` on its own.

    This check is kept anyway, for the error message rather than the outcome: the
    model's own failure names a field, while this one names the node and says
    where grants actually come from. It is also the guard that survives if some
    later change moves ``grants`` onto the base class, which is exactly the
    landmine worth keeping a tripwire on.
    """
    cleaned: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict) and "grants" in entry:
            raise AgentRunnerFactoryError(
                f"agent node {node_id!r}: mcp_servers entries must not declare "
                "'grants'; a server's granted capabilities are operator-owned "
                "and are registered through the MCP server registry"
            )
        cleaned.append(entry)
    return cleaned

async def build_agent_runners(
    graph: Graph,
    contract_registry: ContractRegistry,
    *,
    provider: ProviderAdapter | None = None,
    secret_provider: SecretProvider | None = None,
    tenant_id: str | None = None,
    allow_env_fallback: bool = True,
    llm_key_map: dict[str, str] | None = None,
    llm_base_url_map: dict[str, str] | None = None,
    thread_state_store: ThreadStateStore | None = None,
    allow_development_inline_mcp: bool = False,
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
        llm_base_url_map=llm_base_url_map,
    )
    runners: dict[str, AgentRunner] = {}
    node_map: dict[str, Node] = {node.node_id: node for node in graph.nodes}
    for node in graph.nodes:
        if not isinstance(node, AgentNode):
            continue
        try:
            input_model = await contract_registry.resolve_model_type(
                ContractReference.parse(node.input_contract_ref)
            )
            output_model = await contract_registry.resolve_model_type(
                ContractReference.parse(node.output_contract_ref)
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
        #
        # The same ``ToolOutputSafetyConfig`` object reaches the manifests (via
        # declaration screening) and the runner that will hold them, because the
        # two decisions are one: a deployment that turned injection screening off
        # must get it off at both boundaries, and nothing in ``AgentNodeData``
        # can answer the question separately.
        safety = ToolOutputSafetyConfig()
        tool_attachments = [
            _tool_manifest(node.node_id, binding, node_map, safety)
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
            tool_output_safety=safety,
            memory_refs=list(data.memory_refs),
            mcp_servers=_author_mcp_servers(node.node_id, data.mcp_servers),
            allow_development_inline_mcp=allow_development_inline_mcp,
            prompt_config=prompt_config,
            retry_policy=RetryPolicy(**data.retry_policy) if data.retry_policy else RetryPolicy(),
            timeout_seconds=float(data.timeout_seconds) if data.timeout_seconds else None,
            model_params=ModelParams(**data.model_params) if data.model_params else None,
            **extra_config,
        )
        runners[node.node_id] = AgentRunner(
            config,
            shared_provider,
            thread_state_store=thread_state_store,
        )
    return runners
