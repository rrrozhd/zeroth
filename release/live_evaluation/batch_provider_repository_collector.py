"""Authoritative persisted-store collector for provider-backed batch runs.

The collector is read-only. It joins product-owned run, audit, reservation,
provider, cost, and Regulus identities and derives branch concurrency only from
the published graph and persisted child execution intervals.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from .batch_provider_service_adapter import (
    BatchCollectionIdentity,
    CollectedChildReconciliation,
    CollectedParentReconciliation,
)

_BRANCH_NODE = re.compile(r"^branch:(\d+):subgraph:")
_SECRET_SHAPE = re.compile(
    r"(?:^|[\r\n])\s*authorization\s*:|\bBearer\s+\S{16,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    re.IGNORECASE,
)
_ABSOLUTE_TOLERANCE = Decimal("0.000001")
_RELATIVE_TOLERANCE = Decimal("0.005")


class AuthoritativeBatchCollectionBlocked(RuntimeError):  # noqa: N818
    """A durable plane cannot prove the requested parent observation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuditSource(Protocol):
    def records_for_runs(self, run_ids: tuple[str, ...]) -> Sequence[Mapping[str, object]]: ...

    def verify_run(self, run_id: str) -> Mapping[str, object]: ...


def _open_read_only(path: Path, *, missing: str, invalid: str) -> sqlite3.Connection:
    if not path.is_file():
        raise AuthoritativeBatchCollectionBlocked(missing)
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.DatabaseError as exc:
        raise AuthoritativeBatchCollectionBlocked(invalid) from exc


def _object(value: object, code: str) -> dict[str, object]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise AuthoritativeBatchCollectionBlocked(code) from exc
    if not isinstance(decoded, dict):
        raise AuthoritativeBatchCollectionBlocked(code)
    return decoded


def _array(value: object, code: str) -> list[object]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise AuthoritativeBatchCollectionBlocked(code) from exc
    if not isinstance(decoded, list):
        raise AuthoritativeBatchCollectionBlocked(code)
    return decoded


def _identifier(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() or ord(character) < 32 for character in value)
        or _SECRET_SHAPE.search(value)
    ):
        raise AuthoritativeBatchCollectionBlocked(code)
    return value


