"""C04: CrewAI crews and flows captured through the shared contract.

CrewAI's dependency set conflicts with this environment's pins, so every check runs
the recipe in a clean pinned venv against the loopback server and asserts the rows
the economic plane stored.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution

HERE = Path(__file__).parent
RECIPE = HERE.parents[2] / "packaging" / "sdk" / "recipes" / "c04_crewai"
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]


def charges(engine, run_id: str, version: str = "v1") -> list[StoredExecution]:
    with Session(engine) as db:
        rows = db.scalars(
            select(StoredExecution)
            .where(StoredExecution.run_id == run_id, StoredExecution.workflow_version == version)
            .where(StoredExecution.cost_role == "charge")
            .order_by(StoredExecution.id)
        ).all()
        for row in rows:
            db.expunge(row)
        return rows


def install(tmp_path: Path, pin: str) -> Path:
    venv = tmp_path / pin
    python = venv / "bin" / "python"
    subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(python), str(HERE.parents[2] / "packaging" / "sdk"),
         "-r", str(RECIPE / f"requirements-{pin}.txt")],
        check=True, capture_output=True, text=True,
    )
    return python


def run_check(python: Path, origin: str, scenario: str, version: str = "v1") -> dict:
    result = subprocess.run(
        [str(python), str(RECIPE / "check.py"), "--zeroth-url", origin, "--zeroth-key", server.token(),
         "--workload", str(HERE / "workload.py"), "--version", version, "--scenario", scenario],
        capture_output=True, text=True, timeout=600,
        env={"PATH": "/usr/bin:/bin", "HOME": str(python.parents[1]), "CREWAI_TRACING_ENABLED": "false",
             "CREWAI_DISABLE_TELEMETRY": "true", "OTEL_SDK_DISABLED": "true"},
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("pin", ["floor", "current"])
def test_clean_install_reconciles_reference_and_modes(engine, origin, tmp_path, pin):
    python = install(tmp_path, pin)
    versions = ["v1", "v2"] if pin == "current" else ["v1"]
    reports = [run_check(python, origin, "reference", version) for version in versions]
    assert all(r["lost"] == 0 for r in reports)
    observed = ledger.read(engine, workload.WORKFLOW)
    for version in versions:
        assert observed[version] == EXPECTED[version], version

    modes = run_check(python, origin, "modes")
    assert modes["lost"] == 0
    for label in "AB":
        rows = charges(engine, f"modes-concurrent-{label}")
        assert [r.event_metadata["usage"]["input_tokens"] for r in rows] == [100], label
        assert rows[0].event_metadata["agent"] == "planner"
    retry = charges(engine, "modes-retry")
    assert [(r.cost_measurement, r.event_metadata["error"]) for r in retry] == [
        ("unmeasured", "transient"), ("estimated", None)]
    flow = charges(engine, "modes-flow")  # events are dispatched on handler threads: order-free
    assert sorted(r.step_id for r in flow) == ["llm", "llm#2"]
    assert sorted(r.event_metadata["usage"]["input_tokens"] for r in flow) == [30, 40]
    assert {r.event_metadata["provider"] for r in flow} == {"anthropic"}
    parallel = charges(engine, "modes-parallel")
    assert sorted(r.event_metadata["usage"]["input_tokens"] for r in parallel) == [60, 61, 70]
    assert {r.event_metadata["agent"] for r in parallel} == {"planner", "quick"}
    bare = charges(engine, "modes-bare")
    assert [(r.cost_measurement, r.event_metadata["pricing"], r.event_metadata["provider"]) for r in bare] == [
        ("unmeasured", "unknown_provider", "unknown")]
    with Session(engine) as db:
        total = db.scalars(select(StoredExecution).where(
            StoredExecution.run_id.like("modes-%"), StoredExecution.cost_role == "charge")).all()
    assert len(total) == 10  # a second instrument_crewai() call added nothing

    frozen = subprocess.run(
        ["uv", "pip", "freeze", "--python", str(python)], check=True, capture_output=True, text=True
    ).stdout.split()
    (HERE / "evidence").mkdir(exist_ok=True)
    (HERE / "evidence" / f"c04_crewai-{pin}.json").write_text(json.dumps({
        "row": "C04", "pin": pin, "requirements": (RECIPE / f"requirements-{pin}.txt").read_text().split(),
        "installed": sorted(p for p in frozen if p.split("==")[0] in (
            "crewai", "litellm", "openai", "pydantic", "zeroth-sdk", "httpx")),
        "reports": reports + [modes], "reconciled": f"expected_ledger.json {', '.join(versions)}",
        "modes": "concurrent crews on two runs, agent retry, flow, async tasks, bare model name, "
                 "idempotent registration",
    }, indent=2) + "\n")
