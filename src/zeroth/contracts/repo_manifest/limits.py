"""Named bounds for the `.zeroth.yaml` repository manifest.

Every limit an author-supplied manifest is held to lives here, so the schema
in :mod:`zeroth.contracts.repo_manifest.document`, the parser budget handed to
:func:`zeroth.platform.primitives.load_untrusted_yaml`, and any error message
that names a bound all read from one place.
"""

from __future__ import annotations

__all__ = [
    "CONFIG_FILENAME",
    "ENVIRONMENT_KEY_PATTERN",
    "CAPABILITY_NAME_PATTERN",
    "MAX_CAPABILITIES",
    "MAX_ENVIRONMENT_VALUE_CHARS",
    "MAX_MANIFEST_BYTES",
    "MAX_PATH_CHARS",
    "MAX_PATH_SEGMENTS",
    "MAX_SMOKE_FILES",
    "MAX_STDOUT_CONTAINS_CHARS",
    "SCRIPT_NAME_PATTERN",
]

# Where a repository declares its manifest, relative to the checkout root.
CONFIG_FILENAME = ".zeroth.yaml"

# 128 KiB. A real manifest is a few hundred bytes; the cap only ever admits
# padding, and it bounds the parser's input before a single byte is decoded.
MAX_MANIFEST_BYTES = 131_072

# Relative-path fields: bounded length, bounded segment count. The shape rules
# themselves (no NUL, no backslash, no leading slash, no ".." segment) are
# enforced by the models.
MAX_PATH_CHARS = 512
MAX_PATH_SEGMENTS = 32

# Script names key the ``scripts`` mapping and appear in issue paths, so the
# charset is narrow enough to be safe to echo.
SCRIPT_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"

# Environment variables: conventional uppercase names, bounded values. Secret
# references are deliberately not a v1 feature -- a value is a literal string.
ENVIRONMENT_KEY_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
MAX_ENVIRONMENT_VALUE_CHARS = 1024

# Capability names requested by a script.
CAPABILITY_NAME_PATTERN = r"^[a-z][a-z0-9_.:\-]{0,63}$"
MAX_CAPABILITIES = 16

# Smoke-check bounds.
MAX_SMOKE_FILES = 16
MAX_STDOUT_CONTAINS_CHARS = 256
