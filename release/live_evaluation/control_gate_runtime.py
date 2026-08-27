"""Concrete, fail-closed executor for the local control-plane gate.

All potentially external behavior is injected.  The executor itself only mutates the
campaign evidence bundle and the configured persistent economics SQLite database.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from zeroth.econ.plane.enforcement import service as enforcement_service
from zeroth.econ.plane.enforcement.models import CostReservation, TenantBudget
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

from .control_gate import (
    BudgetGateProof,
    ChromaCorpusDocument,
    ChromaIdentity,
    ControlPlaneGate,
    PaidProbeExecutor,
    PaidProbeResult,
    SignedAuditReadiness,
)

_TENANT_LIMIT = Decimal("10.00")
_RUN_LIMIT = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class SyntheticChromaDocument:
    document_id: str
    content: str

    @property
    def sha256(self) -> str:
        return f"sha256:{hashlib.sha256(self.content.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class LocalGateResult:
    budget: BudgetGateProof
    audit: SignedAuditReadiness
    chroma: ChromaIdentity
    documents: tuple[ChromaCorpusDocument, ...]


class SignedAuditInspector(Protocol):
    def inspect(self) -> SignedAuditReadiness: ...


class ChromaInspector(Protocol):
    def inspect(self) -> ChromaIdentity: ...


class ChromaSeeder(Protocol):
    def seed_exactly(
        self,
        tenant_id: str,
        documents: tuple[SyntheticChromaDocument, ...],
    ) -> tuple[ChromaCorpusDocument, ...]: ...


_SYNTHETIC_DOCUMENTS = (
    SyntheticChromaDocument(
        "evaluation-ground-truth-alpha",
        "Synthetic evaluation fact alpha: the launch window is 09:30 UTC.",
    ),
    SyntheticChromaDocument(
        "evaluation-ground-truth-beta",
        "Synthetic evaluation fact beta: the approved queue depth is four.",
    ),
    SyntheticChromaDocument(
        "evaluation-conflict-beta",
        "Synthetic conflicting fact beta: an obsolete note says queue depth is six.",
    ),
)


class ControlGateRuntime:
    """Derive gate proofs from persistent services, not caller booleans."""

    def __init__(
        self,
        *,
        gate: ControlPlaneGate,
        econ_database: Path,
        command_working_directory: Path,
    ) -> None:
        self.gate = gate
        self.econ_database = econ_database.expanduser().resolve(strict=True)
        self.command_working_directory = command_working_directory.resolve(strict=True)
        existing_sequences = (
            int(path.name.split("-", 1)[0])
            for path in (self.gate.store.root / "commands").glob("[0-9][0-9][0-9][0-9]-*.json")
        )
        self._command_sequence = max(existing_sequences, default=0)

    def _engine(self) -> Engine:
        return create_engine(
            f"sqlite+pysqlite:///{self.econ_database}",
            future=True,
            connect_args={"check_same_thread": False, "timeout": 5},
        )

    def _scoped(self, engine: Engine) -> tuple[Session, ScopedSession]:
        raw = Session(engine)
        return raw, ScopedSession(
            raw,
            TenantWideScopeContext(tenant_id=self.gate.campaign.tenant_id),
        )

    def _record_step(self, name: str, result: dict[str, object]) -> str:
        self._command_sequence += 1
        path = self.gate.store.record_command(
            sequence=self._command_sequence,
            name=name,
            argv=("zeroth-local-control-gate", name),
            working_directory=self.command_working_directory,
            exit_code=0,
            stdout=json.dumps(result, sort_keys=True),
            stderr="",
        )
        return path.relative_to(self.gate.store.root).as_posix()

    def _reserve(
        self,
        engine: Engine,
        *,
        operation_id: str,
        maximum: Decimal,
        run_id: str | None = None,
        run_cap: Decimal | None = None,
    ) -> None:
        raw, scoped = self._scoped(engine)
        try:
            enforcement_service.reserve_cost(
                scoped,
                operation_id=operation_id,
                max_cost_usd=maximum,
                campaign_id=self.gate.campaign.campaign_id,
                run_id=run_id,
                evidence_kind="synthetic_control",
                run_cap_usd=run_cap,
                require_new=True,
            )
        finally:
            raw.close()

    def _resume_budget_gate(self, engine: Engine, *, prefix: str) -> BudgetGateProof | None:
        """Validate a completed synthetic proof without readmitting its operations."""
        expected = {
            f"{prefix}:concurrent-a": (
                "committed",
                Decimal("0.10"),
                Decimal("0.01"),
                Decimal("0.09"),
                "complete",
            ),
            f"{prefix}:concurrent-b": (
                "released",
                Decimal("0.10"),
                Decimal("0"),
                Decimal("0.10"),
                "provider_not_called",
            ),
            f"{prefix}:tenant-ceiling": (
                "released",
                Decimal("9.99"),
                Decimal("0"),
                Decimal("9.99"),
                "proof_complete",
            ),
            f"{prefix}:restart-recovery": (
                "released",
                Decimal("0.05"),
                Decimal("0"),
                Decimal("0.05"),
                "recovered_and_released",
            ),
        }
        with Session(engine) as session:
            rows = session.execute(
                select(CostReservation).where(
                    CostReservation.tenant_id == self.gate.campaign.tenant_id,
                    CostReservation.operation_id.in_(expected),
                )
            ).scalars().all()
            budget = session.execute(
                select(TenantBudget).where(
                    TenantBudget.tenant_id == self.gate.campaign.tenant_id
                )
            ).scalar_one_or_none()
        if not rows:
            return None
        if len(rows) != len(expected) or budget is None:
            raise RuntimeError("synthetic budget proof is partial; reconciliation is required")
        for row in rows:
            observed = (
                row.status,
                Decimal(row.max_cost_usd),
                Decimal(row.actual_cost_usd or 0),
                Decimal(row.released_cost_usd),
                row.cleanup_status,
            )
            if observed != expected[row.operation_id]:
                raise RuntimeError(
                    "synthetic budget proof state changed; reconciliation is required"
                )
        if Decimal(str(budget.budget_cap_usd)) != _TENANT_LIMIT:
            raise RuntimeError("tenant budget changed after the synthetic proof")
        references = (
            self._record_step(
                "budget-concurrency",
                {"admitted_count": 2, "resumed_from_persistent_state": True},
            ),
            self._record_step(
                "budget-commit-release",
                {"committed_usd": "0.01", "released_usd": "0.19", "resumed": True},
            ),
            self._record_step(
                "budget-rejection",
                {"run_overage_rejected": True, "tenant_overage_rejected": True, "resumed": True},
            ),
            self._record_step(
                "budget-recovery",
                {"recovered": True, "terminal_state_reconciled": True},
            ),
        )
        return BudgetGateProof(
            tenant_limit_usd=_TENANT_LIMIT,
            run_limit_usd=_RUN_LIMIT,
            concurrent_admission_proved=True,
            over_limit_rejected=True,
            commit_and_release_proved=True,
            restart_recovery_proved=True,
            evidence_references=references,
        )

    def _prove_budget_gate(self) -> BudgetGateProof:
        prefix = f"control-gate:{self.gate.campaign.campaign_id}"
        engine = self._engine()
        raw, scoped = self._scoped(engine)
        try:
            enforcement_service.upsert_tenant_budget(
                scoped,
                self.gate.campaign.tenant_id,
                float(_TENANT_LIMIT),
            )
        finally:
            raw.close()

        resumed = self._resume_budget_gate(engine, prefix=prefix)
        if resumed is not None:
            engine.dispose()
            return resumed

        run_id = f"{prefix}:concurrent-run"
        concurrent_operations = (f"{prefix}:concurrent-a", f"{prefix}:concurrent-b")

        def admit(operation_id: str) -> str:
            self._reserve(
                engine,
                operation_id=operation_id,
                maximum=Decimal("0.10"),
                run_id=run_id,
                run_cap=_RUN_LIMIT,
            )
            return operation_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = tuple(pool.map(admit, concurrent_operations))
        if set(admitted) != set(concurrent_operations):
            raise RuntimeError("concurrent budget admission proof is incomplete")
        concurrency_reference = self._record_step(
            "budget-concurrency",
            {"admitted_count": 2, "maximum_each_usd": "0.10", "run_cap_usd": "0.25"},
        )

        run_rejected = False
        try:
            self._reserve(
                engine,
                operation_id=f"{prefix}:run-over-limit",
                maximum=Decimal("0.06"),
                run_id=run_id,
                run_cap=_RUN_LIMIT,
            )
        except enforcement_service.CostReservationDenied as exc:
            run_rejected = "run ceiling" in str(exc)
        if not run_rejected:
            raise RuntimeError("persistent store did not reject the run overage")

        raw, scoped = self._scoped(engine)
        try:
            committed = enforcement_service.commit_cost(
                scoped,
                operation_id=concurrent_operations[0],
                actual_cost_usd=Decimal("0.01"),
                cost_measurement="measured",
                cost_event_id=f"{prefix}:proof-cost",
                cleanup_status="complete",
            )
            released = enforcement_service.release_cost(
                scoped,
                operation_id=concurrent_operations[1],
                cleanup_status="provider_not_called",
            )
            committed_released = Decimal(committed.released_cost_usd)
            released_released = Decimal(released.released_cost_usd)
        finally:
            raw.close()
        commit_release_proved = (
            committed_released == Decimal("0.09")
            and released_released == Decimal("0.10")
        )
        if not commit_release_proved:
            raise RuntimeError("commit/release accounting proof is incomplete")
        commit_reference = self._record_step(
            "budget-commit-release",
            {"committed_usd": "0.01", "released_usd": "0.19"},
        )

        tenant_reservation = f"{prefix}:tenant-ceiling"
        self._reserve(engine, operation_id=tenant_reservation, maximum=Decimal("9.99"))
        tenant_rejected = False
        try:
            self._reserve(
                engine,
                operation_id=f"{prefix}:tenant-over-limit",
                maximum=Decimal("0.00000001"),
            )
        except enforcement_service.CostReservationDenied as exc:
            tenant_rejected = "tenant ceiling" in str(exc)
        raw, scoped = self._scoped(engine)
        try:
            enforcement_service.release_cost(
                scoped,
                operation_id=tenant_reservation,
                cleanup_status="proof_complete",
            )
        finally:
            raw.close()
        over_limit_rejected = run_rejected and tenant_rejected
        if not over_limit_rejected:
            raise RuntimeError("persistent store did not reject both budget overages")
        rejection_reference = self._record_step(
            "budget-rejection",
            {"run_overage_rejected": True, "tenant_overage_rejected": True},
        )

        recovery_operation = f"{prefix}:restart-recovery"
        self._reserve(
            engine,
            operation_id=recovery_operation,
            maximum=Decimal("0.05"),
            run_id=f"{prefix}:recovery-run",
            run_cap=_RUN_LIMIT,
        )
        engine.dispose()
        recovered_engine = self._engine()
        try:
            with Session(recovered_engine) as session:
                recovered = session.execute(
                    select(CostReservation).where(
                        CostReservation.tenant_id == self.gate.campaign.tenant_id,
                        CostReservation.operation_id == recovery_operation,
                    )
                ).scalar_one()
                recovered_status = recovered.status
                recovered_held = Decimal(recovered.held_cost_usd)
            restart_recovery_proved = (
                recovered_status == "reserved"
                and recovered_held == Decimal("0.05")
            )
            if not restart_recovery_proved:
                raise RuntimeError("reservation did not survive persistent-store restart")
            raw, scoped = self._scoped(recovered_engine)
            try:
                enforcement_service.release_cost(
                    scoped,
                    operation_id=recovery_operation,
                    cleanup_status="recovered_and_released",
                )
            finally:
                raw.close()
        finally:
            recovered_engine.dispose()
        recovery_reference = self._record_step(
            "budget-recovery",
            {"held_usd_after_reopen": "0.05", "recovered": True},
        )

        return BudgetGateProof(
            tenant_limit_usd=_TENANT_LIMIT,
            run_limit_usd=_RUN_LIMIT,
            concurrent_admission_proved=True,
            over_limit_rejected=True,
            commit_and_release_proved=True,
            restart_recovery_proved=True,
            evidence_references=(
                concurrency_reference,
                commit_reference,
                rejection_reference,
                recovery_reference,
            ),
        )

    def execute_local_gates(
        self,
        *,
        audit_inspector: SignedAuditInspector,
        chroma_inspector: ChromaInspector,
        chroma_seeder: ChromaSeeder,
    ) -> LocalGateResult:
        budget = self._prove_budget_gate()
        self.gate.record_budget_gate(budget)

        audit = audit_inspector.inspect()
        self.gate.record_signed_audit_readiness(audit)
        self._record_step(
            "signed-audit-readiness",
            {"algorithm": audit.algorithm, "state": audit.state},
        )

        chroma = chroma_inspector.inspect()
        seeded = chroma_seeder.seed_exactly(
            self.gate.campaign.tenant_id,
            _SYNTHETIC_DOCUMENTS,
        )
        expected = tuple(
            ChromaCorpusDocument(
                document_id=item.document_id,
                tenant_id=self.gate.campaign.tenant_id,
                sha256=item.sha256,
            )
            for item in _SYNTHETIC_DOCUMENTS
        )
        if seeded != expected:
            raise ValueError("Chroma seeder did not confirm exactly three expected documents")
        self.gate.record_chroma_readiness(chroma, seeded)
        self._record_step(
            "chroma-seed",
            {
                "document_count": 3,
                "host": chroma.host,
                "image": chroma.image,
                "port": chroma.port,
            },
        )
        return LocalGateResult(
            budget=budget,
            audit=audit,
            chroma=chroma,
            documents=seeded,
        )

    def execute_authorized_probes(
        self,
        *,
        provider_probe: PaidProbeExecutor,
        chroma_probe: PaidProbeExecutor,
    ) -> tuple[PaidProbeResult, PaidProbeResult]:
        reconciled = {
            event["data"]["kind"]
            for event in self.gate.store.read_events()
            if event.get("type") == "control.probe.reconciled"
            and isinstance(event.get("data"), dict)
            and event["data"].get("kind") in {"provider", "chroma"}
        }
        if reconciled:
            raise RuntimeError("control probes are already completed")

        results: list[PaidProbeResult] = []
        for kind, executor in (("provider", provider_probe), ("chroma", chroma_probe)):
            operation_id = f"control-probe:{self.gate.campaign.campaign_id}:{kind}"
            run_id = f"control-run:{self.gate.campaign.campaign_id}:{kind}"
            authorization = self.gate.authorize_paid_probe(
                kind=kind,
                operation_id=operation_id,
                run_id=run_id,
            )
            result = executor.execute_paid_probe(authorization)
            self.gate.reconcile_paid_probe(result)
            self._record_step(
                f"{kind}-probe",
                {
                    "audit_chain_signed": result.audit_chain_signed,
                    "cleanup_state": result.cleanup_state,
                    "kind": result.kind,
                    "measured_cost_usd": str(result.measured_cost_usd),
                    "request_count": result.request_count,
                },
            )
            results.append(result)
        return results[0], results[1]
