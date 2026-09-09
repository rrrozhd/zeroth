"""Source counts and money stay independent of HTTP acknowledgements and reports."""

from tests.econ.assertions import assert_interval_abstention

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

from fastapi import Depends, FastAPI
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
import uvicorn

from tests.econ_plane.test_sdk_evidence_namespace import engine as database_engine
from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.cloud.api import router as cloud_router
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.decisioning.api import router as decision_router
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.econ.plane.performance import models as performance_models  # noqa: F401
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext
from zeroth.protocol import (
    ExecutionEvent,
    OutcomeDefinition,
    OutcomeEvent,
    VersionComparisonRequest,
)
from zeroth.sdk import ZerothClient, ZerothTransportError

engine = database_engine
SOURCE = Path(__file__).with_name("fixtures") / "source_ledger.json"
UNITS = 100_000_000


@pytest.fixture
def ledger():
    return json.loads(SOURCE.read_text())


@pytest.fixture
def origin(engine):
    app = FastAPI()
    for router in (cloud_router, decision_router, instrumentation_router):
        app.include_router(router, prefix="/v1")

    def database():
        with Session(engine) as raw:
            yield raw

    def scoped_database(user=Depends(get_current_user)):  # noqa: B008
        with Session(engine) as raw:
            yield ScopedSession(raw, TenantWideScopeContext(tenant_id=user.tenant_id))

    # Only database construction changes; authentication, roles, tenant claims,
    # validation, ingestion, retained reports and serialization remain real.
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_scoped_db] = scoped_database
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "economic routes did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "economic HTTP listener did not stop"


def client(origin, tenant="tenant-a", **kwargs):
    return ZerothClient(api_key=mint_econ_service_token(tenant), base_url=origin, **kwargs)


def source_id(version, charge):
    return f"{version}:{charge['run']}:{charge['operation']}:{charge['attempt']}"


def request_from_source(ledger):
    windows = {}
    for side, version in ledger["versions"].items():
        runs = []
        for run in ledger["runs"]:
            ids = [source_id(version, c) for c in ledger["charges"] if c["run"] == run["id"]]
            ids.append(f"{version}:{run['id']}:summary")
            # The source ID list is fixed before constructing or sending events.
            encoded = json.dumps(sorted(ids, key=lambda x: x.encode()), separators=(",", ":"))
            runs.append(
                dict(
                    run_id=run["id"],
                    terminal_state=run["terminal"],
                    execution_count=len(ids),
                    execution_ids_digest=hashlib.sha256(encoded.encode()).hexdigest(),
                )
            )
        windows[side] = dict(
            source_window_id="source-close-1",
            opened_at=ledger["opened_at"],
            closed_at=ledger["closed_at"],
            runs=runs,
        )
    return VersionComparisonRequest(
        workflow=ledger["workflow"],
        baseline_version="before",
        candidate_version="after",
        source_windows=windows,
        policy={"min_runs": 4, "min_success_rate": 0.5},
    )


def packets(ledger, version):
    result = []
    for charge in ledger["charges"]:
        units = charge[version]
        state = "unmeasured" if units is None else charge.get(f"{version}_state", "measured")
        ident = source_id(version, charge)
        result.append(
            ExecutionEvent(
                workflow=ledger["workflow"],
                workflow_version=version,
                run_id=charge["run"],
                step=charge["operation"],
                attempt=charge["attempt"],
                event_id=ident,
                source_window_id="source-close-1",
                recorded_at=ledger["opened_at"],
                cost_role="charge",
                charge_id=f"charge:{ident}",
                cost_usd=None if units is None else Decimal(units) / UNITS,
                cost_measurement=state,
                metadata={"source_category": charge["category"]},
            )
        )
    for run in ledger["runs"]:
        result.append(
            ExecutionEvent(
                workflow=ledger["workflow"],
                workflow_version=version,
                run_id=run["id"],
                step="summary",
                event_id=f"{version}:{run['id']}:summary",
                cost_role="summary",
                source_window_id="source-close-1",
                recorded_at=ledger["closed_at"],
            )
        )
    return result


