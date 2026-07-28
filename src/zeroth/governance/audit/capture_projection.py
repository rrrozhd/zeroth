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
was persisted verbatim inside a dropped-content schema. A canonical mapping key
is now :func:`key_digest` -- a truncated SHA-256 of the key -- so two payloads
that shared a key still look alike and no producer text survives. Type names in
a schema come from a closed set (:data:`_SCHEMA_TYPE_NAMES`), because
:meth:`ContentFreeProjection.schema` only ever reads canonical values and a
dynamically constructed class can carry any ``__name__`` it likes.

**A canonical mapping is a sequence of entries, not a mapping.** Rendering keys
to fixed markers made distinct keys collide: every unsafe or non-string key
became the same ``***REDACTED***`` key, so two submitted entries overwrote each
other and the record claimed a count of one and a schema missing a branch --
R4's retained hashes and counts, wrong. Canonicalizing to ``{"<entries>":
[[key, value], ...]}`` removes the collapse entirely: an entry is a list
element, so it cannot be overwritten by a sibling whatever its key rendered to.
Keys are hashed when they are exact strings and become a closed
per-type marker otherwise, and the entries are sorted by their own rendering so
two equal payloads still digest alike.

**Metadata is a typed allowlist, not a scrub.** ``execution_metadata`` and
approval metadata are free-form ``dict[str, Any]`` that producers fill with
whatever they were holding, and neither key-based redaction nor an allowlist of
key *names* closes that. Each allowlisted key declares a
:class:`~zeroth.governance.audit.capture_vocabulary.MetadataKind`, and
:meth:`ContentFreeProjection.project` here is the enforcement of it: a value
that does not fit its key's kind is summarized rather than retained. What the
kinds mean, and why a vocabulary replaced the old label-shape check, is argued
in :mod:`~zeroth.governance.audit.capture_vocabulary`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from zeroth.governance.audit.capture_vocabulary import (
    ALLOWED_METADATA_KEYS as ALLOWED_METADATA_KEYS,
)
from zeroth.governance.audit.capture_vocabulary import (
    METADATA_KINDS,
    METADATA_VOCABULARIES,
    MetadataKind,
)

REDACTED = "***REDACTED***"

# Bounds every walk here, so a deep or cyclic payload cannot exhaust the stack
# inside the delivery worker.
MAX_DEPTH = 6
# A key name is schema; a string long enough to be a credential is not.
_MAX_NAME_CHARS = 64
# Identifier-shaped: what a key name looks like when it is a name.
_SAFE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.:\-]*\Z")
# An identifier this codebase minted (a run-scoped branch id, a join key):
# lower-case, bounded, punctuated only by the separators an id uses.
_MAX_IDENTIFIER_CHARS = 128
_SAFE_IDENTIFIER = re.compile(r"\A[a-z0-9][a-z0-9._:\-]*\Z")
# A hash, optionally prefixed by its algorithm ("sha256:...").
_SAFE_DIGEST = re.compile(r"\A(?:[a-z0-9]{1,16}:)?[0-9a-f]{16,128}\Z")
# How much of a key's SHA-256 stands in for the key in a canonical rendering.
_KEY_DIGEST_CHARS = 16
# The only type names a schema can carry, because schemas read canonical values.
_SCHEMA_TYPE_NAMES = frozenset({"NoneType", "bool", "int", "float", "str", "dict", "list"})
# The single key a canonical mapping carries; its value is the entry sequence.
ENTRIES_KEY = "<entries>"
# Closed markers for mapping keys that are not exact strings. Matched by
# identity on the exact type, never by name: a class may be *called* ``int``.
_KEY_TYPE_MARKERS: tuple[tuple[type, str], ...] = (
    (type(None), "<key:none>"),
    (bool, "<key:bool>"),
    (int, "<key:int>"),
    (float, "<key:float>"),
    (tuple, "<key:tuple>"),
    (bytes, "<key:bytes>"),
    (frozenset, "<key:frozenset>"),
)
_OTHER_KEY_MARKER = "<key:other>"


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
        name: A candidate mapping key.

    Returns:
        A truncated SHA-256 of the key, or :data:`REDACTED` for anything that
        is not an exact ``str``. Producer-supplied key text never survives into
        a persisted summary, so a credential used as a key cannot be read back
        out of a schema.
    """
    if type(name) is not str:
        return REDACTED
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:_KEY_DIGEST_CHARS]


def canonical_key(key: Any) -> str:
    """Render one mapping key as a hashed exact string or a closed type marker.

    Args:
        key: The producer-supplied key, of any type.

    Returns:
        :func:`key_digest` of the key when it is an exact ``str``, else a marker
        naming only the key's exact type. Two keys may render alike; entries are
        a sequence, so alike renderings no longer overwrite one another.
    """
    key_type = type(key)
    if key_type is str:
        return key_digest(key)
    for candidate, marker in _KEY_TYPE_MARKERS:
        if key_type is candidate:
            return marker
    return _OTHER_KEY_MARKER


def _ordering(item: Any) -> str:
    """Order canonical siblings by their own rendering, never by their type's."""
    return json.dumps(item, sort_keys=True)


