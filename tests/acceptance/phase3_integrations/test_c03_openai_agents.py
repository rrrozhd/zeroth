"""C03: OpenAI Agents SDK applications captured through the shared contract."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("agents", reason="C03 needs the optional openai-agents dependency group")

from agents import RunConfig  # noqa: E402
from agents.tracing import TracingProcessor, add_trace_processor  # noqa: E402
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.openai import instrument_openai
from zeroth.instrumentation.openai_agents import ZerothRunHooks

HERE = Path(__file__).parent
RECIPES = HERE.parents[2] / "packaging" / "sdk" / "recipes"
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = load("c03_openai_agents_check", RECIPES / "c03_openai_agents" / "check.py")
app, fakes = check.app, check.fakes
c01_fixtures = load("c01_direct_fixtures_for_c03", RECIPES / "c01_direct" / "fixtures.py")


class Spy:
    def __init__(self, inner):
        self.inner, self.events, self.statuses = inner, [], []

    def record_execution(self, event):
        self.events.append(event)
        response = self.inner.record_execution(event)
        self.statuses.append(response["status"])
        return response

    def record_outcome(self, event):
        self.events.append(event)
        response = self.inner.record_outcome(event)
        self.statuses.append(response["status"])
        return response


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


@pytest.fixture
def recorder(origin):
    return Recorder(Spy(server.sdk(origin)))


def test_reference_workload_reconciles_through_the_agents(engine, recorder):
    for version in workload.VERSIONS:
        asyncio.run(check.reference(recorder, version, workload))
    assert recorder.lost == []
    assert set(recorder.client.statuses) == {"inserted"}
    assert ledger.read(engine, workload.WORKFLOW) == EXPECTED


def test_redelivering_captured_events_changes_nothing(engine, recorder):
    asyncio.run(check.reference(recorder, "v1", workload))
    spy = recorder.client
    first = len(spy.events)
    for event in list(spy.events):
        recorder.deliver(event)
    assert set(spy.statuses[first:]) == {"duplicate"}
    assert ledger.read(engine, workload.WORKFLOW)["v1"] == EXPECTED["v1"]


def test_handoff_tool_and_failure_charge_each_model_call_once(engine, recorder):
    provider = fakes.FakeProvider()
    config = RunConfig(model_provider=provider, tracing_disabled=True)
    planner = app.planner(handoff_to=app.writer(), tools=[app.web_search])
    provider.get_model(app.PLANNER).expect(
        fakes.reply([fakes.tool_call("web_search", {"query": "q"})], [20, 5]),
        fakes.reply([fakes.tool_call("transfer_to_writer", {})], [1200, 300]),
    )
    provider.get_model(app.WRITER).expect(fakes.reply(None, [2000, 500, 800], "done"))

    async def scenario():
        with recorder.run(workload.WORKFLOW, "v1", "modes").active() as run:
            with ZerothRunHooks(model_provider=provider) as hooks:
                assert await app.run(planner, "q", hooks=hooks, config=config) == "done"
            aggregates = hooks.aggregates()
            provider.get_model(app.WRITER).expect(RuntimeError("provider unavailable"))
            with pytest.raises(RuntimeError), ZerothRunHooks(model_provider=provider) as failing:
                await app.run(app.writer(), "q", hooks=failing, config=config)
            run.summary("completed", metadata=aggregates)
        return aggregates

    aggregates = asyncio.run(scenario())
    rows = charges(engine, "modes")
    assert [r.step_id for r in rows] == ["planner", "planner#2", "writer", "writer#2"]
    assert [r.cost_measurement for r in rows] == ["estimated"] * 3 + ["unmeasured"]
    assert rows[2].event_metadata["usage"] == {
        "input_tokens": 2000, "output_tokens": 500, "cache_read_tokens": 800,
        "cache_write_tokens": 0, "reasoning_tokens": 0,
    }
    assert rows[2].model_version == app.WRITER and rows[2].event_metadata["provider"] == "anthropic"
    assert rows[3].event_metadata["error"] == "RuntimeError"
    assert aggregates["handoffs"] == [["planner", "writer"]]
    assert aggregates["agents"] == {"planner": {"model_calls": 2, "turns": 2},
                                    "writer": {"model_calls": 1, "turns": 1}}
    assert provider.pending == 0


def test_provider_wrapper_and_hooks_do_not_double_count(engine, recorder):
    from agents.models.openai_provider import OpenAIProvider

    replay = c01_fixtures.openai_replay()
    plain = c01_fixtures.openai_client(replay, asynchronous=True)
    wrapped = instrument_openai(c01_fixtures.openai_client(replay, asynchronous=True))

    async def scenario():
        for run_id, client in (("hooks-only", plain), ("both", wrapped)):
            config = RunConfig(
                model_provider=OpenAIProvider(openai_client=client, use_responses=True),
                tracing_disabled=True,
            )
            replay.expect(c01_fixtures.Reply(usage=(100, 10, 20), text="answer"))
            with recorder.run(workload.WORKFLOW, "v1", run_id).active(), ZerothRunHooks() as hooks:
                assert await app.run(app.planner(), "q", hooks=hooks, config=config) == "answer"

    asyncio.run(scenario())
    only, combined = charges(engine, "hooks-only"), charges(engine, "both")
    assert [(r.step_id, r.token_cost_usd) for r in only] == [(r.step_id, r.token_cost_usd) for r in combined]
    assert only[0].event_metadata["usage"]["cache_read_tokens"] == 20
    assert only[0].event_metadata.get("framework") == "openai-agents"
    assert "framework" not in combined[0].event_metadata
    assert combined[0].event_metadata["provider_request_id"] == "req-fixture"
    assert len(replay.requests) == 2


def test_existing_trace_processors_keep_working(engine, recorder):
    class Tracer(TracingProcessor):
        def __init__(self):
            self.spans = []

        def on_trace_start(self, trace):
            pass

        def on_trace_end(self, trace):
            pass

        def on_span_start(self, span):
            self.spans.append(type(span.span_data).__name__)

        def on_span_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self):
            pass

    tracer = Tracer()
    add_trace_processor(tracer)
    provider = fakes.FakeProvider()
    provider.get_model(app.QUICK).expect(fakes.reply(None, [10, 2]))
    config = RunConfig(model_provider=provider)

    async def scenario():
        with recorder.run(workload.WORKFLOW, "v1", "modes").active(), ZerothRunHooks(
            model_provider=provider
        ) as hooks:
            await app.run(app.quick(), "q", hooks=hooks, config=config)

    asyncio.run(scenario())
    assert "AgentSpanData" in tracer.spans
    assert len(charges(engine, "modes")) == 1


@pytest.mark.parametrize("pin", ["floor", "current"])
def test_clean_install_reconciles_the_reference_workload(engine, origin, tmp_path, pin):
    recipe = RECIPES / "c03_openai_agents"
    venv = tmp_path / pin
    python = venv / "bin" / "python"
    subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(python), str(HERE.parents[2] / "packaging" / "sdk"),
         "-r", str(recipe / f"requirements-{pin}.txt")],
        check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        [str(python), str(recipe / "check.py"), "--zeroth-url", origin, "--zeroth-key", server.token(),
         "--workload", str(HERE / "workload.py"), "--version", "v1"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["lost"] == 0
    assert ledger.read(engine, workload.WORKFLOW)["v1"] == EXPECTED["v1"]
    frozen = subprocess.run(
        ["uv", "pip", "freeze", "--python", str(python)], check=True, capture_output=True, text=True
    ).stdout.replace(HERE.parents[2].resolve().as_uri(), "file:${REPO_ROOT}").splitlines()
    (HERE / "evidence").mkdir(exist_ok=True)
    (HERE / "evidence" / f"c03_openai_agents-{pin}.json").write_text(json.dumps({
        "row": "C03", "pin": pin, "requirements": (recipe / f"requirements-{pin}.txt").read_text().split(),
        "installed": sorted(frozen), "report": report, "reconciled": "expected_ledger.json v1",
        "modes": "reference workload over fake models; handoff/tool/failure, double-path and tracing "
                 "coexistence modes run at the harness environment versions",
    }, indent=2) + "\n")