def labels(sdk, ledger, version):
    sdk.create_outcome_definition(
        OutcomeDefinition(
            workflow_id=ledger["workflow"],
            workflow_version=version,
            outcome_type="accepted",
            operator="equals",
            target=True,
        )
    )
    for run in ledger["runs"]:
        sdk.record_outcome(
            OutcomeEvent(
                workflow=ledger["workflow"],
                workflow_version=version,
                run_id=run["id"],
                accepted=run["accepted"],
                maturity=run.get(f"{version}_maturity", "final"),
                occurred_at="2026-09-06T00:30:00Z",
            )
        )


def assert_source_report(report, ledger):
    # Source amounts/counts, not normalized calculation_inputs, are the oracle.
    accepted = sum(run["accepted"] for run in ledger["runs"])
    for side, version in ledger["versions"].items():
        total = Fraction(sum(c[version] for c in ledger["charges"]), UNITS)
        assert Fraction(report[side]["measured_cost_usd"]) == total
        assert Fraction(report[side]["cost_per_accepted_outcome_usd"]) == total / accepted
        assert report[side]["runs"] == len(ledger["runs"])
        assert report[side]["accepted_runs"] == accepted
        assert report[side]["rejected_runs"] == len(ledger["runs"]) - accepted
        delivery = report["source_delivery"][side]
        assert delivery["status"] == "matched"
        assert delivery["observed_executions"] == len(ledger["charges"]) + len(ledger["runs"])
        assert report["charge_ownership"][side]["owned_charge_records"] == len(ledger["charges"])
        assert report["charge_ownership"][side]["summary_records"] == len(ledger["runs"])
    assert "source_completeness_unverified" in report["limitations"]


class LostReply(httpx.HTTPTransport):
    """Lose one response only after real HTTP ingestion has completed."""

    def handle_request(self, request):
        response = super().handle_request(request)
        response.read()
        assert response.status_code == 200
        response.close()
        raise httpx.ReadTimeout("injected lost acknowledgement after commit", request=request)


@pytest.mark.parametrize(
    "fault", ["complete", "connection_refused", "rejected", "lost_reply", "process_exit"]
)
def test_source_ledger_delivery_faults_and_recovery(origin, engine, ledger, fault):
    request = request_from_source(ledger)  # Frozen before any HTTP delivery.
    sdk = client(origin)
    try:
        # Register each version with a source summary before sending outcomes.
        # Labels can then precede their charged events without violating the API.
        for version in ledger["versions"].values():
            sdk.record_execution(packets(ledger, version)[-1])
            labels(sdk, ledger, version)
        for event in packets(ledger, "before"):
            sdk.record_execution(event)
        all_events = packets(ledger, "after")
        events = all_events[:-1]  # The registration summary is already retained.
        if fault == "complete":
            def record(event):
                try:
                    return sdk.record_execution(event)
                except httpx.HTTPStatusError as error:
                    raise AssertionError(f"{event.event_id}: {error.response.text}") from error

            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(record, list(reversed(events)) + events))
            assert sum(r["status"] == "inserted" for r in responses) == len(events)
            assert sum(r["status"] == "duplicate" for r in responses) == len(events)
            missing = 0
        elif fault == "process_exit":
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "packaging/sdk/src")
            worker = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import json, os, sys
from zeroth.protocol import ExecutionEvent
from zeroth.sdk import ZerothClient, ZerothTransportError
source = json.load(sys.stdin)
client = ZerothClient(api_key=source['token'], base_url=source['origin'])
for payload in source['events'][:source['stop_after']]:
    client.record_execution(ExecutionEvent.model_validate(payload))
