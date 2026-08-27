"""Read-only recovery of one previously authorized provider control probe.

This module is deliberately not an executor.  It opens only the service and
economics SQLite databases in read-only mode and reconstructs a
``PaidProbeResult`` when all durable identities agree exactly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from .control_gate import PaidProbeResult, ProbeKind

_IDENTIFIER = re.compile(r"^[^\s\x00-\x1f\x7f]{1,512}$")


class PaidProbeOutcomeError(RuntimeError):
    """The durable planes cannot prove one exact committed probe outcome."""


def _identity(value: object, reason: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PaidProbeOutcomeError(reason)
    return value


def _money(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PaidProbeOutcomeError("cost_identity_mismatch") from None
    if not amount.is_finite() or amount < 0:
        raise PaidProbeOutcomeError("cost_identity_mismatch")
    return amount


def _optional_provider_identity(value: object) -> str | None:
    if value is None:
        return None
    return _identity(value, "provider_request_identity_mismatch")


def _provider_identities(value: object) -> tuple[str | None, ...]:
    found: list[str | None] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "provider_request_id":
                found.append(_optional_provider_identity(item))
            else:
                found.extend(_provider_identities(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_provider_identities(item))
    return tuple(found)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{quote(str(resolved), safe='/')}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    return connection


class AuthoritativePaidProbeOutcomeCollector:
    """Collect one exact provider-probe result without network or database writes."""

    def __init__(
        self,
        *,
        service_database: Path,
        economics_database: Path,
        tenant_id: str,
        campaign_id: str,
        operation_id: str,
        run_id: str,
        kind: ProbeKind = "provider",
    ) -> None:
        self._service_database = service_database
        self._economics_database = economics_database
        self._tenant_id = _identity(tenant_id, "scope_identity_invalid")
        self._campaign_id = _identity(campaign_id, "scope_identity_invalid")
        self._operation_id = _identity(operation_id, "scope_identity_invalid")
        self._run_id = _identity(run_id, "scope_identity_invalid")
        if kind not in {"provider", "chroma"}:
            raise ValueError("unsupported paid probe kind")
        self._kind = kind

    def collect(self) -> PaidProbeResult:
        try:
            with closing(_connect_read_only(self._economics_database)) as economics:
                reservation_rows = economics.execute(
                    """SELECT status, actual_cost_usd, cost_event_id,
                              provider_request_id, cleanup_status
                       FROM cost_reservations
                       WHERE tenant_id = ? AND campaign_id = ?
                         AND operation_id = ? AND run_id = ?""",
                    (
                        self._tenant_id,
                        self._campaign_id,
                        self._operation_id,
                        self._run_id,
                    ),
                ).fetchall()
                if len(reservation_rows) != 1:
                    raise PaidProbeOutcomeError("reservation_not_unique")
                reservation = reservation_rows[0]
                if reservation["status"] != "committed":
                    raise PaidProbeOutcomeError("reservation_not_committed")
                cost_event_id = _identity(reservation["cost_event_id"], "cost_identity_mismatch")
                measured_cost = _money(reservation["actual_cost_usd"])
                provider_request_id = _optional_provider_identity(
                    reservation["provider_request_id"]
                )
                reservation_cleanup = _identity(
                    reservation["cleanup_status"], "cleanup_identity_mismatch"
                )
                if reservation_cleanup not in {"complete", "committed"}:
                    raise PaidProbeOutcomeError("cleanup_identity_mismatch")

                execution_rows = economics.execute(
                    """SELECT execution_id, provider_request_id, cleanup_status,
                              token_cost_usd, tool_cost_usd, compute_cost_usd, metadata
                       FROM execution_events
                       WHERE tenant_id = ? AND campaign_id = ? AND operation_id = ?""",
                    (self._tenant_id, self._campaign_id, self._operation_id),
                ).fetchall()
                if len(execution_rows) != 1:
                    raise PaidProbeOutcomeError("execution_not_unique")
                execution = execution_rows[0]
                try:
                    metadata = json.loads(execution["metadata"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise PaidProbeOutcomeError("execution_metadata_invalid") from None
                if not isinstance(metadata, dict) or any(
                    metadata.get(name) != expected
                    for name, expected in (
                        ("tenant_id", self._tenant_id),
                        ("campaign_id", self._campaign_id),
                        ("operation_id", self._operation_id),
                        ("run_id", self._run_id),
                        ("probe", True),
                    )
                ):
                    raise PaidProbeOutcomeError("execution_scope_mismatch")
                if execution["execution_id"] != cost_event_id:
                    raise PaidProbeOutcomeError("cost_identity_mismatch")
                execution_cost = sum(
                    (
                        _money(value) if value is not None else Decimal("0")
                        for value in (
                            execution["token_cost_usd"],
                            execution["tool_cost_usd"],
                            execution["compute_cost_usd"],
                        )
                    ),
                    Decimal("0"),
                )
                if execution_cost != measured_cost:
                    raise PaidProbeOutcomeError("cost_identity_mismatch")
                execution_provider = _optional_provider_identity(execution["provider_request_id"])
                metadata_providers = _provider_identities(metadata)
                if (
                    execution_provider != provider_request_id
                    or not metadata_providers
                    or any(value != provider_request_id for value in metadata_providers)
                ):
                    raise PaidProbeOutcomeError("provider_request_identity_mismatch")
                if execution["cleanup_status"] != reservation_cleanup:
                    raise PaidProbeOutcomeError("cleanup_identity_mismatch")

            with closing(_connect_read_only(self._service_database)) as service:
                audit_rows = service.execute(
                    """SELECT audit_id, record_json, cost_usd, cost_event_id,
                              chain_sequence
                       FROM node_audits WHERE tenant_id = ? AND run_id = ?""",
                    (self._tenant_id, self._run_id),
                ).fetchall()
                if len(audit_rows) != 1:
                    raise PaidProbeOutcomeError("audit_not_unique")
                audit = audit_rows[0]
                audit_event_id = _identity(audit["audit_id"], "audit_identity_mismatch")
                try:
                    record = json.loads(audit["record_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise PaidProbeOutcomeError("audit_record_invalid") from None
                if not isinstance(record, dict) or any(
                    record.get(name) != expected
                    for name, expected in (
                        ("audit_id", audit_event_id),
                        ("run_id", self._run_id),
                        ("tenant_id", self._tenant_id),
                        ("campaign_id", self._campaign_id),
                        ("cost_event_id", cost_event_id),
                        ("status", "completed"),
                    )
                ):
                    raise PaidProbeOutcomeError("audit_identity_mismatch")
                if audit["cost_event_id"] != cost_event_id:
                    raise PaidProbeOutcomeError("cost_identity_mismatch")
                if (
                    _money(audit["cost_usd"]) != measured_cost
                    or _money(record.get("cost_usd")) != measured_cost
                ):
                    raise PaidProbeOutcomeError("cost_identity_mismatch")
                if audit["chain_sequence"] != 1 or record.get("chain_sequence") != 1:
                    raise PaidProbeOutcomeError("audit_chain_invalid")
                for field in (
                    "record_digest",
                    "record_signature",
                    "signing_algorithm",
                    "signing_key_id",
                ):
                    _identity(record.get(field), "audit_not_signed")
                audit_providers = _provider_identities(record)
                if any(value != provider_request_id for value in audit_providers):
                    raise PaidProbeOutcomeError("provider_request_identity_mismatch")
        except PaidProbeOutcomeError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise PaidProbeOutcomeError("authoritative_database_unavailable") from exc

        if self._kind == "chroma" and provider_request_id is None:
            raise PaidProbeOutcomeError("provider_request_identity_mismatch")
        return PaidProbeResult(
            kind=self._kind,
            operation_id=self._operation_id,
            run_id=self._run_id,
            audit_event_id=audit_event_id,
            cost_event_id=cost_event_id,
            provider_request_id=provider_request_id,
            connector_request_id=(provider_request_id if self._kind == "chroma" else None),
            request_count=1,
            cache_hit=False,
            audit_chain_signed=True,
            cleanup_state="committed",
            measured_cost_usd=measured_cost,
        )
