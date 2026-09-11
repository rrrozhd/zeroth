"""C06: Vercel AI SDK applications captured through the shared contract (Node subprocess)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import faults, ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.econ.plane.instrumentation.models import OutcomeEvent as StoredOutcome

HERE = Path(__file__).parent
RECIPES = HERE.parents[2] / "packaging" / "sdk" / "recipes"
RECIPE = RECIPES / "c06_vercel"
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]
EVENTS_PER_VERSION = 34


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


c07 = load("c07_workers_check_for_c06", RECIPES / "c07_workers" / "check.py")


@pytest.fixture(scope="module")
def installed() -> dict:
    """The application's dependencies from the committed lock, as a customer would install them."""
    subprocess.run(["npm", "ci", "--silent", "--no-audit", "--no-fund"], cwd=RECIPE, check=True,
                   capture_output=True, text=True, timeout=600)
    lock = json.loads((RECIPE / "package-lock.json").read_text())
    versions = {name: lock["packages"][f"node_modules/{name}"]["version"] for name in ("ai", "@ai-sdk/openai", "zod")}
    return {"versions": versions, "lock_sha256": hashlib.sha256((RECIPE / "package-lock.json").read_bytes()).hexdigest(),
            "node": subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()}


def run_check(origin: str, workload_json: Path, *, scenario: str = "reference", version: str = "v1",
              workflow: str, retries: int = 3, timeout_ms: int = 5000, flags: tuple[str, ...] = ()) -> dict:
    result = subprocess.run(
        ["node", *flags, str(RECIPE / "check.ts"), "--zeroth-url", origin, "--zeroth-key", server.token(),
         "--workload", str(workload_json), "--version", version, "--scenario", scenario, "--workflow", workflow,
         "--retries", str(retries), "--timeout-ms", str(timeout_ms)],
        capture_output=True, text=True, timeout=600, cwd=RECIPE, env={**os.environ, "NODE_NO_WARNINGS": "1"},
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.startswith("{")]
    report = json.loads(lines[-1]) if lines else {}
    return {"returncode": result.returncode, "stderr": result.stderr[-3000:], **report}


def charges(engine, workflow: str, run_id: str) -> list[StoredExecution]:
    with Session(engine) as db:
        rows = db.scalars(
            select(StoredExecution)
            .where(StoredExecution.workflow_id == workflow, StoredExecution.run_id == run_id)
            .where(StoredExecution.cost_role == "charge").order_by(StoredExecution.id)
        ).all()
        for row in rows:
            db.expunge(row)
        return rows


def summaries(engine, workflow: str, run_id: str) -> list[StoredExecution]:
    with Session(engine) as db:
        rows = db.scalars(select(StoredExecution).where(
            StoredExecution.workflow_id == workflow, StoredExecution.run_id == run_id,
            StoredExecution.cost_role == "summary")).all()
        for row in rows:
            db.expunge(row)
        return rows


def rows(engine, workflow: str) -> int:
    with Session(engine) as db:
        executions = db.scalars(select(StoredExecution.id).where(StoredExecution.workflow_id == workflow)).all()
        outcomes = db.scalars(select(StoredOutcome.id).where(StoredOutcome.workflow_id == workflow)).all()
    return len(executions) + len(outcomes)


def record(section: str, data: dict) -> None:
    path = HERE / "evidence" / "c06_vercel.json"
    path.parent.mkdir(exist_ok=True)
    current = json.loads(path.read_text()) if path.exists() else {}
    current[section] = data
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")


def test_reference_workload_reconciles_through_the_ai_sdk_app(engine, origin, tmp_path, installed):
    workload_json = c07.export(workload, tmp_path / "workload.json")
    for version in workload.VERSIONS:
        report = run_check(origin, workload_json, version=version, workflow="phase3-vercel")
        assert report["returncode"] == 0, report["stderr"]
        assert report["lost"] == [] and report["delivered"] == EVENTS_PER_VERSION and report["duplicates"] == 0
    assert ledger.read(engine, "phase3-vercel") == EXPECTED
    record("reference", {**installed, "events_per_version": EVENTS_PER_VERSION, "reconciled": "v1, v2"})


