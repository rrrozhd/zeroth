"""C02: LangGraph/LangChain applications captured through the shared contract."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.langchain import ZerothCallbackHandler
from zeroth.instrumentation.openai import instrument_openai

HERE = Path(__file__).parent
RECIPES = HERE.parents[2] / "packaging" / "sdk" / "recipes"
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and pickling resolve the module by name
    spec.loader.exec_module(module)
    return module


check = load("c02_langgraph_check", RECIPES / "c02_langgraph" / "check.py")
app = check.app
c01_fixtures = load("c01_direct_fixtures", RECIPES / "c01_direct" / "fixtures.py")


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


def scenario(recorder, run_id="modes"):
    return recorder.run(workload.WORKFLOW, "v1", run_id).active()


def test_reference_workload_reconciles_through_the_graphs(engine, recorder):
    for version in workload.VERSIONS:
        check.reference(recorder, version, workload)
    assert recorder.lost == []
    assert set(recorder.client.statuses) == {"inserted"}
    assert ledger.read(engine, workload.WORKFLOW) == EXPECTED


def test_redelivering_captured_events_changes_nothing(engine, recorder):
    check.reference(recorder, "v1", workload)
    spy = recorder.client
    first = len(spy.events)
    for event in list(spy.events):
        recorder.deliver(event)
    assert set(spy.statuses[first:]) == {"duplicate"}
    assert ledger.read(engine, workload.WORKFLOW)["v1"] == EXPECTED["v1"]


def test_fanout_subgraph_interrupt_and_resume_charge_each_call_once(engine, recorder):
    from langgraph.types import Command

    fakes = check.models()
    planner, writer, quick = fakes[check.PLANNER], fakes[check.WRITER], fakes[check.QUICK]
    planner.expect(*(check.reply(check.PLANNER, [10 + i, 2, 0]) for i in range(3)))
    quick.expect(check.reply(check.QUICK, [20, 3, 0]))
    writer.expect(check.reply(check.WRITER, [30, 4, 0]))
    planner.expect(check.reply(check.PLANNER, [40, 5, 0]))
    graph = app.reviewed_pipeline(planner, writer, quick)
    config = {"callbacks": [ZerothCallbackHandler()], "configurable": {"thread_id": "t1"}}
    with scenario(recorder):
        paused = graph.invoke(app.initial("q"), config)
        assert "__interrupt__" in paused
        assert [r.step_id for r in charges(engine, "modes")] == ["plan", "branch", "branch#2", "merge"]
        finished = graph.invoke(Command(resume=True), config)
    assert finished["approved"] is True
    rows = charges(engine, "modes")
    assert [r.step_id for r in rows] == ["plan", "branch", "branch#2", "merge", "draft", "final"]
    assert {r.cost_measurement for r in rows} == {"estimated"}
    assert sorted(r.event_metadata["usage"]["input_tokens"] for r in rows) == [10, 11, 12, 20, 30, 40]
    assert len({r.execution_id for r in rows}) == 6
    assert all(fake.pending == 0 for fake in fakes.values())


def _chat_openai(replay, **kwargs):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model="gpt-4.1-mini", api_key="fixture", base_url="https://api.openai.com/v1",
        max_retries=0, http_client=replay.httpx.Client(transport=replay.transport()), **kwargs,
    )


def test_provider_wrapper_and_callback_do_not_double_count(engine, recorder):
    replay = c01_fixtures.openai_replay()
    handler_only = _chat_openai(replay)
    both = _chat_openai(replay)
    instrument_openai(both.root_client)
    for run_id, model in (("handler-only", handler_only), ("both", both)):
        replay.expect(c01_fixtures.Reply(usage=(100, 10, 20)), c01_fixtures.Reply(usage=(200, 20, 0)))
        graph = app.plan_then_answer(model, model)
        with recorder.run(workload.WORKFLOW, "v1", run_id).active():
            graph.invoke(app.initial("q"), {"callbacks": [ZerothCallbackHandler()]})
    only, combined = charges(engine, "handler-only"), charges(engine, "both")
    assert [(r.step_id, r.token_cost_usd) for r in only] == [(r.step_id, r.token_cost_usd) for r in combined]
    assert [r.event_metadata["usage"]["input_tokens"] for r in only] == [100, 200]
    assert [r.event_metadata["usage"]["cache_read_tokens"] for r in only] == [20, 0]
    assert all(r.event_metadata.get("framework") == "langchain" for r in only)
    assert all("framework" not in r.event_metadata for r in combined)
    assert all(r.event_metadata["provider_request_id"] == "req-fixture" for r in combined)
    assert len(replay.requests) == 4


def test_streaming_fallback_and_errors(engine, recorder):
    replay = c01_fixtures.openai_replay()
    llm = _chat_openai(replay, stream_usage=True)
    fakes = check.models()
    writer, reviewer = fakes[check.WRITER], fakes[check.REVIEWER]
    with scenario(recorder) as run:
        replay.expect(c01_fixtures.Reply(usage=(10, 2, 0), text="streamed"))
        with run.label("stream"):
            assert app.stream_text(llm.with_config(callbacks=[ZerothCallbackHandler()]), "q") == "streamed"
        writer.expect(RuntimeError("provider unavailable"))
        reviewer.expect(check.reply(check.REVIEWER, [50, 5, 0]))
        chain = app.with_fallback(writer, reviewer).with_config(callbacks=[ZerothCallbackHandler()])
        with run.label("fallback"):
            assert chain.invoke("q").content == "ok"
        writer.expect(RuntimeError("still down"))
        with run.label("failed"), pytest.raises(RuntimeError):
            writer.with_config(callbacks=[ZerothCallbackHandler()]).invoke("q")
    rows = {r.step_id: r for r in charges(engine, "modes")}
    assert rows["stream"].event_metadata["usage"] == {
        "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "reasoning_tokens": 0,
    }
    assert rows["fallback"].cost_measurement == "unmeasured"
    assert rows["fallback"].event_metadata["error"] == "RuntimeError"
    assert rows["fallback#2"].cost_measurement == "estimated"
    assert rows["fallback#2"].model_version == check.REVIEWER
    assert rows["failed"].cost_measurement == "unmeasured"
    assert len(rows) == 4


def test_existing_callbacks_and_spans_keep_working(engine, recorder):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    class Observer(BaseCallbackHandler):
        """A LangSmith-style tracer the application already had."""

        def __init__(self):
            self.events = []

        def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
            self.events.append("start")

        def on_llm_end(self, response, *, run_id, **kwargs):
            self.events.append("end")

        def on_chain_start(self, serialized, inputs, *, run_id, **kwargs):
            self.events.append("chain")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("customer-app")
    observer = Observer()
    fakes = check.models()
    planner, writer = fakes[check.PLANNER], fakes[check.WRITER]
    planner.expect(check.reply(check.PLANNER, [10, 2, 0]))
    writer.expect(check.reply(check.WRITER, [20, 3, 0]))
    with scenario(recorder), tracer.start_as_current_span("request"):
        app.plan_then_answer(planner, writer).invoke(
            app.initial("q"), {"callbacks": [observer, ZerothCallbackHandler()]}
        )
    assert observer.events.count("start") == 2 and observer.events.count("end") == 2
    assert "chain" in observer.events
    assert [span.name for span in exporter.get_finished_spans()] == ["request"]
    assert len(charges(engine, "modes")) == 2


@pytest.mark.parametrize("pin", ["floor", "current"])
def test_clean_install_reconciles_the_reference_workload(engine, origin, tmp_path, pin):
    recipe = RECIPES / "c02_langgraph"
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
    ).stdout.split()
    (HERE / "evidence").mkdir(exist_ok=True)
    (HERE / "evidence" / f"c02_langgraph-{pin}.json").write_text(json.dumps({
        "row": "C02", "pin": pin, "requirements": (recipe / f"requirements-{pin}.txt").read_text().split(),
        "installed": sorted(frozen), "report": report, "reconciled": "expected_ledger.json v1",
        "modes": "reference workload over fake chat models; interrupt/resume, double-wrap, streaming, "
                 "fallback and coexistence modes run at the harness environment versions",
    }, indent=2) + "\n")
