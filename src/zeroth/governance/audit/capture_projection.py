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

**Names are never printed, only counted and hashed.** Gating a key on
"identifier-shaped and short" was not enough: ``AKIAIOSFODNN7EXAMPLE`` is a
perfectly well-formed identifier, so a seeded credential used as a mapping key
was persisted verbatim inside a dropped-content schema. A schema key is now
:func:`key_digest` -- a truncated SHA-256 of the key -- so two payloads that
shared a key still look alike and no producer text survives. Type names in a
schema come from a closed set (:data:`_SCHEMA_TYPE_NAMES`), because
:meth:`ContentFreeProjection.schema` only ever reads canonical values and a
dynamically constructed class can carry any ``__name__`` it likes.

**Metadata is a typed allowlist, not a scrub.** ``execution_metadata`` and
approval metadata are free-form ``dict[str, Any]`` that producers fill with
whatever they were holding -- a prompt under ``execution_metadata["prompt"]``, a
password nested in a tuple, a raw exception string. Best-effort key-based
redaction cannot close that: it only masks the key names someone thought of. An
allowlist of key *names* did not close it either, because every allowlisted key
accepted any short string: ``correlation_id`` is filled from a client-supplied
``X-Correlation-ID`` header, so a credential pasted into that header was
persisted verbatim. Each allowlisted key therefore declares a
:class:`MetadataKind` -- a number, a boolean, a digest, a bounded lowercase
label, or an opaque identifier that is only ever hashed -- and a value that does
not fit its key's kind is summarized rather than retained.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

REDACTED = "***REDACTED***"

# Bounds every walk here, so a deep or cyclic payload cannot exhaust the stack
# inside the delivery worker.
MAX_DEPTH = 6
# A key name is schema; a string long enough to be a credential is not.
_MAX_NAME_CHARS = 64
# An allowlisted label is a status, a route name or a short vocabulary term.
_MAX_LABEL_CHARS = 64
# Identifier-shaped: what a key name looks like when it is a name.
_SAFE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.:\-]*\Z")
# A label this stage retains verbatim: lower-case, punctuated only by the
# separators a vocabulary term uses. Deliberately narrower than _SAFE_NAME --
# credentials are overwhelmingly mixed-case or opaque, and a value that fails
# this shape is summarized rather than lost.
_SAFE_LABEL = re.compile(r"\A[a-z0-9][a-z0-9._:/\-]*\Z")
# A hash, optionally prefixed by its algorithm ("sha256:...").
_SAFE_DIGEST = re.compile(r"\A(?:[a-z0-9]{1,16}:)?[0-9a-f]{16,128}\Z")
# How much of a key's SHA-256 stands in for the key in a schema.
_KEY_DIGEST_CHARS = 16
# The only type names a schema can carry, because schemas read canonical values.
_SCHEMA_TYPE_NAMES = frozenset({"NoneType", "bool", "int", "float", "str", "dict", "list"})


class MetadataKind(StrEnum):
    """What one allowlisted metadata key is allowed to hold.

    The kind is the whole of the per-key contract: a value that does not match
    its key's kind is replaced by a summary, so widening what a key accepts
    takes a deliberate edit here rather than a producer writing something new
    into it.
    """

    NUMBER = "number"
    BOOLEAN = "boolean"
    DIGEST = "digest"
    LABEL = "label"
    OPAQUE = "opaque"


# The metadata keys this stage recognises as structural, each with the kind of
# value it may carry. A key outside this mapping is not retained under a
# metadata-only capture, whatever it holds.
#
# OPAQUE marks the identifiers whose text is chosen by somebody outside this
# codebase -- ``correlation_id`` comes straight from a client request header,
# ``assistant_id`` from a client-supplied path, ``reviewer`` from whoever
# submitted an approval, ``cost_event_id`` from a producer. None of them is ever
# retained verbatim; each is replaced by a stable digest, which still correlates
# two records that shared one while carrying none of the text. ``cost_event_id``
# and ``cost_usd`` also live in typed :class:`NodeAuditRecord` columns, so the
# evidence survives the projection either way.
METADATA_KINDS: Mapping[str, MetadataKind] = {
    "assistant_id": MetadataKind.OPAQUE,
    "attempt": MetadataKind.NUMBER,
    "budget_cap_usd": MetadataKind.NUMBER,
    "budget_check_degraded": MetadataKind.BOOLEAN,
    "budget_spend_usd": MetadataKind.NUMBER,
    "compatibility_fingerprint": MetadataKind.DIGEST,
    "correlation_id": MetadataKind.OPAQUE,
    "cost_event_id": MetadataKind.OPAQUE,
    "cost_usd": MetadataKind.NUMBER,
    "decision": MetadataKind.LABEL,
    "disposition": MetadataKind.LABEL,
    "duration_ms": MetadataKind.NUMBER,
    "enforcement_applied": MetadataKind.BOOLEAN,
    "governance_level": MetadataKind.LABEL,
    "input_sha256": MetadataKind.DIGEST,
    "input_size_bytes": MetadataKind.NUMBER,
    "model_name": MetadataKind.LABEL,
    "node_kind": MetadataKind.LABEL,
    "operation": MetadataKind.LABEL,
    "output_sha256": MetadataKind.DIGEST,
    "output_size_bytes": MetadataKind.NUMBER,
    "policy_version": MetadataKind.LABEL,
    "provider": MetadataKind.LABEL,
    "reason_code": MetadataKind.LABEL,
    "retry_count": MetadataKind.NUMBER,
    "reviewer": MetadataKind.OPAQUE,
    "status": MetadataKind.LABEL,
    "upstream_status_code": MetadataKind.NUMBER,
}

