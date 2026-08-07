"""Node dispatch for the orchestration runtime.

:class:`NodeDispatcher` resolves a node's type and runs it. Agent nodes are the
involved case: before the runner is called the dispatcher may override its
instruction with a rendered template, wrap its provider for cost
instrumentation and cost cascading, inject shared memory/budget services,
attach a context-window tracker, and attach a tool executor — then restore every
one of those in a ``finally``, because a runner that cannot fork is shared
across dispatches and must not leak state from one node into the next.

Executable-unit and retrieval nodes take their own paths; entrypoint nodes pass
their payload through.

All dependencies arrive explicitly. The dispatcher holds no run state, so the
same instance serves the sequential drive loop and every fan-out branch.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import (
    AgentNode,
    EntrypointNode,
    ExecutableUnitNode,
    Graph,
    Node,
    OperationIdentity,
    RetrievalNode,
    SubgraphNode,
    operation_identity,
)
from zeroth.platform.observability import start_span
from zeroth.runtime.agents import AgentRunner
from zeroth.runtime.orchestration.audit_recorder import enforcement_audit_fields
from zeroth.runtime.orchestration.errors import (
    MemoryBindingResolutionError,
    NodeDispatcherError,
)
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor
from zeroth.runtime.runs import Run
from zeroth.runtime.subgraphs.resolver import base_node_id

logger = logging.getLogger(__name__)

# Sentinel for "attribute not present" in optional runner wiring.
_MISSING: Any = object()

# Regex for {namespace.field} placeholders supported in binding key / key_prefix.
_KEY_PLACEHOLDER_RE = re.compile(r"\{(input|state|run)\.([^}]+)\}")


@dataclass(frozen=True, slots=True)
class SubgraphDispatchResult:
    """Normalized result of dispatching or resuming one child graph."""

    output: dict[str, Any] | None = None
    audit: dict[str, Any] | None = None
    terminal_run: Run | None = None


async def dispatch_subgraph_node(
    *,
    executor: Any,
    orchestrator: Any,
    parent_graph: Graph,
    parent_run: Run,
    node: SubgraphNode,
    input_payload: dict[str, Any],
) -> SubgraphDispatchResult:
    """Route a subgraph through its runtime executor with durable pause replay."""
    if executor is None or orchestrator is None:
        raise NodeDispatcherError("SubgraphExecutor is required for SubgraphNode dispatch")
    pending = parent_run.metadata.get("pending_subgraph")
    resumed = bool(pending and pending.get("node_id") == node.node_id)
    if resumed:
        child = await executor.resume(
            orchestrator=orchestrator,
            parent_graph=parent_graph,
            parent_run=parent_run,
            paused_child_run_id=pending["child_run_id"],
        )
    else:
        child = await executor.execute(
            orchestrator=orchestrator,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id=node.node_id,
            input_payload=input_payload,
        )
    if child.status is RunStatus.WAITING_APPROVAL:
        parent_run.status = RunStatus.WAITING_APPROVAL
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": child.run_id,
            "node_id": node.node_id,
            "graph_ref": node.subgraph.graph_ref,
            "version": node.subgraph.version,
        }
        parent_run.touch()
        persisted = await orchestrator.run_repository.put(parent_run)
        await orchestrator.run_repository.write_checkpoint(persisted)
        return SubgraphDispatchResult(terminal_run=persisted)
    if child.status is not RunStatus.COMPLETED:
        failure = child.failure_state
        detail = failure.message if failure is not None else "unknown failure"
        raise NodeDispatcherError(f"child run {child.run_id} ended {child.status.value}: {detail}")
    parent_run.metadata.pop("pending_subgraph", None)
    output = child.final_output or {}
    if not isinstance(output, dict):
        output = {"result": output}
    return SubgraphDispatchResult(
        output=output,
        audit={
            "subgraph_run_id": child.run_id,
            "subgraph_graph_ref": node.subgraph.graph_ref,
            "subgraph_status": child.status.value,
            "subgraph_resumed": resumed,
        },
    )


def substitute_binding_key(
    key: str,
    *,
    input_payload: dict[str, Any],
    state: dict[str, Any],
    run_id: str,
) -> str:
    """Replace ``{input.field}``, ``{state.field}``, ``{run.run_id}`` placeholders in a key.

    Unknown placeholders are left unchanged so callers can detect them.
    """
    sources: dict[str, dict[str, Any]] = {
        "input": input_payload,
        "state": state,
        "run": {"run_id": run_id},
    }

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        namespace, field_name = m.group(1), m.group(2)
        ns = sources.get(namespace, {})
        return str(ns[field_name]) if field_name in ns else m.group(0)

    return _KEY_PLACEHOLDER_RE.sub(_replace, key)


@dataclass(frozen=True, slots=True)
class NodeDispatcher:
    """Resolves node types and runs them with their governed wiring applied."""

    agent_runners: Mapping[str, AgentRunner]
    executable_unit_runner: Any
    tool_executor: RuntimeToolExecutor
    policy_gate: RuntimePolicyGate | None = None
    thread_resolver: Any = None
    secret_resolver: Any = None
    memory_resolver: Any = None
    budget_enforcer: Any = None
    regulus_client: Any = None
    cost_estimator: Any = None
    deployment_ref: str | None = None
    template_registry: Any = None
    template_renderer: Any = None
    context_window_enabled: bool = True

    def _enforcement_context_for(self, run: Run, node_id: str) -> dict[str, Any]:
        if self.policy_gate is None:
            return {}
        return self.policy_gate.enforcement_context_for(run, node_id)

    def _operation_identity_for(
        self,
        run: Run,
        target_ref: str,
        *,
        call_ordinal: int = 0,
    ) -> OperationIdentity:
        """Derive the logical-operation identity for one side-effecting call.

        The dispatcher is the one place that legitimately reads run state, so it
        resolves the identity here and hands it on explicitly. Everything below
        this line receives it as a parameter and never reaches back into
        ``run.metadata`` -- that indirection is what made the identity invisible
        to integrations before.

        Runs driven outside the token engine carry no dispatch record; they fall
        back to the run id, which still yields one stable key per (run, target,
        call) triple.
        """
        dispatch = run.metadata.get("token_dispatch")
        if not isinstance(dispatch, Mapping):
            dispatch = {}
        return operation_identity(
            run_id=run.run_id,
            dispatch_id=str(dispatch.get("dispatch_id") or run.run_id),
            idempotency_key=str(dispatch.get("idempotency_key") or run.run_id),
            attempt=int(dispatch.get("attempt") or 0),
            target_ref=target_ref,
            call_ordinal=call_ordinal,
        )

    def _effective_capabilities_for(self, run: Run, node_id: str) -> Any:
        if self.policy_gate is None:
            return None
        return self.policy_gate.effective_capabilities_for(run, node_id)

    async def dispatch(
        self,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Dispatch a node inside an OBS tracing span.

        Wraps every dispatch path (main drive loop and fan-out branches, which
        call this directly) so each node hop produces one span carrying the
        node/run identifiers that also key the metrics and audit records.
        ``graph`` enables tool-attachment dispatch for agents with tool
        bindings; callers without it simply run the agent tool-less.
        """
        with start_span(
            "zeroth.node",
            {
                "zeroth.node_id": node.node_id,
                "zeroth.node_type": type(node).__name__,
                "zeroth.run_id": run.run_id,
            },
        ):
            return await self.dispatch_inner(node, run, input_payload, graph)

    async def dispatch_inner(
        self,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run a single node and return its output and audit data.

        Figures out what kind of node it is (agent or executable unit),
        finds the right runner, and executes it. Raises NodeDispatcherError
        if the node type isn't supported or no runner is registered.
        """
        if isinstance(node, AgentNode):
            return await self._dispatch_agent(node, run, input_payload, graph)
        if isinstance(node, EntrypointNode):
            # Ingress pass-through: POST /v1/runs already validated the payload
            # against the deployment's pinned entry contract. The entrypoint
            # marks where (and with what) the run entered the workflow.
            return dict(input_payload), {
                "execution_mode": "entrypoint",
                "passthrough": True,
            }
        if isinstance(node, ExecutableUnitNode):
            return await self._dispatch_executable_unit(node, run, input_payload)
        if isinstance(node, RetrievalNode):
            return await self.dispatch_retrieval(node, run, input_payload)
        raise NodeDispatcherError(f"unsupported node type: {type(node)!r}")

    async def _dispatch_agent(
        self,
        node: AgentNode,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # Child-workflow node ids arrive namespaced (branch:N:subgraph:...);
        # runners are registered under the authored id, so fall back to it.
        prototype = self.agent_runners.get(node.node_id) or self.agent_runners.get(
            base_node_id(node.node_id)
        )
        if prototype is None:
            raise NodeDispatcherError(f"no agent runner registered for {node.node_id}")
        declared_fork = inspect.getattr_static(prototype, "fork_for_dispatch", _MISSING)
        fork_for_dispatch = prototype.fork_for_dispatch if declared_fork is not _MISSING else None
        runner = fork_for_dispatch() if callable(fork_for_dispatch) else prototype

        # Resolve thread before template rendering so memory can use thread scope.
        thread_id = await self.resolve_thread(node, run)
        tmb_audit_records: list[dict[str, Any]] = []

        # Phase 36: Template resolution -- resolve and render before agent execution.
        effective_instruction: str | None = None
        rendered_prompt_for_audit: str | None = None
        template_ref_for_audit: dict[str, Any] | None = None
        agent_template_ref = getattr(node.agent, "template_ref", None)
        if (
            self.template_registry is not None
            and self.template_renderer is not None
            and agent_template_ref is not None
        ):
            from zeroth.contracts.templates import TemplateRegistry, TemplateRenderer

            template_ref = node.agent.template_ref
            registry: TemplateRegistry = self.template_registry
            renderer: TemplateRenderer = self.template_renderer
            template = registry.get(template_ref.name, template_ref.version)
            # Resolve template memory bindings before building render_vars.
            _memory_ns, _tmb_records = await self.resolve_template_memory(
                node, run, thread_id, input_payload
            )
            tmb_audit_records.extend(_tmb_records)
            render_vars: dict[str, Any] = {
                "input": dict(input_payload),
                "state": dict(run.metadata) if run.metadata else {},
                "memory": _memory_ns,
            }
            render_result = renderer.render(template, render_vars)
            effective_instruction = render_result.rendered
            rendered_prompt_for_audit = render_result.rendered

            # Phase 36: Redact secret variable values before audit storage.
            from zeroth.contracts.templates.redaction import (
                identify_secret_variables,
                redact_rendered_prompt,
            )

            # Flatten nested render_vars for redaction matching.
            render_vars_flat: dict[str, object] = {}
            for _ns, _vals in render_vars.items():
                if isinstance(_vals, dict):
                    for k, v in _vals.items():
                        render_vars_flat[k] = v
            secret_vars = identify_secret_variables(
                list(render_vars_flat.keys()),
            )
            if secret_vars:
                rendered_prompt_for_audit = redact_rendered_prompt(
                    render_result.rendered,
                    render_vars_flat,
                    secret_vars,
                )

            template_ref_for_audit = {
                "name": template.name,
                "version": template.version,
            }

        # Capture every dispatch-mutable surface before the first assignment.
        # Production runners are forks, while lightweight protocol-less test
        # doubles use the prototype and therefore require exception-safe cleanup.
        original_config = getattr(runner, "config", _MISSING)
        original_provider = getattr(runner, "provider", _MISSING)
        original_memory_resolver = getattr(runner, "memory_resolver", _MISSING)
        original_budget_enforcer = getattr(runner, "budget_enforcer", _MISSING)
        original_context_tracker = getattr(runner, "context_tracker", _MISSING)
        original_tool_executor = getattr(runner, "tool_executor", _MISSING)
        _context_window_audit = None
        try:
            # Phase 36: Override runner config instruction with rendered template.
            if effective_instruction is not None and original_config is not _MISSING:
                runner.config = original_config.model_copy(
                    update={"instruction": effective_instruction}
                )

            # Phase 18: Wrap provider with cost instrumentation (per ECON-01).
            # Use getattr so lightweight runners without .provider still work.
            if original_provider is not _MISSING and self.cost_estimator is not None:
                try:
                    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

                    tenant_id = run.tenant_id or "default"
                    runner.provider = InstrumentedProviderAdapter(
                        inner=original_provider,
                        regulus_client=self.regulus_client,
                        cost_estimator=self.cost_estimator,
                        node_id=node.node_id,
                        run_id=run.run_id,
                        tenant_id=tenant_id,
                        deployment_ref=self.deployment_ref or "unknown",
                    )
                except ImportError:
                    pass

            # Cost cascade wraps the instrumented provider so each attempt is priced.
            agent_data = getattr(node, "agent", None)
            if (
                original_provider is not _MISSING
                and agent_data is not None
                and getattr(agent_data, "cascade_enabled", False)
                and getattr(agent_data, "cheap_model", None)
                and getattr(agent_data, "criticality", "medium") == "low"
            ):
                from zeroth.runtime.agents.cascade import CascadingProviderAdapter

                runner.provider = CascadingProviderAdapter(
                    inner=runner.provider,
                    cheap_model=agent_data.cheap_model,
                )

            # Phase 20: Add shared services only when the runner has none configured.
            if (
                self.memory_resolver is not None
                and original_memory_resolver is not _MISSING
                and original_memory_resolver is None
            ):
                runner.memory_resolver = self.memory_resolver
            if (
                self.budget_enforcer is not None
                and original_budget_enforcer is not _MISSING
                and original_budget_enforcer is None
            ):
                runner.budget_enforcer = self.budget_enforcer

            # Phase 37: Context window tracker injection (per D-09, D-11).
            if (
                self.context_window_enabled
                and original_context_tracker is not _MISSING
                and original_context_tracker is None
                and hasattr(node.agent, "context_window")
                and node.agent.context_window is not None
            ):
                from zeroth.runtime.context import (
                    ContextWindowTracker,
                    LLMSummarizationStrategy,
                    ObservationMaskingStrategy,
                    TruncationStrategy,
                )

                cw_settings = node.agent.context_window
                strategy_name = cw_settings.compaction_strategy
                if strategy_name == "truncation":
                    strategy = TruncationStrategy()
                elif strategy_name == "llm_summarization":
                    strategy = LLMSummarizationStrategy(provider=runner.provider)
                else:
                    strategy = ObservationMaskingStrategy()
                runner.context_tracker = ContextWindowTracker(
                    settings=cw_settings,
                    strategy=strategy,
                )

            enforcement_context = self._enforcement_context_for(run, node.node_id)
            if (
                graph is not None
                and original_tool_executor is not _MISSING
                and original_tool_executor is None
                and getattr(node.agent, "tool_bindings", None)
            ):
                runner.tool_executor = self.tool_executor.build(
                    graph,
                    enforcement_context,
                    operation_identity_factory=lambda target_ref, ordinal: (
                        self._operation_identity_for(run, target_ref, call_ordinal=ordinal)
                    ),
                )

            # Budget and capability enforcement are dispatch- and tenant-local.
            runner_context = dict(enforcement_context)
            runner_context.setdefault("tenant_id", run.tenant_id)
            runner_context["capability_enforcement_active"] = (
                self.policy_gate is not None and self.policy_gate.policy_guard is not None
            )
            result = await self.run_agent_with_optional_enforcement(
                runner,
                input_payload,
                thread_id=thread_id,
                runtime_context={
                    "node_id": node.node_id,
                    "run_id": run.run_id,
                    # WS-B: memory resolution is fail-closed on tenant; the
                    # runner forwards this dict unchanged to _load/_store.
                    "tenant_id": run.tenant_id,
                },
                enforcement_context=runner_context,
            )
        finally:
            # Phase 37: Record context window state in audit before restoring.
            _ctx_tracker = getattr(runner, "context_tracker", None)
            if _ctx_tracker is not None and hasattr(_ctx_tracker, "state"):
                _cw_state = _ctx_tracker.state
                # Store for audit enrichment after the finally block.
                _context_window_audit = {
                    "accumulated_tokens": _cw_state.accumulated_tokens,
                    "compaction_count": _cw_state.compaction_count,
                }
            else:
                _context_window_audit = None
            # Restore originals even when setup failed before agent execution.
            if original_config is not _MISSING:
                runner.config = original_config
            if original_provider is not _MISSING:
                runner.provider = original_provider
            if original_memory_resolver is not _MISSING:
                runner.memory_resolver = original_memory_resolver
            if original_budget_enforcer is not _MISSING:
                runner.budget_enforcer = original_budget_enforcer
            # Phase 37: Restore original context tracker.
            if original_context_tracker is not _MISSING:
                runner.context_tracker = original_context_tracker
            if original_tool_executor is not _MISSING:
                runner.tool_executor = original_tool_executor

        audit_record = dict(result.audit_record)
        if enforcement_context:
            # The nested context is kept for the content-capture posture; the
            # flattened fields are what survive the metadata-only default.
            audit_record["enforcement"] = enforcement_context
            audit_record.update(enforcement_audit_fields(enforcement_context, applied=True))
        # Phase 36: Record template metadata in audit.
        if rendered_prompt_for_audit is not None:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["rendered_prompt"] = rendered_prompt_for_audit
        if template_ref_for_audit is not None:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["template_ref"] = template_ref_for_audit
        # Phase 37: Record context window state in audit.
        if _context_window_audit is not None:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["context_window"] = _context_window_audit
        # Record template memory binding resolution in audit.
        if tmb_audit_records:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["template_memory_bindings"] = tmb_audit_records
        return result.output_data, audit_record

    async def _dispatch_executable_unit(
        self,
        node: ExecutableUnitNode,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        enforcement_context = self._enforcement_context_for(run, node.node_id)
        if (
            self.secret_resolver is not None
            and getattr(
                self.executable_unit_runner,
                "secret_resolver",
                None,
            )
            is None
        ):
            self.executable_unit_runner.secret_resolver = self.secret_resolver
        if node.executable_unit.inline_source is not None:
            result = await self.tool_executor.run_inline(
                node,
                input_payload,
                enforcement_context=enforcement_context,
                operation_identity=self._operation_identity_for(run, f"node://{node.node_id}"),
            )
        else:
            result = await self.tool_executor.run_unit(
                node.executable_unit.manifest_ref,
                input_payload,
                enforcement_context=enforcement_context,
                operation_identity=self._operation_identity_for(
                    run, node.executable_unit.manifest_ref
                ),
            )
        audit_record = dict(result.audit_record)
        if enforcement_context:
            # The nested context is kept for the content-capture posture; the
            # flattened fields are what survive the metadata-only default.
            audit_record["enforcement"] = enforcement_context
            audit_record.update(enforcement_audit_fields(enforcement_context, applied=True))
        return result.output_data, audit_record

    async def dispatch_retrieval(
        self,
        node: RetrievalNode,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Retrieve grounded context from a vector connector for a RetrievalNode (RAG-01).

        Queries the configured memory connector with the input's query field and
        returns the input augmented with the retrieved chunks under ``as_name``.
        The audit record carries the query and per-chunk source attribution
        (ids + metadata), not the chunk bodies (RAG-03).
        """
        data = node.retrieval
        if self.memory_resolver is None:
            raise NodeDispatcherError(
                f"retrieval node '{node.node_id}' requires a memory resolver to be wired"
            )
        query_text = input_payload.get(data.query_key)
        if not isinstance(query_text, str) or not query_text.strip():
            raise NodeDispatcherError(
                f"retrieval node '{node.node_id}': input field '{data.query_key}' "
                "must be a non-empty string"
            )
        from zeroth.contracts.governed import MemoryScope

        scope = {
            "run": MemoryScope.RUN,
            "thread": MemoryScope.THREAD,
            "shared": MemoryScope.SHARED,
        }[data.scope]
        try:
            resolved = await self.memory_resolver.resolve(
                [data.connector_ref],
                thread_id=run.thread_id or None,
                runtime_context={
                    "run_id": run.run_id,
                    "node_id": node.node_id,
                    "tenant_id": run.tenant_id,  # WS-B: fail-closed tenant scoping
                },
                node_id=node.node_id,
                # WS-C: retrieval reads memory -> gated on MEMORY_READ.
                effective_capabilities=self._effective_capabilities_for(run, node.node_id),
            )
        except KeyError as exc:
            raise NodeDispatcherError(
                f"retrieval node '{node.node_id}': unknown memory connector '{data.connector_ref}'"
            ) from exc
        connector = resolved[0].connector
        entries = await connector.search({"text": query_text, "limit": data.top_k}, scope)
        chunks = [
            {"id": entry.key, "content": entry.value, "metadata": dict(entry.metadata)}
            for entry in entries
        ]
        output_data = {**dict(input_payload), data.as_name: chunks}
        audit_record = {
            "retrieval": {
                "connector_ref": data.connector_ref,
                "query": query_text,
                "scope": data.scope,
                "top_k": data.top_k,
                "result_count": len(chunks),
                "sources": [
                    {"id": entry.key, "metadata": dict(entry.metadata)} for entry in entries
                ],
            }
        }
        return output_data, audit_record

    async def resolve_thread(self, node: AgentNode, run: Run) -> str | None:
        """Figure out which thread ID an agent node should use.

        Some agents participate in threads (conversations), others don't.
        This checks the agent's configuration and uses the thread resolver
        to find or create the right thread.
        """
        mode = node.agent.thread_participation
        persistence_mode = node.agent.state_persistence.get("mode")
        # Persistent conversations live in thread state, so opting in counts
        # as thread participation even when the mode was left at "none".
        persists_conversation = getattr(node.agent, "persist_conversation", False)
        if mode == "none" and persistence_mode != "thread" and not persists_conversation:
            return None
        if self.thread_resolver is not None:
            resolution = await self.thread_resolver.resolve(
                run.thread_id,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                participating_agent_refs=[node.node_id],
                run_id=run.run_id,
            )
            run.thread_id = resolution.thread.thread_id
        return run.thread_id

    async def resolve_template_memory(
        self,
        node: AgentNode,
        run: Run,
        thread_id: str | None,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch memory values declared in ``template_memory_bindings``.

        Returns ``(memory_namespace, audit_records)`` where *memory_namespace*
        is the dict that populates ``{{ memory.* }}`` in prompt templates and
        *audit_records* is a list of per-binding audit dicts appended to the
        node audit record under ``execution_metadata.template_memory_bindings``.

        Raises ``MemoryBindingResolutionError`` when a connector is unknown or
        when a read/search call fails.
        """
        bindings = node.agent.template_memory_bindings
        if not bindings or self.memory_resolver is None:
            return {}, []

        from zeroth.contracts.governed import MemoryScope

        _scope_map = {
            "run": MemoryScope.RUN,
            "thread": MemoryScope.THREAD,
            "shared": MemoryScope.SHARED,
        }

        # Only resolve the connector refs actually used by template bindings.
        refs_needed = list({b.connector_instance_id for b in bindings})
        runtime_context: dict[str, Any] = {
            "node_id": node.node_id,
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,  # WS-B: fail-closed tenant scoping
        }
        try:
            resolved = await self.memory_resolver.resolve(
                refs_needed,
                thread_id=thread_id,
                runtime_context=runtime_context,
                node_id=node.node_id,
                # WS-C: template memory bindings read memory -> gated on MEMORY_READ.
                effective_capabilities=self._effective_capabilities_for(run, node.node_id),
            )
        except KeyError as exc:
            raise MemoryBindingResolutionError(
                f"unknown memory connector referenced in template_memory_bindings: {exc}"
            ) from exc

        connector_by_ref: dict[str, Any] = {rb.memory_ref: rb.connector for rb in resolved}
        state: dict[str, Any] = dict(run.metadata) if run.metadata else {}
        input_dict: dict[str, Any] = dict(input_payload)

        memory_ns: dict[str, Any] = {}
        audit_records: list[dict[str, Any]] = []

        for binding in bindings:
            connector = connector_by_ref.get(binding.connector_instance_id)
            if connector is None:
                raise MemoryBindingResolutionError(
                    f"connector '{binding.connector_instance_id}' not found in memory_refs; "
                    "add it to agent.memory_refs before using it in template_memory_bindings"
                )

            scope = _scope_map[binding.scope]

            try:
                if binding.access_mode == "get":
                    resolved_key = substitute_binding_key(
                        binding.key or "",
                        input_payload=input_dict,
                        state=state,
                        run_id=run.run_id,
                    )
                    entry = await connector.read(resolved_key, scope)
                    value = entry.value if entry is not None else binding.default
                    audit_records.append(
                        {
                            "as_name": binding.as_name,
                            "connector_instance_id": binding.connector_instance_id,
                            "access_mode": "get",
                            "key": resolved_key,
                            "scope": binding.scope,
                            "found": entry is not None,
                        }
                    )
                else:  # scan
                    prefix = binding.key_prefix or ""
                    if prefix:
                        prefix = substitute_binding_key(
                            prefix,
                            input_payload=input_dict,
                            state=state,
                            run_id=run.run_id,
                        )
                    all_entries = await connector.search({}, scope)
                    items: dict[str, Any] = {
                        entry.key[len(prefix) :]: entry.value
                        for entry in all_entries
                        if entry.key.startswith(prefix)
                    }
                    if binding.max_items is not None:
                        items = dict(list(items.items())[: binding.max_items])
                    value = items if items else binding.default
                    audit_records.append(
                        {
                            "as_name": binding.as_name,
                            "connector_instance_id": binding.connector_instance_id,
                            "access_mode": "scan",
                            "key_prefix": prefix,
                            "scope": binding.scope,
                            "item_count": len(items),
                        }
                    )
            except MemoryBindingResolutionError:
                raise
            except Exception as exc:
                raise MemoryBindingResolutionError(
                    f"failed to read memory binding '{binding.as_name}' "
                    f"(connector={binding.connector_instance_id}): {exc}"
                ) from exc

            memory_ns[binding.as_name] = value

        return memory_ns, audit_records

    async def run_agent_with_optional_enforcement(
        self,
        runner: AgentRunner,
        input_payload: Mapping[str, Any],
        *,
        thread_id: str | None,
        runtime_context: Mapping[str, Any],
        enforcement_context: Mapping[str, Any],
    ) -> Any:
        """Call agent runners with enforcement context when their signature supports it."""
        parameters = inspect.signature(runner.run).parameters
        if "enforcement_context" in parameters:
            return await runner.run(
                input_payload,
                thread_id=thread_id,
                runtime_context=runtime_context,
                enforcement_context=enforcement_context,
            )
        return await runner.run(
            input_payload,
            thread_id=thread_id,
            runtime_context=runtime_context,
        )
