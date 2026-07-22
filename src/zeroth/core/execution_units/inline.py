"""Legacy import path for :mod:`zeroth.integrations.execution.inline`."""

from zeroth.integrations.execution.inline import (
    INLINE_COMMAND,
    INLINE_ENTRY_FILENAME,
    INLINE_SOURCE_MAX_CHARS,
    FreeformPayload,
    build_inline_binding,
    build_inline_manifest,
    inline_source_digest,
)

__all__ = [
    "FreeformPayload",
    "INLINE_COMMAND",
    "INLINE_ENTRY_FILENAME",
    "INLINE_SOURCE_MAX_CHARS",
    "build_inline_binding",
    "build_inline_manifest",
    "inline_source_digest",
]