def _money(value: object, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AuthoritativeBatchCollectionBlocked(code) from exc
    if not result.is_finite() or result < 0:
        raise AuthoritativeBatchCollectionBlocked(code)
    return result


def _reconciles(left: Decimal, right: Decimal) -> bool:
    tolerance = max(
        _ABSOLUTE_TOLERANCE,
        max(abs(left), abs(right)) * _RELATIVE_TOLERANCE,
    )
    return abs(left - right) <= tolerance


class RepositoryBackedBatchCollector:
    """Collect one parent observation from service, audit, and econ stores."""

    def __init__(
        self,
        *,
        service_database: Path,
        econ_database: Path,
        audit_source: AuditSource,
    ) -> None:
        self.service_database = Path(service_database)
        self.econ_database = Path(econ_database)
        self.audit_source = audit_source

    def collect(self, identity: BatchCollectionIdentity) -> CollectedParentReconciliation:
        if not isinstance(identity, BatchCollectionIdentity):
            raise TypeError("identity must be a BatchCollectionIdentity")
        audits = self._audits(identity)
        configured, observed, indexes = self._concurrency(identity, audits)
        children, campaign_spend = self._economics(identity, audits, indexes)
        return CollectedParentReconciliation(
            campaign_id=identity.campaign_id,
            repetition=identity.repetition,
            parent_run_id=identity.parent_run_id,
            configured_concurrency=configured,
            observed_peak_concurrency=observed,
            campaign_spend_after_usd=campaign_spend,
            children=children,
        )

    def _concurrency(
        self,
        identity: BatchCollectionIdentity,
        audits: Mapping[str, Mapping[str, object]],
    ) -> tuple[int, int, dict[str, int]]:
        database = _open_read_only(
            self.service_database,
            missing="service_database_missing",
            invalid="service_database_invalid",
        )
        try:
            parent = database.execute(
                """SELECT run_id, parent_run_id, graph_version_ref, metadata
                FROM runs WHERE tenant_id = ? AND run_id = ?""",
                (identity.tenant_id, identity.parent_run_id),
            ).fetchone()
            if parent is None or parent["parent_run_id"] is not None:
                raise AuthoritativeBatchCollectionBlocked("parent_lineage_incomplete")
            parent_metadata = _object(parent["metadata"], "parent_campaign_identity_incomplete")
            if parent_metadata.get("campaign_id") != identity.campaign_id:
                raise AuthoritativeBatchCollectionBlocked("parent_campaign_identity_incomplete")
            graph_id, separator, raw_version = str(parent["graph_version_ref"]).rpartition("@")
            if not separator or not graph_id:
                raise AuthoritativeBatchCollectionBlocked(
                    "configured_concurrency_not_authoritative"
                )
            try:
                version = int(raw_version)
            except ValueError as exc:
                raise AuthoritativeBatchCollectionBlocked(
                    "configured_concurrency_not_authoritative"
                ) from exc
            graph_row = database.execute(
                """SELECT payload FROM graph_versions
                WHERE tenant_id = ? AND graph_id = ? AND version = ?""",
                (identity.tenant_id, graph_id, version),
            ).fetchone()
            if graph_row is None:
                raise AuthoritativeBatchCollectionBlocked(
                    "configured_concurrency_not_authoritative"
                )
            graph = _object(graph_row["payload"], "configured_concurrency_not_authoritative")
            nodes = graph.get("nodes")
            values = []
            if isinstance(nodes, list):
                for node in nodes:
                    config = node.get("parallel_config") if isinstance(node, dict) else None
                    if isinstance(config, dict):
                        values.append(config.get("max_concurrency"))
            if len(values) != 1 or not isinstance(values[0], int) or isinstance(values[0], bool):
                raise AuthoritativeBatchCollectionBlocked(
                    "configured_concurrency_not_authoritative"
                )
            configured = values[0]

            placeholders = ",".join("?" for _ in identity.child_run_ids)
            rows = database.execute(
                f"""SELECT run_id, parent_run_id, metadata, execution_history FROM runs
                WHERE tenant_id = ? AND run_id IN ({placeholders})""",
                (identity.tenant_id, *identity.child_run_ids),
            ).fetchall()
            if len(rows) != len(identity.child_run_ids):
                raise AuthoritativeBatchCollectionBlocked("child_lineage_incomplete")
            intervals: list[tuple[datetime, datetime]] = []
            indexes: dict[str, int] = {}
            for row in rows:
                run_id = str(row["run_id"])
                metadata = _object(row["metadata"], "child_campaign_identity_incomplete")
                if (
                    row["parent_run_id"] != identity.parent_run_id
                    or metadata.get("campaign_id") != identity.campaign_id
                ):
                    raise AuthoritativeBatchCollectionBlocked("child_lineage_incomplete")
                history = _array(row["execution_history"], "observed_concurrency_not_authoritative")
                branch_indexes: set[int] = set()
                starts: list[datetime] = []
                completions: list[datetime] = []
                priced_audit_id = audits[run_id].get("audit_id")
                for entry in history:
                    if not isinstance(entry, dict):
                        raise AuthoritativeBatchCollectionBlocked(
                            "observed_concurrency_not_authoritative"
                        )
                    entry_audit_ref = entry.get("audit_ref")
                    if (
                        not isinstance(priced_audit_id, str)
                        or not isinstance(entry_audit_ref, str)
                        or (
                            entry_audit_ref != priced_audit_id
                            and not priced_audit_id.endswith(f":{entry_audit_ref}")
                        )
                    ):
                        continue
                    node_id = entry.get("node_id")
                    match = _BRANCH_NODE.match(node_id) if isinstance(node_id, str) else None
                    if match is not None:
                        branch_indexes.add(int(match.group(1)))
                    try:
                        started = datetime.fromisoformat(str(entry["started_at"]))
                        completed = datetime.fromisoformat(str(entry["completed_at"]))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise AuthoritativeBatchCollectionBlocked(
                            "observed_concurrency_not_authoritative"
                        ) from exc
                    if completed <= started:
                        raise AuthoritativeBatchCollectionBlocked(
                            "observed_concurrency_not_authoritative"
                        )
                    starts.append(started)
                    completions.append(completed)
                if len(branch_indexes) != 1 or len(starts) != 1:
                    raise AuthoritativeBatchCollectionBlocked(
                        "observed_concurrency_not_authoritative"
                    )
                indexes[run_id] = branch_indexes.pop()
                intervals.append((min(starts), max(completions)))
        except sqlite3.DatabaseError as exc:
            raise AuthoritativeBatchCollectionBlocked("service_schema_incomplete") from exc
        finally:
            database.close()
        if set(indexes.values()) != set(range(8)):
            raise AuthoritativeBatchCollectionBlocked("child_branch_identity_incomplete")
        events = sorted(
            [
                event
                for started, completed in intervals
                for event in ((started, 1), (completed, -1))
            ],
            key=lambda event: (event[0], event[1]),
        )
        active = 0
        observed = 0
        for _, delta in events:
            active += delta
            observed = max(observed, active)
        if observed < 1:
            raise AuthoritativeBatchCollectionBlocked("observed_concurrency_not_authoritative")
        return configured, observed, indexes

    def _audits(self, identity: BatchCollectionIdentity) -> dict[str, Mapping[str, object]]:
        for run_id in identity.child_run_ids:
            verification = self.audit_source.verify_run(run_id)
            if (
                verification.get("verified") is not True
                or verification.get("signature_verified") is not True
                or verification.get("unsigned_record_count") != 0
                or verification.get("run_id") != run_id
                or verification.get("tenant_id") != identity.tenant_id
                or verification.get("campaign_id") != identity.campaign_id
            ):
                raise AuthoritativeBatchCollectionBlocked("signed_audit_verification_failed")
        records = self.audit_source.records_for_runs(identity.child_run_ids)
        result: dict[str, Mapping[str, object]] = {}
        for row in records:
            run_id = row.get("run_id")
            if (
                run_id not in identity.child_run_ids
                or row.get("tenant_id") != identity.tenant_id
                or row.get("campaign_id") != identity.campaign_id
                or not row.get("record_signature")
                or row.get("status") not in {"completed", "succeeded"}
            ):
                raise AuthoritativeBatchCollectionBlocked("audit_identity_join_incomplete")
            cost_event_id = row.get("cost_event_id")
            if cost_event_id is None:
                continue
            # Provider instrumentation writes an append-only lifecycle audit
            # named from the cost event before the runtime node audit.  Both
            # carry the same cost identity, but only the runtime audit is linked
            # from execution_history and therefore owns the concurrency interval.
            # Keep the probe in chain verification while excluding it from the
            # one-priced-node-per-child join.
            if row.get("audit_id") == f"audit_{cost_event_id}":
                continue
            if run_id in result:
                raise AuthoritativeBatchCollectionBlocked("audit_identity_join_incomplete")
            result[str(run_id)] = row
        if set(result) != set(identity.child_run_ids):
            raise AuthoritativeBatchCollectionBlocked("audit_identity_join_incomplete")
        return result

    def _economics(
        self,
        identity: BatchCollectionIdentity,
        audits: Mapping[str, Mapping[str, object]],
        indexes: Mapping[str, int],
    ) -> tuple[tuple[CollectedChildReconciliation, ...], Decimal]:
        database = _open_read_only(
            self.econ_database,
            missing="econ_database_missing",
            invalid="econ_database_invalid",
        )
        placeholders = ",".join("?" for _ in identity.child_run_ids)
        try:
            reservations = database.execute(
                f"""SELECT * FROM cost_reservations
                WHERE tenant_id = ? AND campaign_id = ? AND run_id IN ({placeholders})""",
                (identity.tenant_id, identity.campaign_id, *identity.child_run_ids),
            ).fetchall()
            executions = database.execute(
                f"""SELECT * FROM execution_events
                WHERE tenant_id = ? AND campaign_id = ?
                AND json_extract(metadata, '$.run_id') IN ({placeholders})""",
                (identity.tenant_id, identity.campaign_id, *identity.child_run_ids),
            ).fetchall()
            spend_rows = database.execute(
                """SELECT status, held_cost_usd, actual_cost_usd FROM cost_reservations
                WHERE tenant_id = ? AND campaign_id = ?""",
                (identity.tenant_id, identity.campaign_id),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise AuthoritativeBatchCollectionBlocked("econ_schema_incomplete") from exc
        finally:
            database.close()
        by_run: dict[str, sqlite3.Row] = {}
        for row in reservations:
            run_id = str(row["run_id"])
            if run_id in by_run:
                raise AuthoritativeBatchCollectionBlocked("reservation_identity_join_incomplete")
            by_run[run_id] = row
        if set(by_run) != set(identity.child_run_ids):
            raise AuthoritativeBatchCollectionBlocked("reservation_identity_join_incomplete")
        exec_by_run: dict[str, sqlite3.Row] = {}
        for row in executions:
            metadata = _object(row["metadata"], "regulus_identity_join_incomplete")
            run_id = metadata.get("run_id")
            if not isinstance(run_id, str) or run_id in exec_by_run:
                raise AuthoritativeBatchCollectionBlocked("regulus_identity_join_incomplete")
            exec_by_run[run_id] = row
        if set(exec_by_run) != set(identity.child_run_ids):
            raise AuthoritativeBatchCollectionBlocked("regulus_identity_join_incomplete")

        children: list[CollectedChildReconciliation] = []
        operation_ids: set[str] = set()
        provider_ids: set[str] = set()
        cost_ids: set[str] = set()
        audit_ids: set[str] = set()
        for run_id in identity.child_run_ids:
            reservation = by_run[run_id]
            execution = exec_by_run[run_id]
            audit = audits[run_id]
            operation_id = _identifier(
                reservation["operation_id"], "reservation_identity_join_incomplete"
            )
            cost_event_id = _identifier(
                reservation["cost_event_id"], "reservation_identity_join_incomplete"
            )
            provider_raw = reservation["provider_request_id"]
            provider_id = (
                None
                if provider_raw is None
                else _identifier(provider_raw, "reservation_identity_join_incomplete")
            )
            if (
                reservation["status"] != "committed"
                or execution["operation_id"] != operation_id
                or execution["execution_id"] != cost_event_id
                or execution["provider_request_id"] != provider_id
                or execution["cleanup_status"] != reservation["cleanup_status"]
                or audit.get("cost_event_id") != cost_event_id
            ):
                raise AuthoritativeBatchCollectionBlocked("cross_plane_identity_join_incomplete")
            actual = _money(reservation["actual_cost_usd"], "reservation_measurement_incomplete")
            economics = _money(
                execution["token_cost_usd"], "regulus_measurement_incomplete"
            ) + sum(
                (
                    Decimal(0)
                    if execution[field] is None
                    else _money(execution[field], "regulus_measurement_incomplete")
                    for field in ("tool_cost_usd", "compute_cost_usd")
                ),
                Decimal(0),
            )
            audit_measurement = audit.get("cost_measurement")
            audit_cost_value = (
                audit.get("estimated_cost_usd")
                if audit_measurement == "estimated"
                else audit.get("cost_usd")
            )
            audit_cost = _money(audit_cost_value, "audit_cost_incomplete")
            maximum = _money(reservation["max_cost_usd"], "reservation_measurement_incomplete")
            released = _money(
                reservation["released_cost_usd"], "reservation_measurement_incomplete"
            )
            audit_id = _identifier(audit.get("audit_id"), "audit_identity_join_incomplete")
            if (
                reservation["cleanup_status"] != "complete"
                or actual > maximum
                or not _reconciles(maximum - actual, released)
                or not _reconciles(actual, audit_cost)
                or not _reconciles(actual, economics)
            ):
                raise AuthoritativeBatchCollectionBlocked("cross_plane_cost_reconciliation_failed")
            if (
                operation_id in operation_ids
                or (provider_id is not None and provider_id in provider_ids)
                or cost_event_id in cost_ids
                or audit_id in audit_ids
            ):
                raise AuthoritativeBatchCollectionBlocked("cross_plane_identity_join_incomplete")
            operation_ids.add(operation_id)
            if provider_id is not None:
                provider_ids.add(provider_id)
            cost_ids.add(cost_event_id)
            audit_ids.add(audit_id)
            children.append(
                CollectedChildReconciliation(
                    item_index=indexes[run_id],
                    child_run_id=run_id,
                    operation_id=operation_id,
                    provider_request_id=provider_id,
                    audit_event_id=audit_id,
                    cost_event_id=cost_event_id,
                    regulus_execution_event_id=_identifier(
                        execution["execution_id"], "regulus_identity_join_incomplete"
                    ),
                    # The service's durable reservation handle is operation_id;
                    # reserve() returns it and the table enforces tenant+operation uniqueness.
                    reservation_id=operation_id,
                    reservation_operation_id=operation_id,
                    reservation_status=str(reservation["status"]),
                    reserved_max_cost_usd=maximum,
                    reservation_actual_cost_usd=actual,
                    reservation_released_cost_usd=released,
                    reservation_cleanup_status=_identifier(
                        reservation["cleanup_status"], "reservation_cleanup_incomplete"
                    ),
                    cache_hit=False,
                    audit_cost_usd=audit_cost,
                    local_cost_usd=actual,
                    economics_cost_usd=economics,
                )
            )
        campaign_spend = sum(
            (
                _money(
                    row["actual_cost_usd"]
                    if row["status"] == "committed"
                    else row["held_cost_usd"],
                    "campaign_spend_incomplete",
                )
                for row in spend_rows
                if row["status"] in {"reserved", "committed", "ambiguous"}
            ),
            Decimal(0),
        )
        return tuple(sorted(children, key=lambda child: child.item_index)), campaign_spend
