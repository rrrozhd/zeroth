"""Content-free renderings of producer-supplied audit payloads.

:class:`~zeroth.governance.audit.capture_policy.AuditCapturePolicy` decides
*whether* an event may retain content; this module decides *what a dropped
payload is replaced by*. Everything here answers "was it the same payload, what
shape was it, and how big was it?" and nothing here can answer "what did it
say".

**The invariant: no value reaching this module is ever rendered by code the
value itself supplies.** ``str(key)``, ``repr(value)`` and ``format`` all
dispatch to methods on the object being described, so a producer -- or an
attacker who reached a producer -- can make the *description* of a payload
carry the payload. That is not hypothetical: a mapping key whose ``__str__``
returned a credential put the credential into a persisted schema, and the same
key made ``json.dumps`` raise, which turned the digest into ``None`` and
silently removed the hash that was supposed to stand in for the dropped
content. :func:`canonicalize` therefore dispatches on *type*, never on
behaviour: exact scalar types pass through, mappings and sequences are walked,
and anything else becomes a bounded type name. Every other function here reads
the canonical form, so the digest cannot fail and the schema cannot be
authored.

**Names are gated, not printed.** A key survives into a schema only when it is
an exact ``str``, is short enough to be a name rather than a credential, and
matches an identifier-shaped allowlist; anything else is replaced. The same
gate covers type names, because a dynamically constructed class can carry an
arbitrary ``__name__``.

**Metadata is an allowlist, not a scrub.** ``execution_metadata`` and approval
metadata are free-form ``dict[str, Any]`` that producers fill with whatever
they were holding -- a prompt under ``execution_metadata["prompt"]``, a
password nested in a tuple, a raw exception string. Best-effort key-based
redaction cannot close that: it only masks the key names someone thought of.
:meth:`ContentFreeProjection.metadata` keeps the keys this module recognises as
structural, bounds and scrubs their values, and replaces everything else with a
digest, a schema and a count -- so an unrecognised key contributes evidence,
never text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

REDACTED = "***REDACTED***"

# Bounds every walk here, so a deep or cyclic payload cannot exhaust the stack
# inside the delivery worker.
MAX_DEPTH = 6
# A key name is schema; a string long enough to be a credential is not.
_MAX_NAME_CHARS = 64
# An allowlisted metadata value is an identifier, a status or a short label.
# Anything longer is described rather than retained.
_MAX_METADATA_TEXT_CHARS = 256
# Identifier-shaped: what a key name looks like when it is a name.
_SAFE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.:\-]*\Z")

# The metadata keys this stage recognises as structural. A key outside this set
# is not retained under a metadata-only capture, whatever it holds: the set is
# the whole guarantee, so it is deliberately short and lists only keys whose
# meaning is fixed by this codebase rather than by a payload.
ALLOWED_METADATA_KEYS = frozenset(
    {
        "assistant_id",
        "attempt",
        "budget_cap_usd",
        "budget_check_degraded",
        "budget_spend_usd",
        "compatibility_fingerprint",
        "correlation_id",
        "cost_event_id",
        "cost_usd",
        "decision",
        "disposition",
        "duration_ms",
        "enforcement_applied",
        "governance_level",
        "input_sha256",
        "input_size_bytes",
        "model_name",
        "node_kind",
        "operation",
        "output_sha256",
        "output_size_bytes",
        "policy_version",
        "provider",
        "reason_code",
        "retry_count",
        "reviewer",
        "status",
        "upstream_status_code",
    }
)


def safe_name(name: Any) -> str:
    """Return ``name`` when it is an unauthored identifier, else the redaction marker.

    Args:
        name: The candidate name. Only an exact ``str`` can pass -- a ``str``
            subclass is rejected because ``str(...)`` on one dispatches to its
            own ``__str__``.

    Returns:
        The name itself, or :data:`REDACTED`.
    """
    if type(name) is not str or len(name) > _MAX_NAME_CHARS:
        return REDACTED
    return name if _SAFE_NAME.match(name) else REDACTED


def type_name(value: Any) -> str:
    """Name a value's type without letting the value render itself."""
    return safe_name(type(value).__name__)


