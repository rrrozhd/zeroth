"""C07: explicit Python and TypeScript workers on the sold HTTP contract."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import faults, ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.econ.plane.instrumentation.models import OutcomeEvent as StoredOutcome

HERE = Path(__file__).parent
RECIPE = HERE.parents[2] / "packaging" / "sdk" / "recipes" / "c07_workers"
SDK_SOURCE = HERE.parents[2] / "packaging" / "sdk" / "src"
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]
EVENTS_PER_VERSION = 34
VOLATILE = ("decision_id", "evaluated_at", "workflow", "source_evidence", "calculation_inputs")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = load("c07_workers_check", RECIPE / "check.py")
NODE = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()


def normalised(value):
    """Drop fields that differ by construction: ids, times, the workflow name and its digests."""
    if isinstance(value, dict):
        return {key: normalised(item) for key, item in value.items()
                if key not in VOLATILE and key != "definition_digest"}
    if isinstance(value, list):
        return [normalised(item) for item in value]
    return value


def rows(engine, workflow: str) -> int:
    with Session(engine) as db:
        executions = db.scalars(select(StoredExecution.id).where(StoredExecution.workflow_id == workflow)).all()
        outcomes = db.scalars(select(StoredOutcome.id).where(StoredOutcome.workflow_id == workflow)).all()
    return len(executions) + len(outcomes)


def record(section: str, data: dict) -> None:
    path = HERE / "evidence" / "c07_workers.json"
    path.parent.mkdir(exist_ok=True)
    current = json.loads(path.read_text()) if path.exists() else {}
    current[section] = data
    current["node"] = NODE
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")


def test_typescript_and_python_workers_reconcile_and_agree(engine, origin, tmp_path):
    from zeroth.protocol import DecisionPolicy, OutcomeDefinition, VersionComparisonRequest

    ts_json = check.export(workload, tmp_path / "ts.json", workflow="phase3-worker-ts")
    py_json = check.export(workload, tmp_path / "py.json", workflow="phase3-worker-py")
    env = {**os.environ, "PYTHONPATH": str(SDK_SOURCE)}
    for version in workload.VERSIONS:
        ts = check.run_node(origin, server.token(), ts_json, version)
        assert ts["returncode"] == 0 and ts["lost"] == [] and ts["duplicates"] == 0, ts
        assert ts["delivered"] == EVENTS_PER_VERSION
        py = check.run_python(origin, server.token(), py_json, version, env=env)
        assert py["returncode"] == 0 and py["lost"] == [], py
    assert ledger.read(engine, "phase3-worker-ts") == EXPECTED
    assert ledger.read(engine, "phase3-worker-py") == EXPECTED

    ts_decision = check.compare_node(origin, server.token(), ts_json, "v1", "v2")["decision"]
    sdk = server.sdk(origin)
    for version in workload.VERSIONS:
        sdk.create_outcome_definition(OutcomeDefinition(
            workflow_id="phase3-worker-py", workflow_version=version, outcome_type="accepted",
            operator="equals", target=True,
        ))
    py_decision = sdk.compare_versions(VersionComparisonRequest(
        workflow="phase3-worker-py", baseline_version="v1", candidate_version="v2",
        policy=DecisionPolicy(min_runs=4, min_success_rate=0.5, allow_estimated_cost=True),
    ))
    assert normalised(ts_decision) == normalised(py_decision)
    assert ts_decision["verdict"] == py_decision["verdict"]
    record("parity", {"versions": list(workload.VERSIONS), "events_per_version": EVENTS_PER_VERSION,
                      "verdict": py_decision["verdict"], "reason_codes": py_decision["reason_codes"],
                      "normalised_equal": True, "volatile_fields_ignored": list(VOLATILE)})


def test_typescript_worker_retries_retryable_faults_to_exactly_once(engine, origin, tmp_path):
    ts_json = check.export(workload, tmp_path / "ts.json", workflow="phase3-worker-ts-faults")
    table = {2: 503, 4: "drop", 6: 503, 7: 503}

    def schedule(index: int):
        if index == 9:
            time.sleep(2)  # the client aborts at 500 ms; the request still lands: replay-safe retry
        return table.get(index, "pass")

    with faults.faulty(origin, schedule) as (proxy_origin, proxy):
        report = check.run_node(proxy_origin, server.token(), ts_json, "v1", retries=6, timeout_ms=500)
    assert report["returncode"] == 0, report["stderr"]
    assert report["lost"] == [] and report["delivered"] == EVENTS_PER_VERSION
    # proxy indices count retries too: events 2 (503), 3 (drop), 4 (503, 503) and 5 (timeout) were
    # retried; the hang holds only the timed-out request, so its retry is not stalled behind it,
    # and faulty() waits until the abandoned request has landed as a replay-safe duplicate.
    assert sum(1 for n in report["attempts"].values() if n > 1) == 4
    assert 2 <= max(report["attempts"].values()) <= 7
    assert ledger.read(engine, "phase3-worker-ts-faults")["v1"] == EXPECTED["v1"]
    assert rows(engine, "phase3-worker-ts-faults") == EVENTS_PER_VERSION
    record("retry", {"faulted": proxy.faulted, "timed_out_index": 9, "events_retried": 4,
                     "delivered": report["delivered"], "duplicates_seen_by_client": report["duplicates"],
                     "rows": EVENTS_PER_VERSION})


def test_typescript_worker_reports_client_errors_without_retry(engine, origin, tmp_path):
    ts_json = check.export(workload, tmp_path / "ts.json", workflow="phase3-worker-ts-4xx")
    table = {3: 402, 5: 409, 7: 422}
    with faults.faulty(origin, lambda n: table.get(n, "pass")) as (proxy_origin, proxy):
        report = check.run_node(proxy_origin, server.token(), ts_json, "v1", retries=3)
    assert report["returncode"] == 1
    assert [entry["reason"] for entry in report["lost"]] == ["http 402", "http 409", "http 422"]
    assert all(report["attempts"][entry["id"]] == 1 for entry in report["lost"])
    assert report["delivered"] == EVENTS_PER_VERSION - 3
    assert rows(engine, "phase3-worker-ts-4xx") == EVENTS_PER_VERSION - 3
    record("client_errors", {"faulted": proxy.faulted, "lost": report["lost"], "retried": False})


def test_flagged_strip_types_path_reconciles(engine, origin, tmp_path):
    ts_json = check.export(workload, tmp_path / "ts.json", workflow="phase3-worker-ts-flag")
    report = check.run_node(origin, server.token(), ts_json, "v1", flags=("--experimental-strip-types",))
    assert report["returncode"] == 0, report["stderr"]
    assert ledger.read(engine, "phase3-worker-ts-flag")["v1"] == EXPECTED["v1"]
    record("floor", {"flag": "--experimental-strip-types", "node": NODE,
                     "actual_node_22_binary": "NOT_RUN: not available on this machine"})


def test_typescript_worker_is_standalone():
    source = (RECIPE / "worker.ts").read_text()
    imports = re.findall(r'from "([^"]+)"|import\("([^"]+)"\)', source)
    modules = {a or b for a, b in imports}
    assert modules and all(module.startswith("node:") for module in modules), modules
    assert not (RECIPE / "package.json").exists()
    record("standalone", {"imports": sorted(modules), "package_json": False})
