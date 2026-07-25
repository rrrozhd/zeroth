"""Review-friendly evidence bundle builders.

**Denials are read from structural fields, never from ``error`` text.**
:func:`collect_policy_events` used to substring-match ``denied``/``forbidden``/
``policy`` against ``record.error``. That channel is free-form prose an
exception authors, and
:class:`~zeroth.governance.audit.capture_policy.AuditCapturePolicy` now replaces
it with a fixed redaction marker before the durable write -- so the matcher saw
the marker, matched nothing, and both ``/evidence`` endpoints returned an
always-empty ``policy_events`` list. Denial producers promote their verdict onto
allowlisted ``execution_metadata`` keys (``admitted``, ``decision``,
``enforcement_applied``, ``reason_code``) that survive the capture, and this
module reads those.
"""

from __future__ import annotations

import base64
import copy
import logging
from collections.abc import Mapping
from typing import Any

from zeroth.governance.audit.models import NodeAuditRecord

logger = logging.getLogger(__name__)

# Fields that identify an ArtifactReference-shaped dict.
_ARTIFACT_REF_FIELDS = frozenset({"store", "key", "content_type", "size"})
# Values of the allowlisted ``decision`` key that mean "this attempt was stopped".
_DENIED_DECISIONS = frozenset({"deny", "denied", "block", "blocked", "reject", "rejected"})
# The record status a governance rejection is persisted under.
_REJECTED_STATUS = "rejected"


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


def _denied(record: NodeAuditRecord, metadata: Mapping[str, Any]) -> bool:
    """Answer whether one record's structural fields say the attempt was stopped."""
    if metadata.get("admitted") is False or metadata.get("enforcement_applied") is False:
        return True
    decision = metadata.get("decision")
    if type(decision) is str and decision in _DENIED_DECISIONS:
        return True
    return record.status == _REJECTED_STATUS


def _policy_event(record: NodeAuditRecord, metadata: Mapping[str, Any]) -> str:
    """Describe one denial from structural fields only -- never from ``error`` prose."""
    decision = metadata.get("decision")
    outcome = decision if type(decision) is str else record.status
    reason = metadata.get("reason_code")
    detail = f": {reason}" if type(reason) is str and reason else ""
    return f"{record.node_id} {outcome}{detail}"


def collect_policy_events(audits: list[NodeAuditRecord]) -> list[str]:
    """Extract policy and authorization failures into a review-friendly list.

    Args:
        audits: The bundle's audit records, already capture-transformed.

    Returns:
        One line per denied attempt, naming the node, the decision and the
        normalized reason code. Every part comes from an allowlisted metadata
        key or a typed column, so the list survives a metadata-only capture and
        carries no producer text.
    """
    events: list[str] = []
    for record in audits:
        metadata = record.execution_metadata
        if not isinstance(metadata, Mapping) or not _denied(record, metadata):
            continue
        events.append(_policy_event(record, metadata))
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
