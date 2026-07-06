# Context Window Management

*Added in v4.0*

Context window management tracks token usage per agent node and triggers automatic compaction when a configurable threshold is reached, preventing LLM context overflow without manual intervention.

## How It Works

After each LLM invocation, the `ContextWindowTracker` updates the cumulative token count. When tokens exceed the configured threshold, a compaction strategy runs to reduce context size. The compacted result persists in thread memory, and the original messages can optionally be archived. The tracker produces a `CompactionResult` describing what was removed and the resulting token savings.

## Key Components

- **`ContextWindowTracker`** -- Orchestrates token tracking and triggers compaction. Configured per-agent-node via `ContextWindowSettings`. Maintains `CompactionState` across invocations.
- **`TruncationStrategy`** -- Drops oldest messages to fit within the token budget. Fastest strategy with no LLM cost, but loses context permanently.
- **`ObservationMaskingStrategy`** -- Replaces observation/tool-output content with a placeholder, preserving message structure while reducing tokens. Good balance between savings and context preservation.
- **`LLMSummarizationStrategy`** -- Uses an LLM call to summarize the conversation history into a condensed form. Highest quality retention but adds latency and cost.
- **`CompactionStrategy`** -- Abstract base class for implementing custom compaction strategies.

## Configuration

Context window settings are per-agent-node, configured on `ContextWindowSettings`:

- `enabled` -- Whether context window management is active for this node.
- `max_tokens` -- Token threshold triggering compaction.
- `strategy` -- Which compaction strategy to use (`truncation`, `observation_masking`, or `summarization`).
- `archive_originals` -- Whether to keep original messages after compaction.

## Compaction Lifecycle

1. Agent node invokes the LLM and receives a response.
2. `ContextWindowTracker` updates the cumulative token count.
3. If tokens exceed `max_tokens`, the configured `CompactionStrategy` runs.
4. The strategy produces a `CompactionResult` with the reduced message list.
5. `CompactionState` is updated with the new token count and compaction history.
6. If `archive_originals` is enabled, original messages are preserved in thread memory.

## Error Handling

- **`TokenCountError`** -- Raised when token counting fails (e.g., unsupported model encoding).
- **`CompactionError`** -- Raised when a compaction strategy fails to reduce context.
- **`ContextWindowError`** -- Base error for all context window operations.

See the [API Reference](../reference/http-api.md) for endpoint details and the source code under `zeroth.core.context_window` for implementation.