os._exit(17)
""",
                ],
                input=json.dumps(
                    {
                        "token": mint_econ_service_token("tenant-a"),
                        "origin": origin,
                        "events": [e.model_dump(mode="json") for e in events],
                        "stop_after": len(events) // 2,
                    }
                ),
                text=True,
                capture_output=True,
                env=environment,
                timeout=15,
            )
            assert worker.returncode == 17, worker.stderr
            missing = len(events) - len(events) // 2
        else:
            target, remaining = events[0], events[1:]
            for event in remaining:
                sdk.record_execution(event)
            if fault == "connection_refused":
                with socket.socket() as stopped:
                    stopped.bind(("127.0.0.1", 0))  # Reserved but not listening.
                    failed = client(f"http://127.0.0.1:{stopped.getsockname()[1]}", timeout=0.2)
                    try:
                        with pytest.raises(ZerothTransportError) as transport_error:
                            failed.record_execution(target)
                        assert isinstance(
                            transport_error.value.original_error,
                            (httpx.ConnectError, httpx.ConnectTimeout),
                        )
                    finally:
                        failed.close()
            elif fault == "rejected":
                with pytest.raises(httpx.HTTPStatusError) as rejected:
                    sdk.record_execution(
                        target.model_copy(update={"charge_id": remaining[0].charge_id})
                    )
                assert rejected.value.response.status_code == 422
            else:
                with httpx.Client(transport=LostReply()) as transport:
                    failed = client(origin, http_client=transport)
                    with pytest.raises(ZerothTransportError) as transport_error:
                        failed.record_execution(target)
                    assert isinstance(transport_error.value.original_error, httpx.ReadTimeout)
            missing = 0 if fault == "lost_reply" else 1
        damaged = sdk.compare_versions(request)
        assert damaged["source_delivery"]["candidate"]["expected_executions"] == len(all_events)
        assert (
            damaged["source_delivery"]["candidate"]["observed_executions"]
            == len(all_events) - missing
        )
        assert damaged["verdict"] == "abstain"
        if not missing:
            assert_interval_abstention(damaged)
        if missing:
            assert damaged["source_delivery"]["candidate"]["status"] == "mismatch"
            assert damaged["candidate"]["unmeasured_runs"] > 0
            assert damaged["candidate"]["cost_per_accepted_outcome_usd"] is None
        results = [sdk.record_execution(event) for event in events]
        assert sum(r["status"] == "inserted" for r in results) == missing
        repaired = sdk.compare_versions(request)
        assert_source_report(repaired, ledger)
        assert_interval_abstention(repaired)
        assert (repaired["decision_id"] != damaged["decision_id"]) == bool(missing)
        assert damaged in sdk.list_decisions()  # Repair never rewrites the old record.
        with Session(engine) as raw:
            charges = list(
                raw.scalars(select(StoredExecution).where(StoredExecution.cost_role == "charge"))
            )
            assert len(charges) == 2 * len(ledger["charges"])
            for row in charges:
                source = next(
                    c
                    for c in ledger["charges"]
                    if source_id(row.workflow_version, c) == row.execution_id
                )
                assert row.attempt == source["attempt"]
                assert row.token_cost_usd == Decimal(source[row.workflow_version]) / UNITS
                assert row.event_metadata["source_category"] == source["category"]
        with httpx.Client(
            base_url=origin,
            headers={"Authorization": f"Bearer {mint_econ_service_token('tenant-a')}"},
        ) as reader:
            response = reader.get(
                "/v1/debugger/breakage", params={"workflow_id": ledger["workflow"]}
            )
            assert response.status_code == 200, response.text
            failed = {r["id"] for r in ledger["runs"] if not r["accepted"]}
            for version in ledger["versions"].values():
                rows = [row for row in response.json() if row["workflow_version"] == version]
                failed_charges = [c for c in ledger["charges"] if c["run"] in failed]
                for field, source in (
                    ("measured_failure_exposure_usd", failed_charges),
                    (
                        "measured_repeated_attempt_cost_usd",
                        [c for c in failed_charges if c["attempt"] > 1],
                    ),
                ):
                    assert sum(Fraction(str(row[field])) for row in rows) == Fraction(
                        sum(c[version] for c in source), UNITS
                    )
    finally:
        sdk.close()
