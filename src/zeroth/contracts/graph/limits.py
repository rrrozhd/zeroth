"""Authoring limits that both the publish gate and the runtime must agree on."""

from __future__ import annotations

# Inline source travels inside graph payload rows and diff output — cap it
# well below anything that would strain those paths.
INLINE_SOURCE_MAX_CHARS = 65_536
