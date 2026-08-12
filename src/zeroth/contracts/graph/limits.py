"""Authoring limits that both the publish gate and the runtime must agree on."""

from __future__ import annotations

# Inline source travels inside graph payload rows and diff output — cap it
# well below anything that would strain those paths.
INLINE_SOURCE_MAX_CHARS = 65_536

# A05-5: inline_source was the only authored string with a declared bound. The
# rest of what an author writes into a graph — the agent's instruction, every
# author-written description, and every condition expression — was unbounded, so
# a single graph could carry an arbitrary payload into the same rows, diffs, and
# provider requests that INLINE_SOURCE_MAX_CHARS exists to protect. The
# instruction additionally reaches the model as prompt text on every step.
#
# Enforced at the publish gate rather than as a Pydantic ``max_length``: these
# models also deserialize graph rows that were persisted before any bound
# existed, and a field-level cap would make an oversized stored graph
# unloadable instead of unpublishable.
#
# Sized generously against real authoring: an instruction is a system prompt (a
# few thousand tokens at most), a description is one line the model reads in a
# tool schema, and an expression is a single predicate.
AGENT_INSTRUCTION_MAX_CHARS = 32_768
DESCRIPTION_MAX_CHARS = 2_048
CONDITION_EXPRESSION_MAX_CHARS = 4_096

# Display metadata is loaded from historical graph rows, so these are enforced by
# the publish validator rather than Pydantic fields. Each value is intentionally
# generous for UI copy while preventing a single label or tag from becoming an
# unbounded graph payload.
DISPLAY_TITLE_MAX_CHARS = 512
DISPLAY_TAG_MAX_CHARS = 256
DISPLAY_LABEL_MAX_CHARS = 256
