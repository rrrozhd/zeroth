"""Authoritative live-campaign reconciliation export from durable planes."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx


class AuthoritativeExportBlocked(RuntimeError):  # noqa: N818
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuditSource(Protocol):
    def records_for_runs(self, run_ids: tuple[str, ...]) -> Sequence[Mapping[str, object]]: ...
    def verify_run(self, run_id: str) -> Mapping[str, object]: ...


class HttpAuditSource:
    """Read sanitized audits and signed-chain verdicts from public deployment APIs."""

    def __init__(
        self,
        *,
        deployments: Mapping[str, str],
        headers: Mapping[str, str],
        client: httpx.Client | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        if not deployments:
            raise ValueError("at least one deployment audit endpoint is required")
        self.deployments = dict(deployments)
        self.headers = dict(headers)
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self._records: dict[str, tuple[dict[str, object], ...]] = {}
        self._verification: dict[str, dict[str, object]] = {}

    def _load_run(self, run_id: str) -> None:
        if run_id in self._records:
            return
        records: list[dict[str, object]] = []
        verifications: list[dict[str, object]] = []
        run_identities: list[dict[str, object]] = []
        for deployment_ref, base_url in self.deployments.items():
            audit_response = self.client.get(
                f"{base_url}/v1/deployments/{quote(deployment_ref, safe='')}/audits",
                params={"run_id": run_id},
                headers=self.headers,
            )
            if audit_response.status_code != 200:
                raise AuthoritativeExportBlocked("audit_public_api_unavailable")
            body = audit_response.json()
            rows = body.get("records") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise AuthoritativeExportBlocked("audit_public_api_invalid")
            if not rows:
                continue
            records.extend(dict(row) for row in rows)
            run_response = self.client.get(
                f"{base_url}/v1/runs/{quote(run_id, safe='')}",
                headers=self.headers,
            )
            if run_response.status_code != 200:
                raise AuthoritativeExportBlocked("run_identity_api_unavailable")
            run_identity = run_response.json()
            if not isinstance(run_identity, dict):
                raise AuthoritativeExportBlocked("run_identity_api_invalid")
            run_identities.append(run_identity)
            verify_response = self.client.get(
                f"{base_url}/v1/runs/{quote(run_id, safe='')}/audit-verification",
                headers=self.headers,
            )
            if verify_response.status_code != 200:
                raise AuthoritativeExportBlocked("audit_verification_api_unavailable")
            verification = verify_response.json()
            if not isinstance(verification, dict):
                raise AuthoritativeExportBlocked("audit_verification_api_invalid")
            verifications.append(verification)
        if not records or not verifications or not run_identities:
            raise AuthoritativeExportBlocked("campaign_run_audit_missing")
        audit_ids = [str(row.get("audit_id")) for row in records]
        if len(audit_ids) != len(set(audit_ids)):
            raise AuthoritativeExportBlocked("duplicate_audit_identity")
        identity_triplets = {
            (
                row.get("run_id"),
                row.get("tenant_id"),
                row.get("campaign_id"),
            )
            for row in run_identities
        }
        if len(identity_triplets) != 1:
            raise AuthoritativeExportBlocked("run_identity_api_ambiguous")
        identity_run_id, identity_tenant_id, identity_campaign_id = identity_triplets.pop()
        self._records[run_id] = tuple(records)
        self._verification[run_id] = {
            "verified": all(row.get("verified") is True for row in verifications),
            "signature_verified": all(
                row.get("signature_verified") is True for row in verifications
            ),
            "unsigned_record_count": sum(
                int(row.get("unsigned_record_count", 0)) for row in verifications
            ),
            "run_id": identity_run_id,
            "tenant_id": identity_tenant_id,
            "campaign_id": identity_campaign_id,
        }

    def records_for_runs(self, run_ids: tuple[str, ...]) -> Sequence[Mapping[str, object]]:
        for run_id in run_ids:
            self._load_run(run_id)
        return tuple(row for run_id in run_ids for row in self._records[run_id])

    def verify_run(self, run_id: str) -> Mapping[str, object]:
        self._load_run(run_id)
        return self._verification[run_id]


def run_ids_from_events(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise AuthoritativeExportBlocked("campaign_event_journal_missing")
    run_ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuthoritativeExportBlocked("campaign_event_journal_invalid") from exc
        correlation = event.get("correlation") if isinstance(event, dict) else None
        run_id = correlation.get("run_id") if isinstance(correlation, dict) else None
        if isinstance(run_id, str) and run_id:
            run_ids.add(run_id)
    if not run_ids:
        raise AuthoritativeExportBlocked("campaign_run_inventory_empty")
    return tuple(sorted(run_ids))


def _money(value: object, *, code: str) -> str:
    try:
        parsed = Decimal(str(value or "0"))
    except Exception as exc:
        raise AuthoritativeExportBlocked(code) from exc
    if not parsed.is_finite() or parsed < 0:
        raise AuthoritativeExportBlocked(code)
    return format(parsed, "f")


def _canonical_call_audit(
    matches: Sequence[Mapping[str, object]], *, cost_event_id: str
) -> Mapping[str, object]:
    """Select the runtime projection paired with one lifecycle audit row.

    Provider instrumentation writes ``audit_<cost_event_id>`` and runtime
    execution writes the node-facing audit. They are two signed projections of
    one call, not two calls. Any other multiplicity or divergent amount remains
    ambiguous and fails closed.
    """
    if len(matches) == 1:
        return matches[0]
    lifecycle_id = f"audit_{cost_event_id}"
    lifecycle = [row for row in matches if row.get("audit_id") == lifecycle_id]
    runtime = [row for row in matches if row.get("audit_id") != lifecycle_id]
    if len(lifecycle) != 1 or len(runtime) != 1:
        raise AuthoritativeExportBlocked("audit_identity_join_incomplete")
    lifecycle_cost = _audit_amount(lifecycle[0])
    runtime_cost = _audit_amount(runtime[0])
    if Decimal(lifecycle_cost) != Decimal(runtime_cost):
        raise AuthoritativeExportBlocked("audit_identity_join_incomplete")
    return runtime[0]


def _audit_amount(row: Mapping[str, object]) -> str:
    """Return the audit's declared measured or estimated call amount."""
    value = row.get("cost_usd")
    if value is None:
        value = row.get("estimated_cost_usd")
    if value is None:
        raise AuthoritativeExportBlocked("audit_cost_incomplete")
    return _money(value, code="audit_cost_incomplete")