# Derived, never maintained alongside: the two cannot drift apart.
ALLOWED_METADATA_KEYS = frozenset(METADATA_KINDS)


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


def key_digest(name: Any) -> str:
    """Stand a mapping key's text off with a short, stable digest of it.

    Args:
        name: A canonical mapping key -- always an exact ``str``, because
            :func:`canonicalize` gates keys before this is reached.

    Returns:
        A truncated SHA-256 of the key, or :data:`REDACTED` for anything that
        is not an exact ``str``. Producer-supplied key text never survives into
        a persisted summary, so a credential used as a key cannot be read back
        out of a schema.
    """
    if type(name) is not str:
        return REDACTED
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:_KEY_DIGEST_CHARS]


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
    if value is None or value_type is bool or value_type is int:
        return value
    if value_type is float:
        return value if math.isfinite(value) else REDACTED
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
        scrub: The policy's redaction chain, applied to the bounded labels this
            projection retains. It is a complement, not the guarantee: what
            makes the output content-free is that only allowlisted keys with
            values matching their declared kind survive at all.
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
        """Describe a canonical payload's shape: hashed key names and closed type names."""
        if depth >= MAX_DEPTH:
            return "..."
        if type(canonical) is dict:
            return {
                key_digest(key): self.schema(item, depth=depth + 1)
                for key, item in canonical.items()
            }
        if type(canonical) is list:
            # The first element stands for the list's shape; its length is in ``count``.
            return [self.schema(canonical[0], depth=depth + 1)] if canonical else []
        name = type(canonical).__name__
        return name if name in _SCHEMA_TYPE_NAMES else REDACTED

    def metadata(self, metadata: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Project free-form metadata onto the typed allowlist, summarizing what it drops.

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
            kind = METADATA_KINDS.get(key) if type(key) is str else None
            if kind is None:
                dropped += 1
                continue
            kept[key] = self.project(kind, value)
        summary = self.summarize(metadata)
        summary["dropped_keys"] = dropped
        return kept, summary

    def project(self, kind: MetadataKind, value: Any) -> Any:
        """Retain one allowlisted value only while it matches its key's declared kind."""
        value_type = type(value)
        if kind is MetadataKind.NUMBER:
            if value_type is int:
                return value
            if value_type is float and math.isfinite(value):
                return value
            return self.summarize(value)
        if kind is MetadataKind.BOOLEAN:
            return value if value_type is bool else self.summarize(value)
        if kind is MetadataKind.DIGEST:
            if value_type is str and _SAFE_DIGEST.match(value):
                return value
            return self.summarize(value)
        if kind is MetadataKind.LABEL:
            return self._label(value)
        # OPAQUE: chosen outside this codebase, so hashed rather than retained.
        return self.summarize(value)

    def _label(self, value: Any) -> Any:
        """Retain a bounded lower-case vocabulary term, summarizing anything else."""
        if type(value) is not str or len(value) > _MAX_LABEL_CHARS:
            return self.summarize(value)
        text = self._scrub(value)
        if type(text) is not str or not _SAFE_LABEL.match(text):
            return self.summarize(value)
        return text


__all__ = [
    "ALLOWED_METADATA_KEYS",
    "MAX_DEPTH",
    "METADATA_KINDS",
    "REDACTED",
    "ContentFreeProjection",
    "MetadataKind",
    "canonicalize",
    "digest",
    "entry_count",
    "key_digest",
    "safe_name",
    "type_name",
]