def test_modes_multistep_stream_abort_timeout_unknown_and_flush(engine, origin, tmp_path, installed):
    workload_json = c07.export(workload, tmp_path / "workload.json")
    report = run_check(origin, workload_json, scenario="modes", workflow="phase3-vercel-modes")
    assert report["returncode"] == 0, report["stderr"]
    assert report["lost"] == []
    wf = "phase3-vercel-modes"
    multi = {r.step_id: r for r in charges(engine, wf, "modes-multistep")}
    # the SDK executes the tool inside step 1, so the tool charge lands before that step's charge
    assert {step: r.event_metadata["charge_kind"] for step, r in multi.items()} == {
        "answer": "model", "web_search": "tool", "answer#2": "model"}
    assert [multi[s].event_metadata["usage"]["input_tokens"] for s in ("answer", "answer#2")] == [20, 60]
    assert multi["answer#2"].event_metadata["usage"]["cache_read_tokens"] == 10
    assert multi["answer#2"].event_metadata["provider_request_id"] is not None
    [summary] = summaries(engine, wf, "modes-multistep")
    assert summary.token_cost_usd is None and summary.event_metadata["total_usage"]["inputTokens"] == 90
    assert report["report"]["multistep"] == {"text": "answer", "steps": 2}
    stream = charges(engine, wf, "modes-stream")
    assert [(r.cost_measurement, r.event_metadata["usage"]["input_tokens"]) for r in stream] == [("estimated", 10)]
    assert report["report"]["stream"] == "streamed"
    aborted = charges(engine, wf, "modes-stream-abort")
    assert [(r.cost_measurement, r.event_metadata["error"], r.event_metadata["stream"]) for r in aborted] == [
        ("unmeasured", "aborted", "aborted")]
    timeout = charges(engine, wf, "modes-timeout")
    assert [(r.cost_measurement, r.event_metadata["error"]) for r in timeout] == [("unmeasured", report["report"]["timeoutError"])]
    assert report["report"]["timeoutError"] in ("TimeoutError", "AbortError")
    unknown = charges(engine, wf, "modes-unknown")
    assert [(r.cost_measurement, r.event_metadata["pricing"]) for r in unknown] == [("unmeasured", "unknown_model")]
    assert report["delivered"] == rows(engine, wf)  # everything flushed before the process exited
    record("modes", {"multistep": report["report"]["multistep"], "timeout_error": report["report"]["timeoutError"],
                     "provider_requests": report["report"]["providerRequests"], "delivered": report["delivered"],
                     "rows": rows(engine, wf), "flushed_before_exit": True})


def test_retry_through_faults_reaches_exactly_once(engine, origin, tmp_path, installed):
    workload_json = c07.export(workload, tmp_path / "workload.json")
    table = {2: 503, 4: "drop", 6: 503, 7: 503}

    def schedule(index: int):
        if index == 9:
            time.sleep(5)  # outlasts the client's 3 s timeout: abandoned, retried, and still lands
        return table.get(index, "pass")

    with faults.faulty(origin, schedule) as (proxy_origin, proxy):
        report = run_check(proxy_origin, workload_json, workflow="phase3-vercel-faults", retries=6, timeout_ms=3000)
    assert report["returncode"] == 0, report["stderr"]
    assert report["lost"] == [] and report["delivered"] == EVENTS_PER_VERSION
    # the nine per-run delivery queues share one loopback server; at 500 ms their queueing alone
    # timed deliveries out on CI until events were lost, so the timeout leaves that headroom.
    # Each scheduled fault and the abandoned request cost one more request, whichever events they
    # hit; faulty() waited for the abandoned one, and exactly-once holds by the rows and ledger below
    assert proxy.count >= EVENTS_PER_VERSION + len(table) + 1
    assert ledger.read(engine, "phase3-vercel-faults")["v1"] == EXPECTED["v1"]
    assert rows(engine, "phase3-vercel-faults") == EVENTS_PER_VERSION
    record("retry", {"faulted": proxy.faulted, "events_retried": report["retried"], "rows": EVENTS_PER_VERSION})


def test_recipe_is_standalone(installed):
    package = json.loads((RECIPE / "package.json").read_text())
    assert not any("zeroth" in name for name in package["dependencies"])
    capture = (RECIPE / "capture.ts").read_text()
    assert "from \"" not in capture and "require(" not in capture  # capture.ts imports nothing
    assert "python" not in capture.lower()
    record("standalone", {"dependencies": package["dependencies"], "capture_imports": [], **installed})
