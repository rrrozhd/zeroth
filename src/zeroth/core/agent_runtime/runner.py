"""The main agent runner that ties everything together.

This module contains AgentRunner, which takes input data, builds a prompt,
calls the AI model, validates the output, handles tool calls, manages
retries, and saves thread state. It is the entry point for running an agent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from governai.integrations.tool_calls import build_tool_message
from governai.memory.models import MemoryScope
from pydantic import BaseModel, ValidationError

from zeroth.core.agent_runtime.errors import (
    AgentInputValidationError,
    AgentOutputValidationError,
    AgentProviderError,
    AgentRetryExhaustedError,
    AgentTimeoutError,
    BudgetExceededError,
)
from zeroth.core.agent_runtime.mcp import MCPClientManager
from zeroth.core.agent_runtime.models import (
    AgentConfig,
    AgentRunResult,
    InMemoryThreadStateStore,
    ThreadStateStore,
)
from zeroth.core.agent_runtime.prompt import AgentAuditSerializer, PromptAssembler
from zeroth.core.agent_runtime.provider import (
    ProviderAdapter,
    ProviderRequest,
    run_provider_with_timeout,
)
from zeroth.core.agent_runtime.retry import compute_backoff_delay, is_retryable_provider_error
from zeroth.core.agent_runtime.tools import ToolAttachmentBridge
from zeroth.core.agent_runtime.validation import OutputValidator
from zeroth.core.audit import MemoryAccessRecord
from zeroth.core.memory import MemoryConnectorResolver


class AgentRunner:
    """Runs an agent end-to-end: prompt assembly, model call, output validation.

    This is the main class you use to execute an agent. Give it a config
    and a provider, then call ``run()`` with your input data. It handles
    retries, tool calls, thread state, memory, and audit logging.
    """

    def __init__(
        self,
        config: AgentConfig,
        provider: ProviderAdapter,
        *,
        prompt_assembler: PromptAssembler | None = None,
        output_validator: OutputValidator | None = None,
        audit_serializer: AgentAuditSerializer | None = None,
        thread_state_store: ThreadStateStore | None = None,
        tool_bridge: ToolAttachmentBridge | None = None,
        tool_executor: Any | None = None,
        granted_tool_permissions: list[str] | None = None,
        memory_resolver: MemoryConnectorResolver | None = None,
        budget_enforcer: Any | None = None,
        context_tracker: Any | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.prompt_assembler = prompt_assembler or PromptAssembler()
        self.output_validator = output_validator or OutputValidator()
        self.audit_serializer = audit_serializer or AgentAuditSerializer(
            redact_keys=set(config.prompt_config.redact_keys)
        )
        self.thread_state_store = thread_state_store or InMemoryThreadStateStore()
        attachments = config.tool_attachments
        self.tool_bridge = tool_bridge or ToolAttachmentBridge.from_config(attachments)
        self.tool_executor = tool_executor
        self.granted_tool_permissions = granted_tool_permissions or []
        self.memory_resolver = memory_resolver
        self.budget_enforcer = budget_enforcer
        self.context_tracker: Any | None = context_tracker
        self._mcp_manager: MCPClientManager | None = None

    async def run(
        self,
        input_payload: BaseModel | Mapping[str, Any],
        *,
        thread_id: str | None = None,
        runtime_context: Mapping[str, Any] | None = None,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> AgentRunResult:
        """Execute the agent with the given input and return the result.

        Validates input, assembles the prompt, calls the provider (with
        retries if configured), resolves any tool calls, validates the
        output, saves thread state, and returns the full result with
        audit information.
        """
        validated_input = self._validate_input(input_payload)
        thread_state = await self._load_thread_state(thread_id)
        resolved_runtime_context = dict(runtime_context or {})
        memory_context, memory_interactions = await self._load_memory(
            thread_id=thread_id,
            runtime_context=resolved_runtime_context,
        )
        if memory_context:
            # Memory is added to the prompt context as normal input.
            resolved_runtime_context["memory"] = memory_context
        retry_policy = self.config.retry_policy
        max_attempts = retry_policy.max_attempts
        provider_timeout_seconds = self._effective_timeout(
            self.config.timeout_seconds,
            enforcement_context.get("timeout_override_seconds")
            if enforcement_context is not None
            else None,
        )
        approval_required_for_side_effects = bool(
            (enforcement_context or {}).get("approval_required_for_side_effects")
        )
        prompt = self.prompt_assembler.assemble(
            self.config,
            validated_input,
            thread_state=thread_state,
            runtime_context=resolved_runtime_context,
        )
        messages: list[Any] = list(prompt.messages)

        # Phase 37: Restore compacted messages from thread state if available.
        if thread_state is not None and "compacted_messages" in thread_state:
            messages = list(thread_state["compacted_messages"])

        # Phase 37: Context window compaction before first LLM invocation (per D-09).
        compaction_result: Any = None
        if self.context_tracker is not None:
            messages, compaction_result = await self.context_tracker.maybe_compact(
                messages,
                self.config.model_name,
            )

        # Pre-execution budget check (per D-10, ECON-03)
        if self.budget_enforcer is not None:
            _tenant_id = (
                enforcement_context.get("tenant_id", "default")
                if enforcement_context is not None
                else "default"
            )
            allowed, spend, cap = await self.budget_enforcer.check_budget(_tenant_id)
            if not allowed:
                raise BudgetExceededError(
                    f"tenant budget exceeded: spent ${spend:.4f} of ${cap:.4f} cap",
                    spend=spend,
                    cap=cap,
                )

        await self._start_mcp_servers()
        try:
            last_error: Exception | None = None
            attempts = 0
            for attempt in range(1, max_attempts + 1):
                attempts = attempt
                try:
                    # Each retry rebuilds the provider request from the current message history.
                    request = self._build_provider_request(messages, prompt.metadata)
                    response = await run_provider_with_timeout(
                        self.provider,
                        request,
                        timeout_seconds=provider_timeout_seconds,
                    )
                    response, messages, tool_audits = await self._resolve_tool_calls(
                        response=response,
                        messages=messages,
                        provider_timeout_seconds=provider_timeout_seconds,
                        approval_required_for_side_effects=approval_required_for_side_effects,
                    )
                    # Validation turns the provider response into the typed Zeroth output.
                    output = self.output_validator.validate(self.config.output_model, response)
                    record = self.audit_serializer.serialize_record(
                        prompt=prompt,
                        response=response,
                        extra={
                            "attempts": attempts,
                            "thread_id": thread_id,
                            "thread_state": thread_state,
                            "tool_calls": tool_audits,
                            "memory_interactions": [
                                item.model_dump(mode="json") for item in memory_interactions
                            ],
                        },
                    )
                    # Copy token usage from provider response to audit record (per D-11)
                    if response.token_usage is not None:
                        record["token_usage"] = response.token_usage.model_dump(mode="json")
                    # Phase 37: Record compaction metadata in audit.
                    if compaction_result is not None:
                        record["context_window"] = {
                            "strategy": compaction_result.strategy_name,
                            "tokens_before": compaction_result.tokens_before,
                            "tokens_after": compaction_result.tokens_after,
                            "messages_before": compaction_result.original_count,
                            "messages_after": compaction_result.compacted_count,
                        }
                    memory_interactions.extend(
                        await self._store_memory(
                            output.model_dump(mode="json"),
                            thread_id=thread_id,
                            runtime_context=resolved_runtime_context,
                        )
                    )
                    # Keep both memory reads and writes in the final audit record.
                    record["extra"]["memory_interactions"] = [
                        item.model_dump(mode="json") for item in memory_interactions
                    ]
                    # Phase 37: Pass compacted and archived messages to thread checkpoint.
                    _compacted_msgs = list(messages) if compaction_result is not None else None
                    _archived_msgs = None
                    if compaction_result is not None and hasattr(
                        compaction_result, "archived_messages"
                    ):
                        _archived_msgs = compaction_result.archived_messages
                    await self._checkpoint_thread_state(
                        thread_id,
                        validated_input,
                        output,
                        record,
                        compacted_messages=_compacted_msgs,
                        archived_messages=_archived_msgs,
                    )
                    return AgentRunResult(
                        input_data=validated_input.model_dump(mode="json"),
                        output_data=output.model_dump(mode="json"),
                        attempts=attempts,
                        prompt=prompt,
                        provider_response=response,
                        thread_state_snapshot=thread_state,
                        tool_call_records=tool_audits,
                        audit_record=record,
                    )
                except TimeoutError as exc:
                    last_error = AgentTimeoutError(
                        f"provider timed out after {provider_timeout_seconds} second(s)"
                    )
                    if not retry_policy.retry_on_timeout or attempt == max_attempts:
                        raise last_error from exc
                except AgentOutputValidationError as exc:
                    last_error = exc
                    if not retry_policy.retry_on_validation_error or attempt == max_attempts:
                        raise
                except Exception as exc:
                    last_error = exc
                    # Classify: only retry transient provider errors (per LLM-03)
                    retryable = is_retryable_provider_error(exc)
                    should_retry = retry_policy.retry_on_provider_error and retryable
                    if not should_retry or attempt == max_attempts:
                        if isinstance(last_error, AgentProviderError):
                            raise last_error from exc
                        raise AgentProviderError(str(last_error)) from last_error
                if retry_policy.use_exponential_backoff:
                    delay = compute_backoff_delay(
                        attempt,
                        base_delay=retry_policy.base_delay,
                        max_delay=retry_policy.max_delay,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                elif retry_policy.backoff_seconds:
                    await asyncio.sleep(retry_policy.backoff_seconds)
            if last_error is None:
                last_error = AgentProviderError("provider call failed without a specific error")
            raise AgentRetryExhaustedError(attempts=attempts, last_error=last_error)
        finally:
            await self._stop_mcp_servers()

    def _build_provider_request(
        self,
        messages: list[Any],
        metadata: dict[str, Any],
    ) -> ProviderRequest:
        """Build a ProviderRequest with tools, output_model, and model_params from config."""
        # Convert tool_attachments to OpenAI tool schemas
        tools: list[dict[str, Any]] | None = None
        if self.config.tool_attachments:
            tools = [att.to_openai_tool() for att in self.config.tool_attachments]

        # Pass the Pydantic output model directly — the provider adapter
        # uses LangChain's with_structured_output() for provider-agnostic
        # structured output handling.
        output_model = self.config.output_model
        if output_model is BaseModel or not getattr(output_model, "model_fields", None):
            output_model = None

        return ProviderRequest(
            model_name=self.config.model_name,
            messages=messages,
            metadata=metadata,
            tools=tools,
            output_model=output_model,
            model_params=self.config.model_params,
        )

    async def _resolve_tool_calls(
        self,
        *,
        response: Any,
        messages: list[Any],
        provider_timeout_seconds: float | None,
        approval_required_for_side_effects: bool,
    ) -> tuple[Any, list[Any], list[dict[str, Any]]]:
        """Execute any tool calls the model requested and re-call the model.

        Loops until the model stops requesting tool calls or the max
        tool call limit is reached. Returns the final response, the
        updated message list, and audit records for each tool call.
        """
        tool_audits: list[dict[str, Any]] = []
        tool_calls_used = 0
        current_response = response
        current_messages = list(messages)
        while getattr(current_response, "tool_calls", None):
            if self.tool_executor is None and self._mcp_manager is None:
                raise AgentProviderError(
                    "provider requested tool calls but no tool executor is configured"
                )
            tool_calls = list(current_response.tool_calls)
            if tool_calls_used + len(tool_calls) > self.config.max_tool_calls:
                raise AgentProviderError(
                    f"provider exceeded max_tool_calls={self.config.max_tool_calls}"
                )
            current_messages.append(self._assistant_message_for(current_response))
            for call in tool_calls:
                tool_calls_used += 1
                # Tool calls are checked against the declared manifest before anything executes.
                bindings = self.tool_bridge.ensure_declared_tools(
                    requested_tool_refs=[call["name"]],
                    declared_tool_refs=self.config.declared_tool_refs,
                )
                binding = bindings[0]
                if approval_required_for_side_effects and binding.side_effect_allowed:
                    raise AgentProviderError(
                        f"approval required for side-effecting tool call: {binding.alias}"
                    )
                self.tool_bridge.validate_permissions(binding, self.granted_tool_permissions)
                try:
                    # Route MCP tool calls through MCPClientManager
                    if (
                        binding.executable_unit_ref.startswith("mcp://")
                        and self._mcp_manager is not None
                    ):
                        result = await self._mcp_manager.call_tool(call["name"], call["args"])
                    else:
                        result = self.tool_executor(binding, call["args"])
                        if asyncio.iscoroutine(result):
                            result = await result
                    audit = self.tool_bridge.build_call_audit(
                        binding=binding,
                        arguments=call["args"],
                        granted_permissions=self.granted_tool_permissions,
                        outcome=result if isinstance(result, Mapping) else {"value": result},
                    )
                    content = json.dumps(result, ensure_ascii=False, sort_keys=True)
                    current_messages.append(
                        build_tool_message(
                            tool_call_id=call["id"],
                            name=call["name"],
                            content=content,
                        )
                    )
                except Exception as exc:
                    # Feed tool failures back as tool results so the model can react.
                    audit = self.tool_bridge.build_call_audit(
                        binding=binding,
                        arguments=call["args"],
                        granted_permissions=self.granted_tool_permissions,
                        error=str(exc),
                    )
                    current_messages.append(
                        build_tool_message(
                            tool_call_id=call["id"],
                            name=call["name"],
                            content=str(exc),
                            is_error=True,
                        )
                    )
                tool_audits.append(audit)
            # Phase 37: Compact between tool call re-invocations if needed.
            if self.context_tracker is not None:
                current_messages, _ = await self.context_tracker.maybe_compact(
                    current_messages,
                    self.config.model_name,
                )
            current_response = await run_provider_with_timeout(
                self.provider,
                self._build_provider_request(current_messages, {}),
                timeout_seconds=provider_timeout_seconds,
            )
        return current_response, current_messages, tool_audits

    async def _start_mcp_servers(self) -> None:
        """Start MCP server connections and register discovered tools."""
        if not self.config.mcp_servers:
            return
        self._mcp_manager = MCPClientManager(self.config.mcp_servers)
        discovered_tools = await self._mcp_manager.start()
        # Register discovered MCP tools into the tool bridge registry
        for manifest in discovered_tools:
            self.tool_bridge.registry.register(manifest)
        # Extend config's tool_attachments so they appear in declared_tool_refs
        # and get included in ProviderRequest.tools via _build_provider_request
        self.config = self.config.model_copy(
            update={"tool_attachments": list(self.config.tool_attachments) + discovered_tools}
        )

    async def _stop_mcp_servers(self) -> None:
        """Stop MCP server connections and clean up."""
        if self._mcp_manager is not None:
            await self._mcp_manager.stop()
            self._mcp_manager = None

    def _effective_timeout(
        self,
        configured_timeout: float | None,
        policy_timeout: float | None,
    ) -> float | None:
        """Choose the tighter timeout when policy and config both specify one."""
        if configured_timeout is None:
            return policy_timeout
        if policy_timeout is None:
            return configured_timeout
        return min(configured_timeout, policy_timeout)

    def _assistant_message_for(self, response: Any) -> Any:
        """Build an assistant message from a provider response for the message history."""
        raw = getattr(response, "raw", None)
        if raw is not None:
            return raw
        return {
            "role": "assistant",
            "content": getattr(response, "content", None),
            "tool_calls": list(getattr(response, "tool_calls", [])),
        }

    def _validate_input(self, input_payload: BaseModel | Mapping[str, Any]) -> BaseModel:
        """Validate and convert the input data to the agent's expected input model."""
        try:
            if isinstance(input_payload, BaseModel):
                return self.config.input_model.model_validate(input_payload.model_dump(mode="json"))
            return self.config.input_model.model_validate(input_payload)
        except ValidationError as exc:
            raise AgentInputValidationError(str(exc)) from exc

    async def _load_thread_state(self, thread_id: str | None) -> dict[str, Any] | None:
        """Load the saved thread state if a thread ID is provided."""
        if thread_id is None:
            return None
        store = self.thread_state_store
        if store is None:
            return None
        return await store.load(thread_id)

    async def _checkpoint_thread_state(
        self,
        thread_id: str | None,
        input_payload: BaseModel,
        output_payload: BaseModel,
        record: dict[str, Any],
        *,
        compacted_messages: list[Any] | None = None,
        archived_messages: list[Any] | None = None,
    ) -> None:
        """Save the current input, output, and audit record as thread state."""
        if thread_id is None or self.thread_state_store is None:
            return
        state: dict[str, Any] = {
            "input": input_payload.model_dump(mode="json"),
            "output": output_payload.model_dump(mode="json"),
            "audit": record,
        }
        # Phase 37: Persist compacted messages for cross-run continuity.
        if compacted_messages is not None:
            state["compacted_messages"] = compacted_messages
        if archived_messages is not None:
            state["archived_messages"] = archived_messages
        await self.thread_state_store.checkpoint(thread_id, state)

    async def _load_memory(
        self,
        *,
        thread_id: str | None,
        runtime_context: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[MemoryAccessRecord]]:
        """Read data from all configured memory connectors for this agent.

        Returns the memory payload to include in the prompt and a list
        of audit records describing each memory read.
        """
        resolver = self.memory_resolver
        if resolver is None or not self.config.memory_refs:
            return {}, []
        bindings = await resolver.resolve(
            self.config.memory_refs,
            thread_id=thread_id,
            runtime_context=runtime_context,
            node_id=self.config.name,
        )
        memory_payload: dict[str, Any] = {}
        interactions: list[MemoryAccessRecord] = []
        for binding in bindings:
            entry = await binding.connector.read("latest", MemoryScope.RUN)
            value = entry.value if entry is not None else None
            # Expose each memory source under its own ref in the prompt payload.
            memory_payload[binding.memory_ref] = {"latest": value} if value is not None else {}
            interactions.append(
                MemoryAccessRecord(
                    memory_ref=binding.memory_ref,
                    connector_type=binding.manifest.connector_type,
                    scope=binding.manifest.scope.value,
                    operation="read",
                    key="latest",
                    value=value,
                )
            )
        return memory_payload, interactions

    async def _store_memory(
        self,
        output_payload: Mapping[str, Any],
        *,
        thread_id: str | None,
        runtime_context: Mapping[str, Any],
    ) -> list[MemoryAccessRecord]:
        """Write the agent's output to all configured memory connectors.

        Returns a list of audit records describing each memory write.
        """
        resolver = self.memory_resolver
        if resolver is None or not self.config.memory_refs:
            return []
        bindings = await resolver.resolve(
            self.config.memory_refs,
            thread_id=thread_id,
            runtime_context=runtime_context,
            node_id=self.config.name,
        )
        interactions: list[MemoryAccessRecord] = []
        for binding in bindings:
            # The MVP stores the latest structured output for each memory binding.
            await binding.connector.write("latest", dict(output_payload), MemoryScope.RUN)
            interactions.append(
                MemoryAccessRecord(
                    memory_ref=binding.memory_ref,
                    connector_type=binding.manifest.connector_type,
                    scope=binding.manifest.scope.value,
                    operation="write",
                    key="latest",
                    value=dict(output_payload),
                )
            )
        return interactions