def _metadata(value: object, *, code: str) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AuthoritativeExportBlocked(code) from exc
    if not isinstance(value, dict):
        raise AuthoritativeExportBlocked(code)
    return value


class AuthoritativeCampaignExporter:
    """Join exact first-class identities; optional audit metadata never fills a gap.

    Campaign and provider identities are authoritative in the reservation and
    execution ledgers.  Audit records are joined through their signed, typed
    tenant/run/cost-event fields.  Requiring raw provider identifiers in
    ``execution_metadata`` would conflict with the metadata-only capture policy,
    which deliberately hashes externally authored text.
    """

    def __init__(
        self,
        *,
        econ_database: Path,
        action_sink_database: Path,
        campaign_id: str,
        tenant_id: str,
        run_ids: Sequence[str],
        audit_source: AuditSource,
        provider_window: Path,
    ) -> None:
        self.econ_database = econ_database
        self.action_sink_database = action_sink_database
        self.campaign_id = campaign_id
        self.tenant_id = tenant_id
        self.run_ids = tuple(dict.fromkeys(run_ids))
        self.audit_source = audit_source
        self.provider_window = provider_window

    @property
    def _tag(self) -> dict[str, str]:
        return {"campaign_id": self.campaign_id, "tenant_id": self.tenant_id}

    def export(self) -> dict[str, object]:
        if not self.run_ids:
            raise AuthoritativeExportBlocked("campaign_run_inventory_empty")
        reservations, executions, outcomes = self._econ_rows()
        audits = self._audits()
        provider = self._provider_window()

        audit_rows: list[dict[str, object]] = []
        local_rows: list[dict[str, object]] = []
        regulus_rows: list[dict[str, object]] = []
        reservation_rows: list[dict[str, object]] = []
        excluded_reservations: list[dict[str, object]] = []
        for reservation in reservations:
            operation_id = str(reservation["operation_id"])
            run_id = reservation["run_id"]
            cost_id = reservation["cost_event_id"]
            provider_id = reservation["provider_request_id"]
            if reservation["status"] == "released":
                valid_release = (
                    reservation["cleanup_status"] == "provider_not_called"
                    and cost_id is None
                    and provider_id is None
                    and Decimal(str(reservation["actual_cost_usd"] or "0")) == 0
                    and Decimal(str(reservation["held_cost_usd"] or "0")) == 0
                )
                if not valid_release:
                    raise AuthoritativeExportBlocked(
                        "released_reservation_outcome_unproven"
                    )
                if not isinstance(run_id, str) or not run_id:
                    raise AuthoritativeExportBlocked("reservation_identity_join_incomplete")
                excluded_reservations.append(
                    {
                        **self._tag,
                        "reservation_id": f"reservation-{reservation['row_id']}",
                        "operation_id": operation_id,
                        "run_id": run_id,
                        "reason": "provider_not_called",
                        "cleanup_status": "provider_not_called",
                    }
                )
                continue
            reservation_status = str(reservation["status"])
            if not all(isinstance(value, str) and value for value in (run_id, cost_id)):
                raise AuthoritativeExportBlocked("reservation_identity_join_incomplete")
            if provider_id is not None and not (
                isinstance(provider_id, str) and provider_id
            ):
                raise AuthoritativeExportBlocked("reservation_identity_join_incomplete")
            # Provider request IDs are additive evidence when the adapter exposes
            # one. A committed call remains exactly joinable by tenant, run,
            # operation, cost-event, cleanup, audit, and Regulus identities when
            # the upstream provider does not return a request ID.
            state = {
                "committed": "committed",
                "ambiguous": "held_ambiguous",
            }.get(reservation_status)
            if state is None:
                continue
            reservation_rows.append(
                {
                    **self._tag,
                    "reservation_id": f"reservation-{reservation['row_id']}",
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "state": state,
                    "maximum_usd": _money(
                        reservation["max_cost_usd"], code="reservation_cost_invalid"
                    ),
                    "retained_usd": _money(
                        reservation["held_cost_usd"], code="reservation_cost_invalid"
                    ),
                }
            )
            matching_audits = [
                audit
                for audit in audits
                if audit.get("tenant_id") == self.tenant_id
                and audit.get("run_id") == run_id
                and audit.get("cost_event_id") == cost_id
            ]
            matching_exec = [
                row
                for row in executions
                if row["execution_id"] == cost_id
                and row["operation_id"] == operation_id
                and row["provider_request_id"] == provider_id
                and row["cleanup_status"] == reservation["cleanup_status"]
            ]
            audit = _canonical_call_audit(matching_audits, cost_event_id=cost_id)
            if len(matching_exec) != 1:
                raise AuthoritativeExportBlocked("regulus_identity_join_incomplete")
            execution = matching_exec[0]
            metadata = _metadata(
                execution["metadata"], code="regulus_measurement_metadata_incomplete"
            )
            if metadata.get("run_id") != run_id:
                raise AuthoritativeExportBlocked("regulus_measurement_metadata_incomplete")
            status = str(audit.get("status"))
            if status in {"completed", "succeeded"}:
                run_status = "succeeded"
            elif status == "failed":
                run_status = "failed"
            else:
                raise AuthoritativeExportBlocked("audit_run_status_incomplete")
            amount = _money(
                reservation["actual_cost_usd"], code="local_cost_measurement_incomplete"
            )
            audit_id = str(audit["audit_id"])
            audit_cost = _audit_amount(audit)
            audit_rows.append(
                {
                    **self._tag,
                    "audit_event_id": audit_id,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "cost_event_id": cost_id,
                    "provider_request_id": provider_id,
                    "cost_usd": audit_cost,
                    "cache_hit": False,
                    "run_status": run_status,
                    "signed": bool(audit.get("record_signature")),
                    "chain_verified": True,
                }
            )
            local_rows.append(
                {
                    **self._tag,
                    "cost_event_id": cost_id,
                    "audit_event_id": audit_id,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "provider_request_id": provider_id,
                    "amount_usd": amount,
                    "cache_hit": False,
                    "run_status": run_status,
                    "failure_tax_usd": amount if run_status == "failed" else "0",
                }
            )
            regulus_amount = sum(
                Decimal(str(execution[field] or "0"))
                for field in ("token_cost_usd", "tool_cost_usd", "compute_cost_usd")
            )
            matching_outcomes = [row for row in outcomes if row["execution_id"] == cost_id]
            if len(matching_outcomes) > 1:
                raise AuthoritativeExportBlocked("valuation_outcome_identity_ambiguous")
            valuation: dict[str, object] = {
                "valuation_recorded": False,
                "value_usd": "0",
                "margin_usd": "0",
                "synthetic_outcome_id": None,
            }
            if matching_outcomes:
                outcome = _metadata(
                    matching_outcomes[0]["outcome_payload_json"],
                    code="valuation_outcome_incomplete",
                )
                required = {"synthetic_outcome_id", "value_usd", "margin_usd"}
                if not required.issubset(outcome) or not outcome["synthetic_outcome_id"]:
                    raise AuthoritativeExportBlocked("valuation_outcome_incomplete")
                valuation = {
                    "valuation_recorded": True,
                    "value_usd": _money(
                        outcome["value_usd"], code="valuation_outcome_incomplete"
                    ),
                    "margin_usd": format(Decimal(str(outcome["margin_usd"])), "f"),
                    "synthetic_outcome_id": str(outcome["synthetic_outcome_id"]),
                }
            regulus_rows.append(
                {
                    **self._tag,
                    "execution_event_id": cost_id,
                    "cost_event_id": cost_id,
                    "audit_event_id": audit_id,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "provider_request_id": provider_id,
                    "amount_usd": format(regulus_amount, "f"),
                    "failure_tax_usd": amount if run_status == "failed" else "0",
                    **valuation,
                }
            )
        return {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "tenant_id": self.tenant_id,
            "audits": audit_rows,
            "reservations": reservation_rows,
            "local_cost_events": local_rows,
            "regulus_events": regulus_rows,
            "action_receipts": self._receipts(audits),
            "excluded_reservations": excluded_reservations,
            "provider_window": provider,
        }

    def _econ_rows(
        self,
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
        if not self.econ_database.is_file():
            raise AuthoritativeExportBlocked("econ_database_missing")
        with sqlite3.connect(f"file:{self.econ_database}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            try:
                reservations = db.execute(
                    """SELECT rowid AS row_id, * FROM cost_reservations
                    WHERE tenant_id = ? AND campaign_id = ?
                    AND run_id IN ("""
                    + ",".join("?" for _ in self.run_ids)
                    + ")",
                    (self.tenant_id, self.campaign_id, *self.run_ids),
                ).fetchall()
                cost_event_ids = [
                    row["cost_event_id"]
                    for row in reservations
                    if row["cost_event_id"] is not None
                ]
                executions = (
                    db.execute(
                        "SELECT * FROM execution_events WHERE tenant_id = ? "
                        "AND campaign_id = ? AND execution_id IN ("
                        + ",".join("?" for _ in cost_event_ids)
                        + ")",
                        (self.tenant_id, self.campaign_id, *cost_event_ids),
                    ).fetchall()
                    if cost_event_ids
                    else []
                )
                execution_ids = [row["execution_id"] for row in executions]
                outcomes = (
                    db.execute(
                        "SELECT tenant_id, execution_id, outcome_payload_json "
                        f"FROM outcome_events WHERE tenant_id = ? AND execution_id IN "
                        f"({','.join('?' for _ in execution_ids)})",
                        (self.tenant_id, *execution_ids),
                    ).fetchall()
                    if execution_ids
                    else []
                )
            except sqlite3.DatabaseError as exc:
                raise AuthoritativeExportBlocked("econ_schema_incomplete") from exc
        if not reservations:
            raise AuthoritativeExportBlocked("campaign_reservations_missing")
        return reservations, executions, outcomes

    def _audits(self) -> tuple[dict[str, object], ...]:
        for run_id in self.run_ids:
            verification = self.audit_source.verify_run(run_id)
            if (
                verification.get("verified") is not True
                or verification.get("signature_verified") is not True
                or verification.get("unsigned_record_count") != 0
            ):
                raise AuthoritativeExportBlocked("signed_audit_verification_failed")
            if verification.get("run_id") != run_id:
                raise AuthoritativeExportBlocked("audit_run_identity_mismatch")
            if verification.get("tenant_id") != self.tenant_id:
                raise AuthoritativeExportBlocked("audit_tenant_identity_mismatch")
            if verification.get("campaign_id") != self.campaign_id:
                raise AuthoritativeExportBlocked("audit_campaign_identity_mismatch")
        records = tuple(dict(row) for row in self.audit_source.records_for_runs(self.run_ids))
        for row in records:
            if row.get("tenant_id") != self.tenant_id:
                raise AuthoritativeExportBlocked("audit_tenant_identity_mismatch")
            if row.get("run_id") not in self.run_ids:
                raise AuthoritativeExportBlocked("audit_run_identity_mismatch")
        return records

    def _receipts(self, audits: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        if not self.action_sink_database.is_file():
            raise AuthoritativeExportBlocked("action_sink_database_missing")
        with sqlite3.connect(f"file:{self.action_sink_database}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            try:
                markers = db.execute("SELECT operation_key, receipt FROM action_markers").fetchall()
            except sqlite3.DatabaseError as exc:
                raise AuthoritativeExportBlocked("action_sink_schema_incomplete") from exc
        result = []
        for marker in markers:
            matches = [
                audit
                for audit in audits
                if isinstance(audit.get("execution_metadata"), dict)
                and audit["execution_metadata"].get("operation_key") == marker["operation_key"]
            ]
            if not matches:
                # The sink is campaign-scoped, while this export may intentionally
                # reconcile only a subset of campaign runs. Receipts without an
                # audit in the requested run inventory are outside this export.
                continue
            if len(matches) != 1:
                raise AuthoritativeExportBlocked("action_receipt_audit_join_incomplete")
            audit = matches[0]
            result.append(
                {
                    **self._tag,
                    "receipt_id": str(marker["receipt"]),
                    "audit_event_id": str(audit["audit_id"]),
                    "operation_id": str(marker["operation_key"]),
                    "run_id": str(audit["run_id"]),
                    "status": "completed",
                }
            )
        return result

    def _provider_window(self) -> dict[str, str]:
        if not self.provider_window.is_file():
            raise AuthoritativeExportBlocked("provider_window_missing")
        try:
            payload = json.loads(self.provider_window.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthoritativeExportBlocked("provider_window_invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {"window_id", "total_usd"}:
            raise AuthoritativeExportBlocked("provider_window_invalid")
        return {
            "window_id": str(payload["window_id"]),
            "total_usd": _money(payload["total_usd"], code="provider_window_invalid"),
        }


def _deployment_values(values: Sequence[str]) -> dict[str, str]:
    deployments: dict[str, str] = {}
    for value in values:
        reference, separator, url = value.partition("=")
        if not separator or not reference or not url or reference in deployments:
            raise ValueError("deployments must be unique REF=URL pairs")
        deployments[reference] = url.rstrip("/")
    return deployments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="campaign-reconciliation-export")
    parser.add_argument("--econ-db", type=Path, required=True)
    parser.add_argument("--action-sink-db", type=Path, required=True)
    parser.add_argument("--provider-window", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--deployment", action="append", default=[])
    parser.add_argument("--api-key-env", default="ZEROTH_EVALUATION_API_KEY")
    parser.add_argument("--timeout-seconds", type=float, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            raise AuthoritativeExportBlocked("audit_api_credential_missing")
        source = HttpAuditSource(
            deployments=_deployment_values(args.deployment),
            headers={"X-API-Key": api_key, "X-Tenant-ID": args.tenant},
            timeout_seconds=args.timeout_seconds,
        )
        payload = AuthoritativeCampaignExporter(
            econ_database=args.econ_db,
            action_sink_database=args.action_sink_db,
            campaign_id=args.campaign,
            tenant_id=args.tenant,
            run_ids=run_ids_from_events(args.events),
            audit_source=source,
            provider_window=args.provider_window,
        ).export()
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        print(json.dumps({"status": "complete", "record_count": len(payload["audits"])}))
        return 0
    except AuthoritativeExportBlocked as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
