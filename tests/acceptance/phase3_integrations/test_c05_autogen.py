"""C05: AutoGen AgentChat applications captured through the shared contract.

autogen-core pins protobuf 5.29 while this environment's gateway-conformance group
needs protobuf 6.32, so the recipe runs in clean pinned venvs against the loopback
server and the rows the economic plane stored are asserted here.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution

HERE = Path(__file__).parent
RECIPE = HERE.parents[2] / "packaging" / "sdk" / "recipes" / "c05_autogen"
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
        [
            "uv",
            "pip",
            "install",
            "-q",
            "--python",
            str(python),
            str(HERE.parents[2] / "packaging" / "sdk"),
            "-r",
            str(RECIPE / f"requirements-{pin}.txt"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return python


def run_check(python: Path, origin: str, scenario: str, version: str = "v1") -> dict:
    result = subprocess.run(
        [
            str(python),
            str(RECIPE / "check.py"),
            "--zeroth-url",
            origin,
            "--zeroth-key",
            server.token(),
            "--workload",
            str(HERE / "workload.py"),
            "--version",
            version,
            "--scenario",
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def cache_blind(version: str) -> dict:
    """The frozen ledger as AgentChat can reproduce it: cached prompt tokens priced as input.

    RequestUsage carries no cache split, so an OpenAI-style client reports prompt tokens
    inclusive of cached ones and the adapter prices them at the input rate. Every other
    run is exact; the cached run overstates by exactly cached x (input - cache_read).
    """
    import copy
    from decimal import Decimal

    from zeroth.instrumentation.rate_card import rates

    expected = copy.deepcopy(EXPECTED[version])
    for spec in workload.RUNS:
        extra = 0
        for call in spec["calls"]:
            usage = call.get("usage", {}).get(version)
            if call["kind"] != "model" or not usage or not usage[2]:
                continue
            card = rates(call["model"])
            per_token = (card.input_per_mtok - card.cache_read_per_mtok) * 100  # 1e-8 USD units
            extra += int(Decimal(usage[2]) * per_token)
        if extra:
            run = expected["runs"][spec["id"]]
            run["measured_cost_units"] += extra
            run["model_cost_units"] += extra
            expected["totals"]["measured_cost_units"] += extra
    return expected


def test_ag2_is_reported_as_a_separate_unsupported_row(monkeypatch):
    """With AutoGen 0.2 / AG2 present and autogen_core absent, the import explains itself."""
    monkeypatch.setitem(sys.modules, "autogen", types.ModuleType("autogen"))
    monkeypatch.setitem(sys.modules, "autogen_core", None)
    monkeypatch.setitem(sys.modules, "autogen_core.models", None)
    sys.modules.pop("zeroth.instrumentation.autogen", None)
    with pytest.raises(ImportError) as diagnostic:
        importlib.import_module("zeroth.instrumentation.autogen")
    assert "AutoGen 0.2 / AG2" in str(diagnostic.value)
    assert "unsupported" in str(diagnostic.value) and "autogen-agentchat" in str(diagnostic.value)


@pytest.mark.parametrize("pin", ["floor", "current"])
def test_clean_install_reconciles_reference_and_modes(engine, origin, tmp_path, pin):
    python = install(tmp_path, pin)
    versions = ["v1", "v2"] if pin == "current" else ["v1"]
    reports = [run_check(python, origin, "reference", version) for version in versions]
    assert all(r["lost"] == 0 for r in reports)
    observed = ledger.read(engine, workload.WORKFLOW)
    for version in versions:
        assert observed[version] == cache_blind(version), version

    modes = run_check(python, origin, "modes")
    assert modes["lost"] == 0
    team = charges(engine, "modes-team")
    assert [(r.step_id, r.event_metadata["usage"]["input_tokens"]) for r in team] == [
        ("planner", 100),
        ("writer", 200),
    ]
    assert {r.event_metadata["cache_split"] for r in team} == {"unavailable"}
    assert modes["report"]["team"] == "TERMINATE done"
    stream = charges(engine, "modes-stream")
    assert [
        (r.event_metadata["stream"], r.event_metadata["usage"]["input_tokens"]) for r in stream
    ] == [("complete", 30)]
    assert modes["report"]["stream"] == {"text": "streamed reply", "chunks": 2}
    missing = charges(engine, "modes-missing")
    assert [(r.cost_measurement, r.event_metadata["pricing"]) for r in missing] == [
        ("unmeasured", "missing_usage")
    ]
    assert charges(engine, "modes-cached") == []
    cancelled = charges(engine, "modes-cancelled")
    assert [(r.cost_measurement, r.event_metadata["error"]) for r in cancelled] == [
        ("unmeasured", "RuntimeError")
    ]
    assert modes["report"]["cancelled"] == "cancelled before the call"

    frozen = subprocess.run(
        ["uv", "pip", "freeze", "--python", str(python)], check=True, capture_output=True, text=True
    ).stdout.split()
    (HERE / "evidence").mkdir(exist_ok=True)
    (HERE / "evidence" / f"c05_autogen-{pin}.json").write_text(
        json.dumps(
            {
                "row": "C05",
                "pin": pin,
                "requirements": (RECIPE / f"requirements-{pin}.txt").read_text().split(),
                "installed": sorted(
                    p
                    for p in frozen
                    if p.split("==")[0]
                    in (
                        "autogen-agentchat",
                        "autogen-core",
                        "autogen-ext",
                        "openai",
                        "pydantic",
                        "zeroth-sdk",
                        "protobuf",
                    )
                ),
                "reports": reports + [modes],
                "reconciled": f"expected_ledger.json {', '.join(versions)}",
                "modes": "two-agent team with TERMINATE stop and outcome, streamed run, missing usage, cache hit, "
                "cancellation token; AG2 diagnostic checked in the harness environment",
            },
            indent=2,
        )
        + "\n"
    )
