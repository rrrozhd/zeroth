"""Review-friendly evidence bundle builders."""

from __future__ import annotations

import base64
import copy
import logging
from typing import Any

from zeroth.core.audit.models import NodeAuditRecord

logger = logging.getLogger(__name__)

# Fields that identify an ArtifactReference-shaped dict.
_ARTIFACT_REF_FIELDS = frozenset({"store", "key", "content_type", "size"})


def build_summary(
    audits: list[NodeAuditRecord],
    approvals: list[object],
    *,
    resolve_artifacts: bool = False,
    artifact_store: Any | None = None,
) -> dict[str, int | bool]:
    """Summarize the key governance signals in a bundle.

    When ``resolve_artifacts`` is True and an ``artifact_store`` is provided,
    the summary includes an ``artifacts_resolved`` flag to indicate that
    artifact payloads have been resolved in the evidence export.
    """
    result: dict[str, int | bool] = {
        "audit_count": len(audits),
        "approval_count": len(approvals),
        "tool_call_count": sum(len(record.tool_calls) for record in audits),
        "memory_interaction_count": sum(len(record.memory_interactions) for record in audits),
    }
    if resolve_artifacts and artifact_store is not None:
        result["artifacts_resolved"] = True
    return result


def collect_policy_events(audits: list[NodeAuditRecord]) -> list[str]:
    """Extract policy and authorization failures into a review-friendly list."""
    events: list[str] = []
    for record in audits:
        if not record.error:
            continue
        if "denied" in record.error or "forbidden" in record.error or "policy" in record.error:
            events.append(record.error)
    return events


async def resolve_artifact_references(
    audits: list[NodeAuditRecord],
    artifact_store: Any,
) -> list[NodeAuditRecord]:
    """Resolve ArtifactReferences in audit records to full base64-encoded payloads.

    For each audit record, scans ``output_snapshot`` for ArtifactReference-shaped
    dicts. When found, calls ``artifact_store.retrieve(ref["key"])`` and replaces
    the reference dict with a resolved payload dict containing the base64-encoded
    data, content_type, and size.

    Returns a new list of records with resolved payloads. Does NOT mutate originals.

    Only resolves when explicitly called -- default audit output never auto-resolves
    (T-34-06 mitigation).

    Args:
        audits: List of audit records whose output_snapshots may contain refs.
        artifact_store: The artifact store backend (must have async retrieve method).

    Returns:
        New list of NodeAuditRecord with artifact references replaced by payloads.
    """
    resolved_audits: list[NodeAuditRecord] = []
    for audit in audits:
        resolved_snapshot = await _resolve_snapshot(audit.output_snapshot, artifact_store)
        resolved_audit = audit.model_copy(update={"output_snapshot": resolved_snapshot})
        resolved_audits.append(resolved_audit)
    return resolved_audits


async def _resolve_snapshot(snapshot: dict[str, Any], artifact_store: Any) -> dict[str, Any]:
    """Recursively resolve ArtifactReference-shaped dicts in a snapshot."""
    result = copy.deepcopy(snapshot)
    await _resolve_in_place(result, artifact_store)
    return result


async def _resolve_in_place(obj: Any, artifact_store: Any) -> None:
    """Walk a dict/list structure and replace artifact refs with resolved payloads."""
    if isinstance(obj, dict):
        keys_to_resolve = []
        for key, value in obj.items():
            if isinstance(value, dict) and _ARTIFACT_REF_FIELDS.issubset(value.keys()):
                keys_to_resolve.append(key)
            elif isinstance(value, (dict, list)):
                await _resolve_in_place(value, artifact_store)

        for key in keys_to_resolve:
            ref = obj[key]
            try:
                payload = await artifact_store.retrieve(ref["key"])
                obj[key] = {
                    "_resolved_artifact": base64.b64encode(payload).decode(),
                    "content_type": ref["content_type"],
                    "size": ref["size"],
                }
            except Exception:
                logger.debug("Failed to resolve artifact ref: %s", ref.get("key"))

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                await _resolve_in_place(item, artifact_store)
