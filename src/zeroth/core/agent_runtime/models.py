"""Data models used throughout the agent runtime.

These classes define the shapes of configuration, prompts, results, and
thread state. They are all Pydantic models, which means they automatically
validate the data you put into them.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from zeroth.core.agent_runtime.mcp import MCPServerConfig
from zeroth.core.agent_runtime.tools import ToolAttachmentManifest
from zeroth.core.governed.tools.base import ExecutionPlacement


class RetryPolicy(BaseModel):
    """Controls how many times the agent should retry when something goes wrong.

    You can configure whether to retry on validation errors, provider errors,
    or timeouts, and how long to wait between retries.
    """

    # "extra=forbid" means passing unknown fields raises an error, catching typos early
    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=0, ge=0)
    # Each retry type can be toggled independently so you can, e.g.,
    # retry on timeouts but fail immediately on bad output
    retry_on_validation_error: bool = True
    retry_on_provider_error: bool = True
    retry_on_timeout: bool = True
    backoff_seconds: float = Field(default=0.0, ge=0.0)
    # Exponential backoff settings (used when use_exponential_backoff=True)
    base_delay: float = Field(default=1.0, ge=0.0)
    max_delay: float = Field(default=60.0, ge=0.0)
    use_exponential_backoff: bool = True

    @property
    def max_attempts(self) -> int:
        """Return the total number of attempts (initial try plus retries)."""
        return self.max_retries + 1


class PromptConfig(BaseModel):
    """Settings that control what goes into the prompt sent to the AI model.

    For example, you can choose whether to include the input/output schemas,
    thread state, tool references, or memory references in the prompt.
    You can also specify keys whose values should be hidden (redacted).
    """

    model_config = ConfigDict(extra="forbid")

    include_input_schema: bool = True
    include_output_schema: bool = True
    include_thread_state: bool = True
    include_tool_refs: bool = True
    include_memory_refs: bool = True
    # Keys whose values get replaced with "***REDACTED***" in prompts and audit logs
    redact_keys: tuple[str, ...] = ("password", "secret", "token")
    extra_context: dict[str, Any] = Field(default_factory=dict)
    # When set, the input payload field under this key is read as a list of
    # chat messages ({role: human|ai|tool, content}) and rendered as real
    # conversation turns instead of staying inside the input JSON block.
    messages_key: str | None = None
    # Replay conversation turns stored in thread state before the incoming
    # ones, and append new turns + the agent's reply after each run.
    persist_conversation: bool = False
    # Cap on conversation turns kept and replayed. None keeps everything.
    conversation_max_turns: int | None = None


class ModelParams(BaseModel):
    """Per-node LLM parameters. None means use provider default."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    seed: int | None = None


class ToolOutputSafetyConfig(BaseModel):
    """Model-boundary safety controls for untrusted tool/memory output.

    Tool results and memory-sourced content are re-injected into the LLM, where
    they can carry prompt-injection payloads. These settings length-cap, screen,
    and provenance-wrap that untrusted content before the model sees it. Enabled
    by default (governance-first); set ``enabled=False`` to restore the prior
    raw re-injection. See ``zeroth.core.agent_runtime.sanitization``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # Default per-tool output cap (characters); individual tools can override via
    # ToolAttachmentManifest.max_output_chars.
    max_output_chars: int = Field(default=8000, ge=1)
    wrap_with_provenance: bool = True
    screen_for_injection: bool = True
    # "flag" annotates the audit trail; "block" withholds the suspect content.
    screening_mode: Literal["flag", "block"] = "flag"


class ContentSafetyConfig(BaseModel):
    """Content-safety policy for an agent's own input and output.

    Detects PII and blocklisted terms in agent input/output and, depending on
    ``mode``, flags (audit only), redacts, or blocks the run. **Opt-in**
    (``enabled=False`` by default): unlike model-boundary sanitization, this
    imposes a content policy on the application's own typed data, which is
    high-blast-radius, so it is off until explicitly configured. See
    ``zeroth.core.guardrails.content``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    scan_input: bool = True
    scan_output: bool = True
    # "flag" = audit only; "redact" = replace findings in-place; "block" = reject the run.
    mode: Literal["flag", "redact", "block"] = "flag"
    detect_pii: bool = True
    # Empty = all built-in PII types (email, ssn, credit_card, phone).
    pii_types: tuple[str, ...] = ()
    # Case-insensitive literal terms to flag/redact/block.
    blocklist: tuple[str, ...] = ()


