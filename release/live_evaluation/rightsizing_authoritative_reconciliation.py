"""Authoritative persisted-plane reconciliation for a Rightsizing service run."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from .reconciliation import (
    ActionReceiptRecord,
    AuditRecord,
    LocalCostEvent,
    ProviderWindowSummary,
    ReconciliationInput,
    RegulusExecutionEvent,
    ReservationRecord,
)
from .reconciliation_export import (
    AuditSource,
    AuthoritativeCampaignExporter,
    AuthoritativeExportBlocked,
)
from .rightsizing_service_adapter import ServiceExperimentIdentity

_EXPORT_KEYS = {
    "schema_version",
    "campaign_id",
    "tenant_id",
    "audits",
    "reservations",
    "local_cost_events",
    "regulus_events",
    "action_receipts",
    "excluded_reservations",
    "provider_window",
}
_MONEY_FIELDS = {
    "cost_usd",
    "maximum_usd",
    "retained_usd",
    "amount_usd",
    "failure_tax_usd",
    "value_usd",
    "margin_usd",
}
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class RightsizingReconciliationBlocked(ValueError):  # noqa: N818
    """A stable, non-secret reason why persisted reconciliation cannot pass."""

    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_CODE.fullmatch(code) else "authoritative_export_failed"
        super().__init__(self.code)


CampaignRunInventory = Callable[[str, str], Sequence[str]]


def _row(
    value: object,
    record_type: type[Any],
    *,
    campaign_id: str,
    tenant_id: str,
) -> Any:
    if not isinstance(value, Mapping):
        raise RightsizingReconciliationBlocked("authoritative_export_invalid")
    expected = {field.name for field in fields(record_type)} | {"campaign_id", "tenant_id"}
    if (
        set(value) != expected
        or value.get("campaign_id") != campaign_id
        or value.get("tenant_id") != tenant_id
    ):
        raise RightsizingReconciliationBlocked("authoritative_export_invalid")
    arguments = {
        key: Decimal(str(item)) if key in _MONEY_FIELDS else item
        for key, item in value.items()
        if key not in {"campaign_id", "tenant_id"}
    }
    try:
        return record_type(**arguments)
    except (TypeError, ValueError, ArithmeticError):
        raise RightsizingReconciliationBlocked("authoritative_export_invalid") from None


def _rows(
    payload: Mapping[str, object],
    name: str,
    record_type: type[Any],
    *,
    campaign_id: str,
    tenant_id: str,
) -> tuple[Any, ...]:
    values = payload.get(name)
    if not isinstance(values, list):
        raise RightsizingReconciliationBlocked("authoritative_export_invalid")
    return tuple(
        _row(
            value,
            record_type,
            campaign_id=campaign_id,
            tenant_id=tenant_id,
        )
        for value in values
    )


def _exact(actual: Sequence[tuple[object, ...]], expected: Sequence[tuple[object, ...]]) -> None:
    if Counter(actual) != Counter(expected):
        raise RightsizingReconciliationBlocked("service_identity_mismatch")


class AuthoritativeRightsizingReconciliationCollector:
    """Adapt ``AuthoritativeCampaignExporter`` to one measured service response.

    The exporter first validates the full tagged campaign across its durable
    SQLite, signed-audit, action-sink, and provider-window sources. Only after
    that validation does this collector select the exact target run identified
    by the service response.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        econ_database: Path,
        action_sink_database: Path,
        audit_source: AuditSource,
        provider_window: Path,
        run_inventory: CampaignRunInventory | None = None,
    ) -> None:
        if not tenant_id or any(ch.isspace() or ord(ch) < 32 for ch in tenant_id):
            raise ValueError("tenant_id must be a nonblank opaque identifier")
        self._tenant_id = tenant_id
        self._econ_database = econ_database
        self._action_sink_database = action_sink_database
        self._audit_source = audit_source
        self._provider_window = provider_window
        self._run_inventory = run_inventory

    def _run_ids(self, identity: ServiceExperimentIdentity) -> tuple[str, ...]:
        if self._run_inventory is None:
            return (identity.run_id,)
        try:
            values = self._run_inventory(identity.campaign_id, self._tenant_id)
        except Exception:
            raise RightsizingReconciliationBlocked("campaign_run_inventory_unavailable") from None
        if isinstance(values, (str, bytes)):
            raise RightsizingReconciliationBlocked("campaign_run_inventory_invalid")
        run_ids = tuple(values)
        if (
            not run_ids
            or len(run_ids) != len(set(run_ids))
            or any(
                not isinstance(run_id, str)
                or not run_id
                or any(ch.isspace() or ord(ch) < 32 for ch in run_id)
                for run_id in run_ids
            )
        ):
            raise RightsizingReconciliationBlocked("campaign_run_inventory_invalid")
        if identity.run_id not in run_ids:
            raise RightsizingReconciliationBlocked("campaign_run_inventory_incomplete")
        return run_ids

    def collect(self, identity: ServiceExperimentIdentity) -> ReconciliationInput:
        run_ids = self._run_ids(identity)
        try:
            raw = AuthoritativeCampaignExporter(
                econ_database=self._econ_database,
                action_sink_database=self._action_sink_database,
                campaign_id=identity.campaign_id,
                tenant_id=self._tenant_id,
                run_ids=run_ids,
                audit_source=self._audit_source,
                provider_window=self._provider_window,
            ).export()
        except AuthoritativeExportBlocked as exc:
            raise RightsizingReconciliationBlocked(exc.code) from None
        except Exception:
            raise RightsizingReconciliationBlocked("authoritative_export_failed") from None
        if (
            not isinstance(raw, Mapping)
            or set(raw) != _EXPORT_KEYS
            or raw.get("schema_version") != 1
            or raw.get("campaign_id") != identity.campaign_id
            or raw.get("tenant_id") != self._tenant_id
        ):
            raise RightsizingReconciliationBlocked("authoritative_export_invalid")

        audits = _rows(
            raw,
            "audits",
            AuditRecord,
            campaign_id=identity.campaign_id,
            tenant_id=self._tenant_id,
        )
        reservations = _rows(
            raw,
            "reservations",
            ReservationRecord,
            campaign_id=identity.campaign_id,
            tenant_id=self._tenant_id,
        )
        local = _rows(
            raw,
            "local_cost_events",
            LocalCostEvent,
            campaign_id=identity.campaign_id,
            tenant_id=self._tenant_id,
        )
        regulus = _rows(
            raw,
            "regulus_events",
            RegulusExecutionEvent,
            campaign_id=identity.campaign_id,
            tenant_id=self._tenant_id,
        )
        receipts = _rows(
            raw,
            "action_receipts",
            ActionReceiptRecord,
            campaign_id=identity.campaign_id,
            tenant_id=self._tenant_id,
        )
        target_audits = tuple(row for row in audits if row.run_id == identity.run_id)
        target_reservations = tuple(row for row in reservations if row.run_id == identity.run_id)
        target_local = tuple(row for row in local if row.run_id == identity.run_id)
        target_regulus = tuple(row for row in regulus if row.run_id == identity.run_id)
        target_receipts = tuple(row for row in receipts if row.run_id == identity.run_id)
        if target_receipts:
            raise RightsizingReconciliationBlocked("unexpected_rightsizing_action_receipt")

        expected_calls = tuple(
            (
                call.audit_event_id,
                call.operation_id,
                identity.run_id,
                call.cost_event_id,
                call.provider_request_id,
            )
            for call in identity.calls
        )
        _exact(
            tuple(
                (
                    row.audit_event_id,
                    row.operation_id,
                    row.run_id,
                    row.cost_event_id,
                    row.provider_request_id,
                )
                for row in target_audits
            ),
            expected_calls,
        )
        _exact(
            tuple(
                (
                    row.audit_event_id,
                    row.operation_id,
                    row.run_id,
                    row.cost_event_id,
                    row.provider_request_id,
                )
                for row in target_local
            ),
            expected_calls,
        )
        _exact(
            tuple(
                (
                    row.audit_event_id,
                    row.operation_id,
                    row.run_id,
                    row.cost_event_id,
                    row.provider_request_id,
                )
                for row in target_regulus
            ),
            expected_calls,
        )
        _exact(
            tuple((row.operation_id, row.run_id) for row in target_reservations),
            tuple((call.operation_id, identity.run_id) for call in identity.calls),
        )
        if any(row.state != "committed" for row in target_reservations):
            raise RightsizingReconciliationBlocked("rightsizing_reservation_not_committed")

        provider = raw.get("provider_window")
        if not isinstance(provider, Mapping) or set(provider) != {"window_id", "total_usd"}:
            raise RightsizingReconciliationBlocked("authoritative_export_invalid")
        try:
            provider_window = ProviderWindowSummary(
                window_id=str(provider["window_id"]),
                total_usd=Decimal(str(provider["total_usd"])),
            )
        except (TypeError, ValueError, ArithmeticError):
            raise RightsizingReconciliationBlocked("authoritative_export_invalid") from None
        return ReconciliationInput(
            audits=target_audits,
            reservations=target_reservations,
            local_cost_events=target_local,
            regulus_events=target_regulus,
            action_receipts=(),
            provider_window=provider_window,
        )


__all__ = [
    "AuthoritativeRightsizingReconciliationCollector",
    "CampaignRunInventory",
    "RightsizingReconciliationBlocked",
]