def canonical_entries(canonical: Any) -> list[Any] | None:
    """Return a canonical mapping's entry sequence, or ``None`` for anything else."""
    if type(canonical) is not dict or len(canonical) != 1:
        return None
    entries = canonical.get(ENTRIES_KEY)
    return entries if type(entries) is list else None


def canonicalize(value: Any, *, depth: int = 0) -> Any:
    """Render any payload as JSON-safe data, dispatching on type and never on behaviour.

    Args:
        value: The payload to render. Arbitrarily shaped and producer-supplied.
        depth: Current recursion depth, bounded by :data:`MAX_DEPTH`.

    Returns:
        A structure of ``None``, ``bool``, ``int``, ``float``, ``str``, ``list``
        and entry-sequence mappings -- so :func:`json.dumps` on it cannot raise,
        and nothing in it was produced by ``__str__`` or ``__repr__``.
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
        entries = [
            [canonical_key(key), canonicalize(item, depth=depth + 1)] for key, item in value.items()
        ]
        return {ENTRIES_KEY: sorted(entries, key=_ordering)}
    if isinstance(value, list | tuple):
        return [canonicalize(item, depth=depth + 1) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((canonicalize(item, depth=depth + 1) for item in value), key=_ordering)
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
    entries = canonical_entries(canonical)
    if entries is not None:
        return len(entries)
    if type(canonical) is list or type(canonical) is str:
        return len(canonical)
    return 1


class ContentFreeProjection:
    """Describe dropped payloads as digests, schemas and counts.

    Args:
        scrub: The policy's redaction chain, applied to the bounded identifiers
            this projection retains. It is a complement, not the guarantee: what
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
        """Describe a canonical payload's shape: rendered keys and closed type names."""
        if depth >= MAX_DEPTH:
            return "..."
        entries = canonical_entries(canonical)
        if entries is not None:
            return {
                ENTRIES_KEY: [[key, self.schema(item, depth=depth + 1)] for key, item in entries]
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
            kept[key] = self.project(key, kind, value)
        summary = self.summarize(metadata)
        summary["dropped_keys"] = dropped
        return kept, summary

    def project(self, key: str, kind: MetadataKind, value: Any) -> Any:
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
        if kind is MetadataKind.VOCABULARY:
            terms = METADATA_VOCABULARIES.get(key)
            if terms is not None and value_type is str and value in terms:
                return value
            return self.summarize(value)
        if kind is MetadataKind.IDENTIFIER:
            return self._bounded(value, _MAX_IDENTIFIER_CHARS, _SAFE_IDENTIFIER)
        # OPAQUE: chosen outside this codebase, so hashed rather than retained.
        return self.summarize(value)

    def _bounded(self, value: Any, limit: int, shape: re.Pattern[str]) -> Any:
        """Retain a bounded lower-case term matching ``shape``, summarizing anything else."""
        if type(value) is not str or len(value) > limit:
            return self.summarize(value)
        text = self._scrub(value)
        if type(text) is not str or not shape.match(text):
            return self.summarize(value)
        return text


__all__ = [
    "ALLOWED_METADATA_KEYS",
    "ENTRIES_KEY",
    "MAX_DEPTH",
    "METADATA_KINDS",
    "REDACTED",
    "ContentFreeProjection",
    "MetadataKind",
    "canonical_entries",
    "canonical_key",
    "canonicalize",
    "digest",
    "entry_count",
    "key_digest",
    "safe_name",
    "type_name",
]
