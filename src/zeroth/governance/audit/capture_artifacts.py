"""The artifact addressing a metadata-only capture keeps, and nothing else.

Emptying a record's content channels without keeping the artifact keys it named
would orphan every blob an erased run produced:
:class:`~zeroth.governance.retention.erasure_service.RetentionErasureService`
harvests them straight out of the persisted record, so a record that no longer
names them is a record whose artifacts can never be destroyed. That is the whole
reason this stage retains anything at all here.

**The invariant: the only producer-supplied text that survives is a key this
codebase can prove it minted.** Retention used to run the record's payloads
through the platform's duck-typed reference scanner and persist the resulting
``ArtifactReference.model_dump()`` -- so ``store``, ``content_type``, ``size``,
``created_at``, ``ttl_seconds`` and a free-form nested ``metadata`` dict all rode
into a DEFAULT capture, and a seeded secret in any of them was persisted
verbatim. A ``{run_id}/`` prefix check was the only gate on the key itself, which
a producer satisfies by prefixing its own string. What survives now is a list of
key *strings*, each parsed against the complete grammar
:func:`~zeroth.platform.artifacts.models.generate_artifact_key` mints --
the legacy ``{run_id}/{node_id}/{uuid4hex}`` or explicit framed v1 grammar --
with the parsed ``run_id`` compared against the record's own identity rather
than against the payload. Every optional field is discarded, because erasure
addresses a blob by key and needs nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from zeroth.governance.audit.capture_projection import MAX_DEPTH
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.platform.artifacts.models import parse_generated_artifact_key

# Where the capture boundary files the keys it kept, inside its own marker.
ARTIFACT_KEYS_FIELD = "artifact_keys"

# A record names the artifacts it produced; it is not an artifact index.
MAX_RETAINED_ARTIFACT_KEYS = 64

# The field names that make a mapping an artifact reference, matching the
# platform scanner's duck-type. Used only to *find* candidates -- none of these
# fields other than ``key`` survives.
_REFERENCE_FIELDS = frozenset({"store", "key", "content_type", "size"})

# The two halves of the minted grammar that are not the run id. A node id is
# bounded and punctuated the way an identifier is; the suffix is a uuid4 hex.
_SAFE_NODE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._\-]{0,127}\Z")
_UUID_HEX = re.compile(r"\A[0-9a-f]{32}\Z")

# The channels a reference can be named in. ``execution_metadata`` is included
# because a producer may file one there; it is scanned, never trusted.
_SCANNED_FIELDS = (
    "input_snapshot",
    "output_snapshot",
    "validation_results",
    "execution_metadata",
)


def _is_owned_artifact_key(key: Any, *, run_id: str) -> bool:
    """Return whether ``key`` is a generated artifact key inside ``run_id``'s namespace.

    Args:
        key: The candidate key, of any type and any provenance.
        run_id: The record's own run id, which the key's first segment must equal.

    Returns:
        ``True`` only for an exact ``str`` of the complete
        generated legacy or framed-v1 grammar. A prefix match is not enough:
        ``run-1/anything-a-producer-wanted-to-file`` shares the prefix.
    """
    if type(key) is not str:
        return False
    parsed = parse_generated_artifact_key(key)
    if parsed is None:
        return False
    owner, node_id, suffix = parsed
    return (
        owner == run_id
        and _SAFE_NODE_ID.match(node_id) is not None
        and _UUID_HEX.match(suffix) is not None
    )


def _scan(value: Any, *, run_id: str, kept: list[str], seen: set[str], depth: int) -> None:
    """Collect owned artifact keys from one payload node, bounded in depth and count.

    Args:
        value: The payload node, arbitrarily shaped and producer-supplied.
        run_id: The record's own run id.
        kept: Accumulator of validated keys, in first-seen order.
        seen: Keys already accumulated, so a repeated reference is kept once.
        depth: Current recursion depth, bounded by
            :data:`~zeroth.governance.audit.capture_projection.MAX_DEPTH`.
    """
    if depth >= MAX_DEPTH or len(kept) >= MAX_RETAINED_ARTIFACT_KEYS:
        return
    if isinstance(value, Mapping):
        if _REFERENCE_FIELDS.issubset(value.keys()):
            key = value.get("key")
            if _is_owned_artifact_key(key, run_id=run_id) and key not in seen:
                seen.add(key)
                kept.append(key)
            return
        for item in value.values():
            _scan(item, run_id=run_id, kept=kept, seen=seen, depth=depth + 1)
        return
    if isinstance(value, list | tuple | set | frozenset):
        for item in value:
            _scan(item, run_id=run_id, kept=kept, seen=seen, depth=depth + 1)


def retained_artifact_keys(record: NodeAuditRecord) -> list[str]:
    """Keep the addressing of the artifacts a run owns, never their contents.

    Args:
        record: The record about to lose its content channels.

    Returns:
        Up to :data:`MAX_RETAINED_ARTIFACT_KEYS` deduplicated key strings, each
        one validated against the complete generated-key grammar and owned by
        this record's run. Everything else an artifact reference carried --
        ``store``, ``content_type``, ``size``, timestamps and its free-form
        ``metadata`` -- is discarded.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for name in _SCANNED_FIELDS:
        _scan(getattr(record, name), run_id=record.run_id, kept=kept, seen=seen, depth=0)
    return kept


__all__ = ["ARTIFACT_KEYS_FIELD", "MAX_RETAINED_ARTIFACT_KEYS", "retained_artifact_keys"]
