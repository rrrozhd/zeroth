"""Timeline assembly for audit records.

Provides the AuditTimelineAssembler that takes a collection of audit
records and puts them in chronological order, producing an AuditTimeline
you can step through to understand what happened during a run.
"""

from __future__ import annotations

from collections.abc import Sequence

from zeroth.core.audit.models import AuditTimeline, NodeAuditRecord


class AuditTimelineAssembler:
    """Builds a time-ordered timeline from a collection of audit records.

    Use this when you have a bunch of audit records and want to see them
    in the order they actually happened, like a replay of the run.
    """

    def assemble(self, records: Sequence[NodeAuditRecord]) -> AuditTimeline:
        """Sort the given records by time and return them as an AuditTimeline.

        Records are ordered by their start time, with ties broken by audit_id
        to keep the ordering stable and predictable.
        """
        ordered = sorted(records, key=lambda record: (record.started_at, record.audit_id))
        run_id = ordered[0].run_id if ordered else None
        return AuditTimeline(run_id=run_id, entries=list(ordered))
