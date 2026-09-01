"""Generate a local, synthetic economic-debugger evidence pack."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.debugger.schemas import OutcomeDefinitionCreate
from zeroth.econ.plane.debugger.service import create_outcome_definition, diagnostic_report
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate
from zeroth.econ.plane.instrumentation.service import ingest_execution, ingest_outcome
from zeroth.econ.plane.reconciliation.schemas import ProviderBillImportRequest
from zeroth.econ.plane.reconciliation.service import import_provider_bill, provider_bill_report
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOTICE = "Synthetic example — not customer evidence"
_WORKFLOW_ID = "invoice-processing"
_VERSION = "v1"
_START = datetime(2026, 8, 1, tzinfo=UTC)
_END = _START + timedelta(days=31)


def _event(
    execution_id: str,
    run_id: str,
    *,
    cost: str,
    attempt: int = 1,
    plan: str,
) -> ExecutionEventCreate:
    return ExecutionEventCreate(
        execution_id=execution_id,
        join_key=run_id,
        timestamp=_START + timedelta(days=2),
        capability_id=_WORKFLOW_ID,
        implementation_id=f"{_WORKFLOW_ID}:{_VERSION}",
        model_version="gpt-5",
        token_cost_usd=Decimal(cost),
        cost_measurement=MeasurementState.MEASURED,
        workflow_id=_WORKFLOW_ID,
        workflow_version=_VERSION,
        run_id=run_id,
        step_id="extract",
        attempt=attempt,
        subject_id=f"account-{plan}",
        dimensions={"plan": plan},
        metadata={"provider": "openai", "model": "gpt-5", "project_id": "proj-a"},
    )


def _seed(db: ScopedSession) -> None:
    create_outcome_definition(
        db,
        OutcomeDefinitionCreate(
            workflow_id=_WORKFLOW_ID,
            workflow_version=_VERSION,
            outcome_type="approval",
            operator="equals",
            target=True,
        ),
    )
    events = (
        _event("success-event", "success-run", cost="0.10", plan="team"),
        _event("failure-attempt-1", "failure-run", cost="0.30", plan="free"),
        _event(
            "failure-attempt-2", "failure-run", cost="0.10", attempt=2, plan="free"
        ),
    )
    for event in events:
        ingest_execution(db, event)
    for event, approved in ((events[0], True), (events[2], False)):
        ingest_outcome(
            db,
            OutcomeEventCreate(
                execution_id=event.execution_id,
                join_key=event.run_id,
                capability_id=_WORKFLOW_ID,
                implementation_id=f"{_WORKFLOW_ID}:{_VERSION}",
                outcome_type="approval",
                outcome_value=approved,
                occurred_at=_START + timedelta(days=3),
            ),
        )
    import_provider_bill(
        db,
        ProviderBillImportRequest(
            statement_id="openai-demo-2026-08",
            provider="openai",
            period_start=_START,
            period_end=_END,
            billed_total_usd=Decimal("0.50"),
            source_kind="manual",
            buckets=[
                {
                    "bucket_id": "gpt-5-proj-a",
                    "period_start": _START,
                    "period_end": _END,
                    "amount_usd": Decimal("0.50"),
                    "model": "gpt-5",
                    "provider_dimensions": {"project_id": "proj-a"},
                }
            ],
        ),
    )


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def generate_demo_pack(
    output_dir: Path,
    *,
    render_diagnostic: Callable[[dict], str],
    render_reconciliation: Callable[[dict], str],
) -> Path:
    """Write a complete pack atomically and refuse to replace any existing path."""
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as raw:
            db = ScopedSession(raw, TenantWideScopeContext(tenant_id="demo"))
            _seed(db)
            diagnostic = diagnostic_report(
                db, workflow_id=_WORKFLOW_ID, cohort_dimension="plan"
            )
            reconciliation = provider_bill_report(
                db, provider="openai", statement_id="openai-demo-2026-08"
            )
        if diagnostic is None or reconciliation is None:
            raise RuntimeError("synthetic evidence did not produce both reports")
        diagnostic_data = diagnostic.model_dump(mode="json")
        reconciliation_data = reconciliation.model_dump(mode="json")
        if reconciliation_data["reconciliation_state"] != "reconciled":
            raise RuntimeError("synthetic provider bill did not close")
        (staging / "README.md").write_text(
            "\n".join(
                [
                    "# Zeroth economic debugger demo",
                    "",
                    f"> **{_NOTICE}.**",
                    "",
                    "This pack demonstrates Zeroth's claim boundaries on fixed local data. It",
                    "observes failed-run and repeated-attempt cost, segments breakage by plan,",
                    "and closes the same measured dollars to a synthetic provider bill.",
                    "",
                    "Use real instrumentation and a real provider export before making a",
                    "production or purchasing decision. This demo does not establish savings.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (staging / "economic-diagnostic.json").write_text(
            _json(diagnostic_data), encoding="utf-8"
        )
        (staging / "economic-diagnostic.md").write_text(
            f"> **{_NOTICE}.**\n\n{render_diagnostic(diagnostic_data)}",
            encoding="utf-8",
        )
        (staging / "provider-reconciliation.json").write_text(
            _json(reconciliation_data), encoding="utf-8"
        )
        (staging / "provider-reconciliation.md").write_text(
            f"> **{_NOTICE}.**\n\n{render_reconciliation(reconciliation_data)}",
            encoding="utf-8",
        )
        staging.rename(output)
        return output
    finally:
        engine.dispose()
        if staging.exists():
            shutil.rmtree(staging)


__all__ = ["generate_demo_pack"]
