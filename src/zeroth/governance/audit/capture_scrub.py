"""The capture boundary's redaction chain: one bounded walk, one closed container set.

Split out of :mod:`zeroth.governance.audit.capture_policy` because it is the
one part of the boundary that is a *complement* rather than the guarantee: the
channel drop and the typed metadata allowlist are what hold, and this chain is
what tightens whatever those let through.

**The invariant: every container the record can carry is traversed by the same
code.** The chain used to be three walkers stacked on each other -- a key-based
sanitizer, a registered-secret redactor and a PII filter -- and the three
disagreed about what a container is. All three walked mappings and lists; only
one walked tuples; none walked sets. So a registered secret nested inside a
``set`` passed through the whole chain untouched and Pydantic then serialized it
into the durable JSON of a content-mode record, which is the one place the
channel drop does not cover. The rungs are still the primitives -- they are the
reviewed implementations of *what a leaf becomes* -- but the traversal is now
:meth:`RedactionChain.scrub` alone, over one closed set of container types.
Adding a container type is an edit here, and anything that is not in the set is
replaced by a bounded marker rather than passed through.

**A mapping key is treated exactly like a value.** ``SecretRedactor`` rebuilds a
mapping as ``{key: redact(item)}`` and the PII walk did the same, so a
*registered* secret used as a mapping key was reproduced verbatim. Keys go
through the same masking as values, and a key that is not an exact ``str``
becomes :data:`~zeroth.governance.audit.capture_projection.REDACTED` rather than
``str(key)``, which would have dispatched to the key's own ``__str__``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from zeroth.governance.audit.capture_projection import MAX_DEPTH, REDACTED, type_name
from zeroth.governance.audit.models import AuditRedactionConfig
from zeroth.governance.guardrails.content import PIIFilter
from zeroth.platform.secrets.redaction import SecretRedactor

# These normalized key names are a floor a caller may widen and cannot narrow;
# separator and case variants compare equivalently while output spelling stays
# untouched.
DEFAULT_REDACT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)

# Only these patterns: PIIFilter's phone heuristic matches any ten-digit run.
_PII_PATTERNS: tuple[str, ...] = ("email", "ssn", "credit_card")


def _normalized_key(value: str) -> str:
    return value.casefold().replace("-", "").replace("_", "")


class RedactionChain:
    """Key redaction, registered-secret masking and PII filtering over one payload.

    Args:
        redaction: Extra key-redaction and path-omission rules, merged with
            :data:`DEFAULT_REDACT_KEYS` -- a supplied config widens the default
            and cannot narrow it.
        known_secrets: Resolved secret values to mask wherever they appear,
            including in mapping keys and inside sets. Registering none only
            weakens the value-based complement.
    """

    def __init__(
        self,
        *,
        redaction: AuditRedactionConfig | None = None,
        known_secrets: Mapping[str, str] | None = None,
    ) -> None:
        config = AuditRedactionConfig() if redaction is None else redaction
        self._redact_keys = frozenset(
            _normalized_key(key) for key in DEFAULT_REDACT_KEYS | frozenset(config.redact_keys)
        )
        self._omit_paths = frozenset(
            tuple(_normalized_key(part) for part in path) for path in config.omit_paths
        )
        self._secrets = SecretRedactor(known_secrets)
        self._pii = PIIFilter(_PII_PATTERNS)

    def scrub(self, value: Any) -> Any:
        """Walk one payload, masking every string leaf and every mapping key in it."""
        walked = self._walk(value, path=(), depth=0, ancestors=frozenset())
        return None if walked is _OMITTED else walked

    def scrub_key(self, key: Any) -> str:
        """Mask one mapping key, accepting only an exact ``str`` to begin with."""
        return self._scrub_text(key) if type(key) is str else REDACTED

    def _scrub_text(self, text: str) -> str:
        """Mask registered secrets and then PII in one exact ``str``."""
        masked = self._secrets.redact(text)
        if type(masked) is not str:
            return REDACTED
        filtered, _findings = self._pii.apply(masked)
        return filtered if type(filtered) is str else REDACTED

    def _walk(
        self,
        value: Any,
        *,
        path: tuple[str, ...],
        depth: int,
        ancestors: frozenset[int],
    ) -> Any:
        """Render one node of the payload, dispatching on its exact type.

        Args:
            value: The node to render.
            path: The mapping-key path to this node, for ``omit_paths``.
            depth: Current recursion depth, bounded by :data:`MAX_DEPTH`.
            ancestors: Container identities on the active path, for cycle detection.

        Returns:
            A JSON-safe rendering, or :data:`_OMITTED` when a configured path
            drops it. Containers outside the closed set below cannot reach the
            persisted record intact, so no traversal gap can hide a leaf.
        """
        if path in self._omit_paths:
            return _OMITTED
        if depth >= MAX_DEPTH:
            return REDACTED
        value_type = type(value)
        if value is None or value_type is bool or value_type is int:
            return value
        if value_type is float:
            return value if math.isfinite(value) else REDACTED
        if value_type is str:
            return self._scrub_text(value)
        if isinstance(value, Mapping):
            if id(value) in ancestors:
                return REDACTED
            return self._walk_mapping(
                value, path=path, depth=depth, ancestors=ancestors | {id(value)}
            )
        if isinstance(value, list | tuple):
            if id(value) in ancestors:
                return REDACTED
            return self._walk_items(
                value, path=path, depth=depth, ancestors=ancestors | {id(value)}
            )
        if isinstance(value, set | frozenset):
            # Sets included -- a set is what the old chain walked past. Rendered
            # as a sorted list, because JSON has no set and iteration order over
            # one is not stable between processes.
            if id(value) in ancestors:
                return REDACTED
            return sorted(
                self._walk_items(
                    value, path=path, depth=depth, ancestors=ancestors | {id(value)}
                ),
                key=_ordering,
            )
        if isinstance(value, str):
            # A ``str`` subclass: take the underlying characters through ``str``'s
            # own implementation rather than the subclass's ``__str__``.
            return self._scrub_text(str.__str__(value))
        if isinstance(value, bytes | bytearray):
            return f"<bytes:{len(value)}>"
        return f"<{type_name(value)}>"

    def _walk_items(
        self,
        value: Any,
        *,
        path: tuple[str, ...],
        depth: int,
        ancestors: frozenset[int],
    ) -> list[Any]:
        """Render one sequence's items, dropping the ones an omission rule removes."""
        items = [
            self._walk(item, path=path, depth=depth + 1, ancestors=ancestors) for item in value
        ]
        return [item for item in items if item is not _OMITTED]

    def _walk_mapping(
        self,
        value: Mapping[Any, Any],
        *,
        path: tuple[str, ...],
        depth: int,
        ancestors: frozenset[int],
    ) -> Any:
        """Rebuild one mapping with masked keys, applying the key and path rules."""
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_str = self.scrub_key(key)
            child = (*path, _normalized_key(key_str))
            if child in self._omit_paths:
                continue
            if _normalized_key(key_str) in self._redact_keys:
                result[key_str] = REDACTED
                continue
            walked = self._walk(item, path=child, depth=depth + 1, ancestors=ancestors)
            if walked is not _OMITTED:
                result[key_str] = walked
        return result


class _OmittedType:
    """Sentinel marking a node an ``omit_paths`` rule dropped."""


_OMITTED = _OmittedType()


def _ordering(item: Any) -> str:
    """Order a rendered set's items by their own rendering, never by their type's."""
    return json.dumps(item, sort_keys=True)


__all__ = ["DEFAULT_REDACT_KEYS", "RedactionChain"]