def canonicalize(value: Any, *, depth: int = 0) -> Any:
    """Render any payload as JSON-safe data, dispatching on type and never on behaviour.

    Args:
        value: The payload to render. Arbitrarily shaped and producer-supplied.
        depth: Current recursion depth, bounded by :data:`MAX_DEPTH`.

    Returns:
        A structure of ``None``, ``bool``, ``int``, ``float``, ``str``, ``list``
        and ``dict`` with ``str`` keys -- so :func:`json.dumps` on it cannot
        raise, and nothing in it was produced by ``__str__`` or ``__repr__``.
    """
    if depth >= MAX_DEPTH:
        return "..."
    value_type = type(value)
    if value is None or value_type is bool or value_type is int or value_type is float:
        return value
    if value_type is str:
        return value
    if isinstance(value, Mapping):
        return {safe_name(key): canonicalize(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [canonicalize(item, depth=depth + 1) for item in value]
    if isinstance(value, set | frozenset):
        items = [canonicalize(item, depth=depth + 1) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, str):
        # A ``str`` subclass: take the underlying characters through ``str``'s own
        # implementation rather than the subclass's ``__str__``.
        return str.__str__(value)
    if isinstance(value, bytes | bytearray):
        return f"<bytes:{len(value)}>"
    return f"<{type_name(value)}>"


def digest(canonical: Any) -> str:
    """Hash an already-canonical rendering; total, because the input cannot fail to dump."""
    rendered = json.dumps(canonical, sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def entry_count(canonical: Any) -> int:
    """Count a canonical payload's entries (or characters), never inspecting them."""
    if canonical is None:
        return 0
    if type(canonical) is dict or type(canonical) is list or type(canonical) is str:
        return len(canonical)
    return 1


class ContentFreeProjection:
    """Describe dropped payloads as digests, schemas and counts.

    Args:
        scrub: The policy's redaction chain, applied to the *names* and short
            values this projection retains. It is a complement, not the
            guarantee: what makes the output content-free is that only gated
            names and allowlisted keys survive at all.
    """

    def __init__(self, scrub: Callable[[Any], Any]) -> None:
        self._scrub = scrub

    def summarize(self, value: Any) -> dict[str, Any]:
        """Describe a dropped payload -- digest, shape and size -- without reproducing it."""
        canonical = canonicalize(value)
        return {
            "sha256": digest(canonical),
            "schema": self.schema(canonical),
            "count": entry_count(canonical),
        }

    def schema(self, canonical: Any, *, depth: int = 0) -> Any:
        """Describe a canonical payload's shape: gated key names and type names only."""
        if depth >= MAX_DEPTH:
            return "..."
        if type(canonical) is dict:
            return {
                self._name(key): self.schema(item, depth=depth + 1)
                for key, item in canonical.items()
            }
        if type(canonical) is list:
            # The first element stands for the list's shape; its length is in ``count``.
            return [self.schema(canonical[0], depth=depth + 1)] if canonical else []
        return type_name(canonical)

    def metadata(self, metadata: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Project free-form metadata onto the allowlist, summarizing what it drops.

        Args:
            metadata: The producer's metadata mapping, arbitrarily shaped.

        Returns:
            The retained projection, and a summary of the whole submitted
            mapping (digest, schema, count and how many keys were dropped) so
            the drop is evidence rather than a silent hole.
        """
        kept: dict[str, Any] = {}
        dropped = 0
        for key, value in metadata.items():
            if type(key) is not str or key not in ALLOWED_METADATA_KEYS:
                dropped += 1
                continue
            kept[key] = self._bounded(value)
        summary = self.summarize(metadata)
        summary["dropped_keys"] = dropped
        return kept, summary

    def _bounded(self, value: Any) -> Any:
        """Retain one allowlisted value only while it stays a bounded scalar."""
        value_type = type(value)
        if value is None or value_type is bool or value_type is int or value_type is float:
            return value
        if isinstance(value, str):
            text = self._scrub(str.__str__(value))
            if type(text) is not str:
                return REDACTED
            return text if len(text) <= _MAX_METADATA_TEXT_CHARS else self.summarize(value)
        return self.summarize(value)

    def _name(self, name: str) -> str:
        """Gate a key name, then run the retained name through the redaction chain."""
        gated = safe_name(name)
        if gated == REDACTED:
            return REDACTED
        scrubbed = self._scrub(gated)
        return scrubbed if type(scrubbed) is str else REDACTED


__all__ = [
    "ALLOWED_METADATA_KEYS",
    "MAX_DEPTH",
    "REDACTED",
    "ContentFreeProjection",
    "canonicalize",
    "digest",
    "entry_count",
    "safe_name",
    "type_name",
]