class AgentConfig(BaseModel):
    """The main configuration for an agent.

    This holds everything the runtime needs to know about an agent: its name,
    instruction, which AI model to use, what input/output shapes it expects,
    which tools it can use, retry behavior, and more.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    name: str
    description: str = ""
    instruction: str
    model_name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_tools: list[str] = Field(default_factory=list)
    tool_attachments: list[ToolAttachmentManifest] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    prompt_config: PromptConfig = Field(default_factory=PromptConfig)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: float | None = Field(default=None, ge=0.0)
    max_tool_calls: int = Field(default=4, ge=0)
    tool_output_safety: ToolOutputSafetyConfig = Field(default_factory=ToolOutputSafetyConfig)
    content_safety: ContentSafetyConfig = Field(default_factory=ContentSafetyConfig)
    execution_placement: ExecutionPlacement = "local_only"
    requires_approval: bool = False
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_params: ModelParams | None = None

    @property
    def declared_tool_refs(self) -> list[str]:
        """Return the list of tool names this agent is allowed to use."""
        if self.tool_attachments:
            return [attachment.alias for attachment in self.tool_attachments]
        return list(self.allowed_tools)


class PromptMessage(BaseModel):
    """A single message in a prompt (like one chat bubble).

    Each message has a role (system, user, or assistant) and text content.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class PromptAssembly(BaseModel):
    """The fully assembled prompt ready to be sent to the AI model.

    Contains the list of messages, a rendered text version, and metadata
    that gets logged for auditing purposes.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[PromptMessage] = Field(default_factory=list)
    rendered_prompt: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRunResult(BaseModel):
    """The result you get back after an agent finishes running.

    Contains the input that was sent, the output that was produced,
    how many attempts it took, the prompt that was used, the raw
    provider response, and an audit trail of what happened.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    input_data: dict[str, Any]
    output_data: dict[str, Any]
    attempts: PositiveInt
    prompt: PromptAssembly
    provider_response: Any
    thread_state_snapshot: dict[str, Any] | None = None
    tool_call_records: list[dict[str, Any]] = Field(default_factory=list)
    audit_record: dict[str, Any] = Field(default_factory=dict)


class ThreadStateStore(Protocol):
    """Interface for saving and loading conversation thread state.

    Any class that has ``load`` and ``checkpoint`` methods with the right
    signatures can be used as a thread state store. This lets you swap
    between in-memory, database, or other storage backends.
    """

    async def load(self, thread_id: str) -> dict[str, Any] | None:  # pragma: no cover - protocol
        """Load the saved state for a given thread, or None if no state exists."""

    async def checkpoint(
        self,
        thread_id: str,
        state: dict[str, Any],
    ) -> None:  # pragma: no cover - protocol
        """Save a snapshot of the thread's current state."""


class InMemoryThreadStateStore:
    """A simple thread state store that keeps everything in memory.

    Good for tests and local development. Data is lost when the process stops.
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        """Load the latest saved state for a thread, or None if nothing was saved."""
        state = self._state.get(thread_id)
        if state is None:
            return None
        return dict(state)

    async def checkpoint(self, thread_id: str, state: dict[str, Any]) -> None:
        """Save a snapshot of the thread state and keep it in the history."""
        snapshot = dict(state)
        self._state[thread_id] = snapshot
        self._history.setdefault(thread_id, []).append(snapshot)

    def latest(self, thread_id: str) -> dict[str, Any] | None:
        """Return the most recently saved state for a thread (sync version)."""
        return self._state.get(thread_id)

    def history(self, thread_id: str) -> list[dict[str, Any]]:
        """Return all saved state snapshots for a thread, oldest first."""
        return list(self._history.get(thread_id, []))
