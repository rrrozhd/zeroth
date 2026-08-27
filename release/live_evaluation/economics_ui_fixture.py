"""Disposable provider-independent economics fixture for real console checks.

The fixture refuses the preserved campaign state root and only binds its HTTP
server to loopback. It creates durable run/audit records directly; it never
constructs an LLM runner or performs an external provider call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from scripts.economics_ui_fixture_environment import (
    FIXTURE_DEPLOYMENT,
    FIXTURE_TENANT,
    assert_disposable_state_root,
    fixture_environment,
)

FIXTURE_WORKFLOW = "economics-ui-fixture"
FIXTURE_MODEL = "fixture/local-economics-model-v1"
FIXTURE_NODE = "economics-agent"
MEASURED_RUN_ID = "economics-ui-measured-success-v1"
FAILED_RUN_ID = "economics-ui-estimated-failure-v1"
MEASURED_AUDIT_ID = "economics-ui-measured-audit-v1"
FAILED_AUDIT_ID = "economics-ui-estimated-audit-v1"
MEASURED_LEDGER_EVENT_ID = "economics-ui-measured-ledger-v1"
MEASURED_COST_USD = 0.12
ESTIMATED_COST_USD = 0.08


@dataclass(frozen=True, slots=True)
class EconomicsUIFixture:
    measured_run: object
    failed_run: object
    measured_audit: object
    failed_audit: object


async def seed_economics_records(
    database,
    *,
    tenant_id: str = FIXTURE_TENANT,
    deployment_ref: str = FIXTURE_DEPLOYMENT,
    graph_version_ref: str = "economics-ui-fixture-graph@1",
) -> EconomicsUIFixture:
    """Create the two idempotent durable records used by the UI fixture."""
    from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage
    from zeroth.governance.audit.repository import AuditRepository
    from zeroth.integrations.persistence.runs.run_repository import RunRepository
    from zeroth.platform.storage import NullWorkspaceScopeContext
    from zeroth.runtime.runs import Run, RunFailureState, RunStatus

    scope = NullWorkspaceScopeContext(tenant_id)
    runs = RunRepository(database, scope)
    audits = AuditRepository.scoped(database, scope)

    measured_run = await runs.get(MEASURED_RUN_ID)
    if measured_run is None:
        measured_run = await runs.create(
            Run(
                run_id=MEASURED_RUN_ID,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                workflow_name=FIXTURE_WORKFLOW,
                tenant_id=tenant_id,
                status=RunStatus.COMPLETED,
                audit_refs=[MEASURED_AUDIT_ID],
                final_output={"fixture_result": "measured-success"},
            )
        )

    failed_run = await runs.get(FAILED_RUN_ID)
    if failed_run is None:
        failed_run = await runs.create(
            Run(
                run_id=FAILED_RUN_ID,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                workflow_name=FIXTURE_WORKFLOW,
                tenant_id=tenant_id,
                status=RunStatus.FAILED,
                audit_refs=[FAILED_AUDIT_ID],
                failure_state=RunFailureState(
                    reason="fixture_failure",
                    message="Deterministic provider-independent fixture failure",
                ),
            )
        )

    measured_audit = await audits.get(MEASURED_AUDIT_ID)
    if measured_audit is None:
        measured_audit = await audits.write(
            NodeAuditRecord(
                audit_id=MEASURED_AUDIT_ID,
                run_id=MEASURED_RUN_ID,
                thread_id=MEASURED_RUN_ID,
                node_id=FIXTURE_NODE,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                tenant_id=tenant_id,
                status="completed",
                token_usage=TokenUsage(
                    input_tokens=1200,
                    output_tokens=300,
                    total_tokens=1500,
                    model_name=FIXTURE_MODEL,
                ),
                cost_usd=MEASURED_COST_USD,
                cost_event_id="economics-ui-measured-cost-event-v1",
            )
        )

    failed_audit = await audits.get(FAILED_AUDIT_ID)
    if failed_audit is None:
        failed_audit = await audits.write(
            NodeAuditRecord(
                audit_id=FAILED_AUDIT_ID,
                run_id=FAILED_RUN_ID,
                thread_id=FAILED_RUN_ID,
                node_id=FIXTURE_NODE,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                tenant_id=tenant_id,
                status="failed",
                token_usage=TokenUsage(
                    input_tokens=800,
                    output_tokens=200,
                    total_tokens=1000,
                    model_name=FIXTURE_MODEL,
                ),
                estimated_cost_usd=ESTIMATED_COST_USD,
                error="Deterministic provider-independent fixture failure",
            )
        )

    return EconomicsUIFixture(
        measured_run=measured_run,
        failed_run=failed_run,
        measured_audit=measured_audit,
        failed_audit=failed_audit,
    )


def seed_measured_ledger_event() -> None:
    """Mirror only the measured fixture event into the isolated production ledger."""
    from sqlalchemy import select

    # Importing the plane app registers every mapped table before create_all;
    # importing the instrumentation model alone leaves cross-domain FK targets
    # absent from Base.metadata.
    import zeroth.econ.plane.main  # noqa: F401
    from zeroth.econ.plane.common.bootstrap import bootstrap as bootstrap_econ
    from zeroth.econ.plane.database import SessionLocal
    from zeroth.econ.plane.instrumentation.models import ExecutionEvent

    bootstrap_econ()
    with SessionLocal() as session:
        existing = session.execute(
            select(ExecutionEvent).where(
                ExecutionEvent.tenant_id == FIXTURE_TENANT,
                ExecutionEvent.execution_id == MEASURED_LEDGER_EVENT_ID,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        session.add(
            ExecutionEvent(
                tenant_id=FIXTURE_TENANT,
                execution_id=MEASURED_LEDGER_EVENT_ID,
                join_key=MEASURED_RUN_ID,
                timestamp=datetime.now(UTC).replace(tzinfo=None),
                capability_id=FIXTURE_NODE,
                implementation_id=FIXTURE_MODEL,
                deployment_ref=FIXTURE_DEPLOYMENT,
                evidence_kind="production",
                model_version=FIXTURE_MODEL,
                token_cost_usd=Decimal(str(MEASURED_COST_USD)),
                tool_cost_usd=Decimal("0"),
                compute_cost_usd=Decimal("0"),
                cost_measurement="measured",
                usage_measurement="measured",
                latency_ms=1,
                compute_time_ms=0,
                event_metadata={
                    "run_id": MEASURED_RUN_ID,
                    "fixture_kind": "provider-independent-economics-ui",
                },
            )
        )
        session.commit()


def _install_environment(state_root: Path, *, console_origin: str) -> Path:
    root = assert_disposable_state_root(state_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ.update(fixture_environment(root, console_origin=console_origin))
    return root


async def prepare_fixture(
    state_root: Path,
    *,
    console_origin: str = "http://127.0.0.1:3000",
) -> dict[str, object]:
    """Migrate and seed the isolated fixture database without serving it."""
    root = _install_environment(state_root, console_origin=console_origin)
    from release.live_evaluation.bootstrap import seed_campaign_bootstrap
    from zeroth.platform.config.settings import get_settings
    from zeroth.platform.storage.factory import create_database
    from zeroth.service.cli import ensure_schema

    ensure_schema()
    database = await create_database(get_settings())
    try:
        deployment = await seed_campaign_bootstrap(
            database,
            tenant_id=FIXTURE_TENANT,
            deployment_ref=FIXTURE_DEPLOYMENT,
            model=FIXTURE_MODEL,
        )
        fixture = await seed_economics_records(
            database,
            graph_version_ref=deployment.graph_version_ref,
        )
        seed_measured_ledger_event()
    finally:
        await database.close()

    summary: dict[str, object] = {
        "fixture_kind": "provider-independent-economics-ui",
        "tenant_id": FIXTURE_TENANT,
        "deployment_ref": FIXTURE_DEPLOYMENT,
        "graph_version_ref": deployment.graph_version_ref,
        "model_identity": FIXTURE_MODEL,
        "measured_run_id": fixture.measured_run.run_id,
        "failed_run_id": fixture.failed_run.run_id,
        "measured_cost_usd": MEASURED_COST_USD,
        "estimated_failure_cost_usd": ESTIMATED_COST_USD,
        "external_provider_calls": 0,
    }
    (root / "fixture-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


async def serve_fixture(
    state_root: Path,
    *,
    port: int,
    console_origin: str,
) -> None:
    """Serve the seeded fixture on loopback only."""
    if not 1024 <= port <= 65535:
        raise ValueError("fixture port must be between 1024 and 65535")
    await prepare_fixture(state_root, console_origin=console_origin)
    import uvicorn

    from zeroth.service.entrypoint import _bootstrap

    app = await _bootstrap()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        proxy_headers=False,
        log_level="info",
    )
    await uvicorn.Server(config).serve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "serve"):
        child = subparsers.add_parser(command)
        child.add_argument("--state-root", type=Path, required=True)
        child.add_argument("--console-origin", default="http://127.0.0.1:3000")
        if command == "serve":
            child.add_argument("--port", type=int, default=8312)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        summary = asyncio.run(
            prepare_fixture(args.state_root, console_origin=args.console_origin)
        )
        print(json.dumps(summary, sort_keys=True))
        return 0
    asyncio.run(
        serve_fixture(
            args.state_root,
            port=args.port,
            console_origin=args.console_origin,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
