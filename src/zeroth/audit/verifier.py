"""Tamper-evident verification helpers for audit chains."""

from __future__ import annotations

import hashlib

from zeroth.audit.models import AuditContinuityReport, NodeAuditRecord
from zeroth.storage.json import to_json_value


class AuditContinuityVerifier:
    """Verify digest continuity for run- and deployment-scoped audit history."""

    def __init__(self, repository) -> None:  # noqa: ANN001
        self._repository = repository

    async def verify_run(self, run_id: str) -> AuditContinuityReport:
        records = await self._repository.list_by_run(run_id)
        return self._verify_records(scope=f"run:{run_id}", records=records)

    async def verify_deployment(self, deployment_ref: str) -> AuditContinuityReport:
        records = await self._repository.list_by_deployment(deployment_ref)
        if not records:
            return AuditContinuityReport(
                scope=f"deployment:{deployment_ref}",
                verified=True,
                record_count=0,
            )

        by_run: dict[str, list[NodeAuditRecord]] = {}
        for record in records:
            by_run.setdefault(record.run_id, []).append(record)

        total = 0
        for run_id in sorted(by_run):
            report = self._verify_records(scope=f"run:{run_id}", records=by_run[run_id])
            total += report.record_count
            if not report.verified:
                return AuditContinuityReport(
                    scope=f"deployment:{deployment_ref}",
                    verified=False,
                    record_count=total,
                    failed_audit_id=report.failed_audit_id,
                    error=report.error,
                )
        return AuditContinuityReport(
            scope=f"deployment:{deployment_ref}",
            verified=True,
            record_count=total,
        )

    def _verify_records(
        self,
        *,
        scope: str,
        records: list[NodeAuditRecord],
    ) -> AuditContinuityReport:
        previous_digest: str | None = None
        for record in records:
            if record.previous_record_digest != previous_digest:
                return AuditContinuityReport(
                    scope=scope,
                    verified=False,
                    record_count=len(records),
                    failed_audit_id=record.audit_id,
                    error="previous digest mismatch",
                )
            expected_digest = _compute_record_digest(record)
            if record.record_digest != expected_digest:
                return AuditContinuityReport(
                    scope=scope,
                    verified=False,
                    record_count=len(records),
                    failed_audit_id=record.audit_id,
                    error="record digest mismatch",
                )
            previous_digest = record.record_digest
        return AuditContinuityReport(scope=scope, verified=True, record_count=len(records))


def compute_chained_record(record: NodeAuditRecord, previous_digest: str | None) -> NodeAuditRecord:
    """Return a copy of a record with the chain fields filled in deterministically."""
    chained = record.model_copy(
        update={
            "previous_record_digest": previous_digest,
            "record_digest": None,
        }
    )
    return chained.model_copy(update={"record_digest": _compute_record_digest(chained)})


def _compute_record_digest(record: NodeAuditRecord) -> str:
    payload = record.model_copy(update={"record_digest": None}).model_dump(mode="json")
    return hashlib.sha256(to_json_value(payload).encode("utf-8")).hexdigest()
