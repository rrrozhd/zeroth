"""Typed failures raised by the audit write path.

One exception lives here, and it exists to be *caught narrowly*. The
append-only write refuses a record for two very different reasons -- the
``audit_id`` is already stored (the event is durable) or the record failed
validation before the commit (the event is not stored at all) -- and a caller
that cannot tell those apart will report the second as the first.

:class:`DuplicateAuditIdError` subclasses :class:`ValueError` deliberately.
``AuditRepository.write`` is on the hot durable path for the whole runtime and
several callers already catch ``ValueError`` around it; keeping the subclass
relationship means none of them change behaviour, while a caller that needs the
distinction -- :class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue`,
which counts a duplicate as *delivered* -- can narrow to the exact type.
"""

from __future__ import annotations


class DuplicateAuditIdError(ValueError):
    """The append-only audit write refused an ``audit_id`` that is already stored.

    Raised only after the duplicate check finds an existing row, so it carries
    exactly one meaning: this record is already durable. Every other failure of
    the write -- validation, serialization, storage, transport -- raises
    something else, and must never be read as a successful delivery.
    """


__all__ = ["DuplicateAuditIdError"]
