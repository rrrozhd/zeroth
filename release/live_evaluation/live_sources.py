"""Bounded live source adapters for the evidence-first campaign CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from .cross_cutting_gates import CheckCommandResult, PlaywrightProductionResult
from .evidence import EvidenceStore
from .reconciliation import (
    ActionReceiptRecord,
    AuditRecord,
    LocalCostEvent,
    ProviderWindowSummary,
    ReconciliationInput,
    ReconciliationResult,
    RegulusExecutionEvent,
    ReservationRecord,
    reconcile_campaign,
)

_SNAPSHOT_KEYS = {
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
    "total_usd",
}
_CHECK_PASS = re.compile(r"^Zeroth Check: PASS \(exit 0\)$", re.MULTILINE)


class ReconciliationCollectionBlocked(ValueError):  # noqa: N818
    """A stable, sanitized reason why authoritative collection cannot proceed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SnapshotProductionResult:
    output_path: Path
    argv: tuple[str, ...]
    working_directory: Path
    exit_code: int
    stdout: str
    stderr: str


def _require_object(value: object, *, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReconciliationCollectionBlocked(code)
    return value


def _typed_record(
    row: object,
    record_type: type[Any],
    *,
    campaign_id: str,
    tenant_id: str,
) -> Any:
    payload = _require_object(row, code="authoritative_snapshot_invalid")
    if payload.get("campaign_id") != campaign_id or payload.get("tenant_id") != tenant_id:
        raise ReconciliationCollectionBlocked("campaign_tag_mismatch")
    expected = {field.name for field in fields(record_type)} | {"campaign_id", "tenant_id"}
    if set(payload) != expected:
        raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid")
    values = {
        key: Decimal(str(value)) if key in _MONEY_FIELDS else value
        for key, value in payload.items()
        if key not in {"campaign_id", "tenant_id"}
    }
    try:
        return record_type(**values)
    except (TypeError, ValueError) as exc:
        raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid") from exc


class CampaignSnapshotCollector:
    """Collect an exact reconciliation input from a tagged durable JSON export.

    The current database schemas do not expose a single authoritative aggregate
    join for all six evidence planes.  This adapter therefore accepts only an
    export whose every attributable row carries the exact campaign and tenant
    tags; it never fills identities from optional metadata.
    """

    def __init__(
        self,
        *,
        source: Path,
        campaign_id: str,
        tenant_id: str,
        producer: Callable[[], SnapshotProductionResult] | None = None,
        reconciler: Callable[
            [EvidenceStore, ReconciliationInput], ReconciliationResult
        ] = reconcile_campaign,
    ) -> None:
        self.source = source.expanduser().resolve(strict=False)
        self.campaign_id = campaign_id
        self.tenant_id = tenant_id
        self.producer = producer
        self.reconciler = reconciler

    def __call__(self, store: EvidenceStore) -> ReconciliationResult:
        if self.producer is not None:
            produced = self.producer()
            sequence = len(tuple((store.root / "commands").glob("*.json"))) + 1
            store.record_command(
                sequence=sequence,
                name="reconciliation-export",
                argv=produced.argv,
                working_directory=produced.working_directory,
                exit_code=produced.exit_code,
                stdout=produced.stdout,
                stderr=produced.stderr,
            )
            if produced.output_path.resolve(strict=False) != self.source:
                raise ReconciliationCollectionBlocked("authoritative_export_path_mismatch")
            if produced.exit_code != 0:
                reason = "authoritative_export_failed"
                try:
                    failure = json.loads(produced.stdout)
                except json.JSONDecodeError:
                    failure = None
                if (
                    isinstance(failure, dict)
                    and failure.get("status") == "blocked"
                    and isinstance(failure.get("reason"), str)
                    and re.fullmatch(r"[a-z][a-z0-9_]{2,63}", failure["reason"])
                ):
                    reason = failure["reason"]
                raise ReconciliationCollectionBlocked(reason)
        if not self.source.is_file():
            raise ReconciliationCollectionBlocked("authoritative_snapshot_missing")
        try:
            raw = json.loads(self.source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid") from exc
        payload = _require_object(raw, code="authoritative_snapshot_invalid")
        store.validate(payload)
        if (
            set(payload) != _SNAPSHOT_KEYS
            or payload.get("schema_version") != 1
            or payload.get("campaign_id") != self.campaign_id
            or payload.get("tenant_id") != self.tenant_id
        ):
            raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid")

        def rows(name: str, record_type: type[Any]) -> tuple[Any, ...]:
            values = payload.get(name)
            if not isinstance(values, list):
                raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid")
            return tuple(
                _typed_record(
                    row,
                    record_type,
                    campaign_id=self.campaign_id,
                    tenant_id=self.tenant_id,
                )
                for row in values
            )

        excluded = payload.get("excluded_reservations")
        if not isinstance(excluded, list):
            raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid")
        excluded_keys = {
            "campaign_id",
            "tenant_id",
            "reservation_id",
            "operation_id",
            "run_id",
            "reason",
            "cleanup_status",
        }
        for row in excluded:
            proof = _require_object(row, code="authoritative_snapshot_invalid")
            if (
                set(proof) != excluded_keys
                or proof.get("campaign_id") != self.campaign_id
                or proof.get("tenant_id") != self.tenant_id
                or proof.get("reason") != "provider_not_called"
                or proof.get("cleanup_status") != "provider_not_called"
            ):
                raise ReconciliationCollectionBlocked("excluded_reservation_proof_invalid")

        window = _require_object(
            payload.get("provider_window"), code="authoritative_snapshot_invalid"
        )
        if set(window) != {"window_id", "total_usd"}:
            raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid")
        try:
            provider_window = ProviderWindowSummary(
                window_id=str(window["window_id"]),
                total_usd=Decimal(str(window["total_usd"])),
            )
        except (TypeError, ValueError) as exc:
            raise ReconciliationCollectionBlocked("authoritative_snapshot_invalid") from exc
        snapshot = ReconciliationInput(
            audits=rows("audits", AuditRecord),
            reservations=rows("reservations", ReservationRecord),
            local_cost_events=rows("local_cost_events", LocalCostEvent),
            regulus_events=rows("regulus_events", RegulusExecutionEvent),
            action_receipts=rows("action_receipts", ActionReceiptRecord),
            provider_window=provider_window,
        )
        store.ingest_artifact(self.source, "reconciliation/input.json")
        return self.reconciler(store, snapshot)


def _run_bounded(
    *,
    command: Sequence[str],
    working_directory: Path,
    timeout_seconds: int,
    environment: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    if not command or timeout_seconds < 1 or timeout_seconds > 3600:
        raise ValueError("bounded command requires argv and a timeout from 1 to 3600 seconds")
    try:
        return subprocess.run(
            tuple(command),
            cwd=working_directory.resolve(strict=True),
            env={**os.environ, **dict(environment or {})},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return subprocess.CompletedProcess(tuple(command), 124, stdout, stderr + "bounded timeout")


class BoundedPlaywrightProducer:
    def __init__(
        self,
        *,
        artifact_root: Path,
        command: Sequence[str],
        working_directory: Path,
        timeout_seconds: int,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.artifact_root = artifact_root.expanduser().resolve(strict=False)
        self.command = tuple(command)
        self.working_directory = working_directory
        self.timeout_seconds = timeout_seconds
        self.environment = environment

    def __call__(self) -> PlaywrightProductionResult:
        completed = _run_bounded(
            command=self.command,
            working_directory=self.working_directory,
            timeout_seconds=self.timeout_seconds,
            environment={
                **dict(self.environment or {}),
                "ZEROTH_EVALUATION_BROWSER_ROOT": str(self.artifact_root),
            },
        )
        return PlaywrightProductionResult(
            artifact_root=self.artifact_root,
            argv=self.command,
            working_directory=self.working_directory,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class BoundedSnapshotProducer:
    def __init__(
        self,
        *,
        output_path: Path,
        command: Sequence[str],
        working_directory: Path,
        timeout_seconds: int,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_path = output_path.expanduser().resolve(strict=False)
        self.command = tuple(command)
        self.working_directory = working_directory
        self.timeout_seconds = timeout_seconds
        self.environment = environment

    def __call__(self) -> SnapshotProductionResult:
        completed = _run_bounded(
            command=self.command,
            working_directory=self.working_directory,
            timeout_seconds=self.timeout_seconds,
            environment={
                **dict(self.environment or {}),
                "ZEROTH_EVALUATION_RECONCILIATION_OUTPUT": str(self.output_path),
            },
        )
        return SnapshotProductionResult(
            output_path=self.output_path,
            argv=self.command,
            working_directory=self.working_directory,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class BoundedZerothCheckRunner:
    def __init__(
        self,
        *,
        command: Sequence[str],
        working_directory: Path,
        timeout_seconds: int,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.command = tuple(command)
        self.working_directory = working_directory
        self.timeout_seconds = timeout_seconds
        self.environment = environment

    def __call__(self) -> CheckCommandResult:
        completed = _run_bounded(
            command=self.command,
            working_directory=self.working_directory,
            timeout_seconds=self.timeout_seconds,
            environment=self.environment,
        )
        passed = completed.returncode == 0 and bool(_CHECK_PASS.search(completed.stdout))
        return CheckCommandResult(
            argv=self.command,
            working_directory=self.working_directory,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            verdict="pass" if passed else "fail",
        )
