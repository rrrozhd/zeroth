"""Review-friendly evidence bundle builders."""

from __future__ import annotations

from zeroth.audit.models import NodeAuditRecord


def build_summary(
    audits: list[NodeAuditRecord],
    approvals: list[object],
) -> dict[str, int]:
    """Summarize the key governance signals in a bundle."""
    return {
        "audit_count": len(audits),
        "approval_count": len(approvals),
        "tool_call_count": sum(len(record.tool_calls) for record in audits),
        "memory_interaction_count": sum(len(record.memory_interactions) for record in audits),
    }


def collect_policy_events(audits: list[NodeAuditRecord]) -> list[str]:
    """Extract policy and authorization failures into a review-friendly list."""
    events: list[str] = []
    for record in audits:
        if not record.error:
            continue
        if "denied" in record.error or "forbidden" in record.error or "policy" in record.error:
            events.append(record.error)
    return events
