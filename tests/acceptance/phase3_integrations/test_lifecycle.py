"""Lifecycle: evidence integrity across tenants, concurrency, crashes, rejected writes, outages."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from contextlib import nullcontext
from contextvars import copy_context
from pathlib import Path
from threading import Thread
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import faults, ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.econ.plane.instrumentation.models import OutcomeEvent as StoredOutcome
from zeroth.instrumentation import Recorder
from zeroth.sdk.errors import (
    ZerothConflictError,
    ZerothEntitlementError,
    ZerothServerError,
    ZerothTransportError,
    ZerothValidationError,
)

HERE = Path(__file__).parent
RECIPES = HERE.parents[2] / "packaging" / "sdk" / "recipes"
SDK_SOURCE = HERE.parents[2] / "packaging" / "sdk" / "src"
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]
EVIDENCE = HERE / "evidence" / "lifecycle.json"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


c01 = load("c01_direct_check_for_lifecycle", RECIPES / "c01_direct" / "check.py")
c02 = load("c02_langgraph_check_for_lifecycle", RECIPES / "c02_langgraph" / "check.py")
Reply = load("c01_direct_fixtures_for_lifecycle", RECIPES / "c01_direct" / "fixtures.py").Reply


def named(workflow: str):
    """The frozen workload under another workflow name, so tenants get separate ledgers."""
    return types.SimpleNamespace(WORKFLOW=workflow, RUNS=workload.RUNS, VERSIONS=workload.VERSIONS,
                                 run_time=workload.run_time)


def record(section: str, data: dict) -> None:
    EVIDENCE.parent.mkdir(exist_ok=True)
    current = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}
    current[section] = data
    EVIDENCE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")


def stored(engine, workflow: str):
    with Session(engine) as db:
        executions = db.scalars(
            select(StoredExecution).where(StoredExecution.workflow_id == workflow)
        ).all()
        outcomes = db.scalars(select(StoredOutcome).where(StoredOutcome.workflow_id == workflow)).all()
        for row in (*executions, *outcomes):
            db.expunge(row)
    return executions, outcomes


def test_two_tenants_run_concurrently_without_leakage(engine, origin):
    plan = {"tenant-a": ("phase3-tenant-a", lambda r, w: c01.reference(r, "v1", w)),
            "tenant-b": ("phase3-tenant-b", lambda r, w: c02.reference(r, "v1", w))}
    errors: list[BaseException] = []

    def work(tenant: str) -> None:
        workflow, run_reference = plan[tenant]
        try:
            run_reference(Recorder(server.sdk(origin, tenant)), named(workflow))
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [Thread(target=copy_context().run, args=(work, tenant)) for tenant in plan]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    summary = {}
    for tenant, (workflow, _) in plan.items():
        assert ledger.read(engine, workflow)["v1"] == EXPECTED["v1"], tenant
        executions, outcomes = stored(engine, workflow)
        assert {row.tenant_id for row in (*executions, *outcomes)} == {tenant}
        assert {row.run_id for row in executions} == {spec["id"] for spec in workload.RUNS}
        assert len({row.execution_id for row in executions}) == len(executions)
        summary[tenant] = {"workflow": workflow, "executions": len(executions), "outcomes": len(outcomes)}
    record("tenants", {"concurrent": True, "models": 4, **summary})


def test_crash_mid_workload_then_rerun_delivers_every_event_once(engine, origin, tmp_path):
    log = tmp_path / "delivered.jsonl"
    env = {**os.environ, "PYTHONPATH": str(SDK_SOURCE)}
    worker = [sys.executable, str(HERE / "lifecycle_worker.py"), origin, server.token(),
              str(HERE / "workload.py"), "v1", str(log)]
    crashed = subprocess.run([*worker, "10"], capture_output=True, text=True, env=env, timeout=120)
    assert crashed.returncode == 17, crashed.stderr
    delivered = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(delivered) == 10 and {d["status"] for d in delivered} == {"inserted"}
    executions, outcomes = stored(engine, workload.WORKFLOW)
    assert len(executions) + len(outcomes) == 10  # rows equal the worker's own delivered log

    rerun = subprocess.run(worker, capture_output=True, text=True, env=env, timeout=120)
    assert rerun.returncode == 0, rerun.stderr
    total = json.loads(rerun.stdout.strip().splitlines()[-1])["delivered"]
    replayed = [json.loads(line) for line in log.read_text().splitlines()][10:]
    assert len(replayed) == total
    assert [d["status"] for d in replayed[:10]] == ["duplicate"] * 10
    assert {d["status"] for d in replayed[10:]} == {"inserted"}
    assert ledger.read(engine, workload.WORKFLOW)["v1"] == EXPECTED["v1"]
    executions, outcomes = stored(engine, workload.WORKFLOW)
    assert len(executions) + len(outcomes) == total
    record("restart", {"crashed_after": 10, "rows_after_crash": 10, "events_total": total,
                       "duplicates_on_rerun": 10, "inserted_on_rerun": total - 10,
                       "flush": "not applicable: delivery is synchronous, nothing is buffered"})


def test_rejected_writes_and_outage_are_reported_exactly_and_replayable(engine, origin):
    table = {3: 402, 5: 409, 7: 422, 9: 503, 11: "drop", 13: 503, 14: 503, 15: 503, 16: 503}
    with faults.faulty(origin, lambda n: table.get(n, "pass")) as (proxy_origin, proxy):
        recorder = Recorder(server.sdk(proxy_origin, timeout=10.0), raise_on_error=False)
        workload.replay(recorder, "v1")
    assert [index for index, _ in proxy.faulted] == sorted(table)
    assert [type(error) for _, error in recorder.lost] == [
        ZerothEntitlementError, ZerothConflictError, ZerothValidationError, ZerothServerError,
        ZerothTransportError, ZerothServerError, ZerothServerError, ZerothServerError, ZerothServerError,
    ]
    executions, outcomes = stored(engine, workload.WORKFLOW)
    assert len(executions) + len(outcomes) == proxy.count - len(table)  # exactly the faulted ones are missing

    direct = Recorder(server.sdk(origin))
    statuses = [direct.deliver(event)["status"] for event, _ in recorder.lost]
    assert statuses == ["inserted"] * len(table)
    assert ledger.read(engine, workload.WORKFLOW)["v1"] == EXPECTED["v1"]
    record("faults", {"requests": proxy.count, "faulted": proxy.faulted,
                      "lost_classes": [type(e).__name__ for _, e in recorder.lost],
                      "replayed_inserted": len(statuses), "ledger_after_replay": "exact"})


def test_passive_measurement_adds_no_provider_or_tool_calls(origin):
    counts = {}
    for instrument in (False, True):
        assistant, oai, ant = c01.build(instrument=instrument)
        tool_calls: list[str] = []
        assistant.search = lambda query, _calls=tool_calls: (_calls.append(query), f"results for {query}")[1]
        oai.expect(Reply(text="plan"), Reply(text="review"), Reply(text="streamed"),
                   Reply(tool_call={"name": "web_search", "arguments": {"query": "q"}}), Reply(text="tooled"),
                   Reply(text="reviewed"))
        ant.expect(Reply(text="answer"), Reply(text="stream-answer"))
        context = Recorder(server.sdk(origin)).run(workload.WORKFLOW, "v1", f"passive-{instrument}").active() \
            if instrument else nullcontext()
        with context:
            outputs = [assistant.plan("q"), assistant.review("q"), assistant.stream_plan("q"),
                       assistant.tool_cycle("q"), assistant.search_then_review("q"), assistant.answer("q"),
                       assistant.stream_answer("q")]
        counts[instrument] = {"provider_requests": len(oai.requests) + len(ant.requests),
                              "tool_calls": len(tool_calls), "outputs": outputs}
    assert counts[False] == counts[True]
    record("side_effects", {"provider_requests": counts[True]["provider_requests"],
                            "tool_calls": counts[True]["tool_calls"], "equal_with_and_without": True})


def test_overhead_is_measured_and_recorded(origin):
    calls = 200
    timings = {}
    for instrument in (False, True):
        assistant, oai, _ = c01.build(instrument=instrument)
        oai.expect(*(Reply(usage=(10, 2, 0)) for _ in range(calls)))
        context = Recorder(server.sdk(origin)).run(workload.WORKFLOW, "v1", "overhead").active() \
            if instrument else nullcontext()
        started = perf_counter()
        with context:
            for _ in range(calls):
                assistant.plan("q")
        timings[instrument] = perf_counter() - started
    overhead = {
        "calls": calls,
        "uninstrumented_s": round(timings[False], 4),
        "instrumented_s": round(timings[True], 4),
        "per_call_ms": round((timings[True] - timings[False]) / calls * 1000, 3),
        "method": "wall-clock over 200 sequential chat.completions calls to an in-process mock provider; "
                  "instrumented runs deliver one event per call synchronously over HTTP to the loopback "
                  "economic server on this machine. A measurement of this setup, not a threshold verdict: "
                  "the A11 adapter-overhead SLO is unset.",
    }
    record("overhead", overhead)
    manifest_path = HERE / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["lifecycle"] = {
        "status": "measured on the loopback server; not an accepted gate",
        "evidence": "evidence/lifecycle.json",
        "overhead_per_call_ms": overhead["per_call_ms"],
        "overhead_method": overhead["method"],
        "not_run": ["hosted backend, real network partition, multi-process shutdown ordering"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
