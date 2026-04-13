"""Artifact reference extraction and TTL refresh helpers.

Provides utilities to scan serialized run state for ArtifactReference-shaped
dicts (duck-typing on store/key/content_type/size) and batch-refresh their
TTLs. Used by the orchestrator at checkpoint sites to extend artifact lifetimes
during long approval waits.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from zeroth.core.artifacts.errors import ArtifactTTLError
from zeroth.core.artifacts.models import ArtifactReference

if TYPE_CHECKING:
    from zeroth.core.artifacts.store import ArtifactStore

logger = logging.getLogger(__name__)

# Fields that must all be present for duck-type matching.
_REQUIRED_FIELDS = frozenset({"store", "key", "content_type", "size"})

# Safety bound: warn if more than this many refs found in a single scan (T-34-07).
_MAX_REFS_WARNING = 1000


def extract_artifact_refs(data: dict[str, Any]) -> list[ArtifactReference]:
    """Recursively scan a dict for ArtifactReference-shaped objects.

    A nested dict is considered an ArtifactReference candidate if it contains
    ALL FOUR required fields: ``store``, ``key``, ``content_type``, ``size``.
    Candidates are validated via ``ArtifactReference.model_validate()``; any
    that fail validation (e.g. wrong types) are silently skipped.

    Args:
        data: A dict (typically from run execution history output_snapshot)
              to scan for artifact references.

    Returns:
        List of validated ArtifactReference objects found at any nesting depth.
    """
    refs: list[ArtifactReference] = []
    _scan(data, refs)
    if len(refs) > _MAX_REFS_WARNING:
        logger.warning(
            "Found %d artifact references in scan (threshold %d); "
            "this may indicate excessive artifact generation",
            len(refs),
            _MAX_REFS_WARNING,
        )
    return refs


def _scan(obj: Any, refs: list[ArtifactReference]) -> None:
    """Recursive scanner that walks dicts and lists looking for artifact refs."""
    if isinstance(obj, dict):
        # Check if this dict itself is an artifact reference.
        if _REQUIRED_FIELDS.issubset(obj.keys()):
            try:
                ref = ArtifactReference.model_validate(obj)
                refs.append(ref)
                return  # Don't recurse into a matched ref
            except ValidationError:
                pass  # Not a valid ArtifactReference despite having the fields
        # Recurse into values.
        for value in obj.values():
            _scan(value, refs)
    elif isinstance(obj, list):
        for item in obj:
            _scan(item, refs)


async def refresh_artifact_ttls(
    artifact_store: ArtifactStore | None,
    data: dict[str, Any],
    ttl: int,
) -> int:
    """Extract artifact refs from data and refresh their TTLs.

    This is the main integration point called by the orchestrator after
    ``write_checkpoint``. It scans the provided data for ArtifactReference
    shapes and refreshes each one's TTL on the store.

    When ``artifact_store`` is None, this is a no-op (returns 0). Individual
    ``ArtifactTTLError`` exceptions per ref are caught and logged -- a single
    expired artifact does not prevent refreshing the rest.

    Args:
        artifact_store: The artifact store backend, or None.
        data: Dict to scan for artifact references (typically combined
              output_snapshots from run execution history).
        ttl: New TTL in seconds to set on each found artifact.

    Returns:
        Count of artifacts whose TTL was successfully refreshed.
    """
    if artifact_store is None:
        return 0

    refs = extract_artifact_refs(data)
    if not refs:
        return 0

    refreshed = 0
    for ref in refs:
        try:
            await artifact_store.refresh_ttl(ref.key, ttl)
            refreshed += 1
        except ArtifactTTLError:
            logger.debug(
                "Skipping TTL refresh for expired/missing artifact: %s",
                ref.key,
            )
    return refreshed
