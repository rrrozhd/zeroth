"""C01: direct OpenAI/Anthropic SDK applications captured through the shared contract."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import ledger, server, workload
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.anthropic import instrument_anthropic
from zeroth.instrumentation.openai import instrument_openai

HERE = Path(__file__).parent
RECIPE = HERE.parents[2] / "packaging" / "sdk" / "recipes" / "c01_direct"
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]
sys.path.insert(0, str(RECIPE))
check = importlib.import_module("check")
fixtures = importlib.import_module("fixtures")
app = importlib.import_module("app")
Reply = fixtures.Reply


class Spy:
    """Keep every event and the server's status so the same events can be re-sent."""

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


def scenario(recorder, run_id="modes", version="v1"):
    return recorder.run(workload.WORKFLOW, version, run_id).active()


def test_reference_workload_reconciles_through_the_application(engine, recorder):
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


def test_streams_charge_at_completion_and_stay_unmeasured_when_aborted(engine, recorder):
    assistant, oai, ant = check.build()
    with scenario(recorder) as run:
        oai.expect(Reply(usage=(30, 4, 0)), Reply(usage=(31, 4, 0)), Reply(usage=(32, 4, 0)))
        ant.expect(Reply(usage=(40, 5, 0)), Reply(usage=(41, 5, 0)), Reply(usage=(42, 5, 0)))
        with run.label("chat-complete"):
            assert assistant.stream_plan("q") == "ok"
        with run.label("responses-complete"):
            assert assistant.stream_review("q") == "ok"
        with run.label("chat-aborted"):
            stream = assistant.openai.chat.completions.create(
                model=app.PLANNER, messages=[{"role": "user", "content": "q"}], stream=True,
                stream_options={"include_usage": True},
            )
            next(iter(stream))
            stream.close()
        with run.label("messages-complete"):
            assert assistant.stream_answer("q") == "ok"
        with run.label("events-complete"):
            events = assistant.anthropic.messages.create(
                model=app.WRITER, max_tokens=8, messages=[{"role": "user", "content": "q"}],
                stream=True,
            )
            assert [e.type for e in events][-1] == "message_stop"
        with run.label("messages-aborted"):
            with assistant.anthropic.messages.stream(
                model=app.WRITER, max_tokens=8, messages=[{"role": "user", "content": "q"}]
            ) as stream:
                next(iter(stream.text_stream))
    rows = {row.step_id: row for row in charges(engine, "modes")}
    assert rows["chat-complete"].event_metadata["usage"]["input_tokens"] == 30
    assert rows["responses-complete"].event_metadata["usage"]["input_tokens"] == 31
    assert rows["messages-complete"].event_metadata["usage"]["input_tokens"] == 40
    assert rows["events-complete"].event_metadata["usage"]["output_tokens"] == 5
    for step in ("chat-complete", "responses-complete", "messages-complete", "events-complete"):
        assert rows[step].cost_measurement == "estimated"
        assert rows[step].event_metadata["stream"] == "complete"
    for step in ("chat-aborted", "messages-aborted"):
        assert rows[step].cost_measurement == "unmeasured" and rows[step].token_cost_usd is None
        assert rows[step].event_metadata["error"] == "aborted"
        assert rows[step].event_metadata["stream"] == "aborted"
    assert not oai.queue and not ant.queue


def test_sdk_retries_and_provider_failures_are_charged_attempts(engine, recorder):
    import openai

    assistant, oai, ant = check.build(max_retries=2)
    with scenario(recorder) as run:
        oai.expect(Reply(status=429), Reply(usage=(50, 6, 0)))
        with run.label("retried"):
            assert assistant.plan("q") == "ok"
        oai.expect(Reply(status=500), Reply(status=500), Reply(status=500))
        with run.label("failed"), pytest.raises(openai.InternalServerError):
            assistant.plan("q")
    rows = charges(engine, "modes")
    retried = [r for r in rows if r.step_id == "retried"]
    assert [(r.attempt, r.cost_measurement, r.event_metadata["error"]) for r in retried] == [
        (1, "unmeasured", "retried"), (2, "estimated", None),
    ]
    assert retried[1].event_metadata["provider_request_id"] == "req-fixture"
    failed = [r for r in rows if r.step_id == "failed"]
    assert len(failed) == 1 and failed[0].cost_measurement == "unmeasured"
    assert failed[0].event_metadata["error"] == "InternalServerError"
    assert failed[0].event_metadata["retries_unobserved"] is True
    assert len(oai.requests) == 5


def test_cached_tokens_tools_and_cache_writes_are_priced_from_the_split(engine, recorder):
    assistant, oai, ant = check.build()
    with scenario(recorder) as run:
        oai.expect(Reply(usage=(1000, 100, 200)))
        with run.label("cached"):
            assistant.plan("q")
        ant.expect(Reply(usage=(300, 50, 100, 400)))
        with run.label("cache-write"):
            assistant.answer("q")
        oai.expect(
            Reply(usage=(20, 5, 0), tool_call={"name": "web_search", "arguments": {"query": "q"}}),
            Reply(usage=(60, 9, 0)),
        )
        run.tool_charge("web_search", tool="web_search", cost_usd=Decimal("0.005"))
        with run.label("tool-turn"):
            assert assistant.tool_cycle("q") == "ok"
    rows = {row.step_id: row for row in charges(engine, "modes")}
    assert rows["cached"].event_metadata["usage"] == {
        "input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 200,
        "cache_write_tokens": 0, "reasoning_tokens": 0,
    }
    assert rows["cached"].token_cost_usd == Decimal("0.00058000")  # 1000*0.4 + 100*1.6 + 200*0.1
    assert rows["cache-write"].token_cost_usd == Decimal("0.00318000")  # 300*3+50*15+100*.3+400*3.75
    assert rows["web_search"].event_metadata["charge_kind"] == "tool"
    assert rows["tool-turn"].attempt == 1 and rows["tool-turn#2"].attempt == 1
    assert len([r for r in rows.values() if r.event_metadata["charge_kind"] == "model"]) == 4


def test_async_clients_including_cancellation(engine, recorder):
    oai, ant = fixtures.openai_replay(), fixtures.anthropic_replay()
    assistant = app.AsyncAssistant(
        instrument_openai(fixtures.openai_client(oai, asynchronous=True)),
        instrument_anthropic(fixtures.anthropic_client(ant, asynchronous=True)),
    )

    async def slow(request):
        await asyncio.sleep(0.5)
        return oai.handler(request)

    async def main():
        with scenario(recorder) as run:
            oai.expect(Reply(usage=(11, 2, 0)), Reply(usage=(12, 2, 0)))
            ant.expect(Reply(usage=(13, 2, 0)), Reply(usage=(14, 2, 0)))
            with run.label("plan"):
                assert await assistant.plan("q") == "ok"
            with run.label("stream-plan"):
                assert await assistant.stream_plan("q") == "ok"
            with run.label("answer"):
                assert await assistant.answer("q") == "ok"
            with run.label("stream-answer"):
                assert await assistant.stream_answer("q") == "ok"
            oai.expect(Reply(usage=(15, 2, 0)))
            assistant.openai._client._transport = oai.httpx.MockTransport(slow)
            with run.label("cancelled"), pytest.raises(TimeoutError):
                await asyncio.wait_for(assistant.plan("q"), timeout=0.05)

    asyncio.run(main())
    rows = {row.step_id: row for row in charges(engine, "modes")}
    assert {s: rows[s].event_metadata["usage"]["input_tokens"]
            for s in ("plan", "stream-plan", "answer", "stream-answer")} == {
        "plan": 11, "stream-plan": 12, "answer": 13, "stream-answer": 14}
    assert rows["cancelled"].cost_measurement == "unmeasured"
    assert rows["cancelled"].event_metadata["error"] == "CancelledError"


def test_application_behaviour_is_identical_with_and_without_instrumentation(recorder):
    import openai

    def observe(instrument: bool):
        assistant, oai, ant = check.build(instrument=instrument)
        results = []
        with scenario(recorder, run_id=f"parity-{instrument}"):
            oai.expect(Reply(text="plan"), Reply(text="review"), Reply(text="streamed"),
                       Reply(text="not json"), Reply(text='{"ok": 1}'),
                       Reply(tool_call={"name": "web_search", "arguments": {"query": "q"}}),
                       Reply(text="tooled"), Reply(status=500), Reply(text="fallback"))
            ant.expect(Reply(text="answer"), Reply(text="stream-answer"), Reply(status=503))
            for op in (
                lambda: assistant.plan("q"), lambda: assistant.review("q"),
                lambda: assistant.stream_plan("q"), lambda: assistant.robust_extract("q"),
                lambda: assistant.tool_cycle("q"), lambda: assistant.answer("q"),
                lambda: assistant.stream_answer("q"),
            ):
                results.append(op())
            with pytest.raises(openai.InternalServerError) as failed:
                assistant.plan("q")
            results.append(type(failed.value).__name__)
            results.append(assistant.answer_with_fallback("q"))
        return results

    assert observe(False) == observe(True)


def test_openai_compatible_base_url_is_not_openai_unless_declared(engine, recorder):
    oai = fixtures.openai_replay()
    proxied = instrument_openai(fixtures.openai_client(oai, base_url="http://localhost:4000/v1"))
    declared = instrument_openai(
        fixtures.openai_client(oai, base_url="http://localhost:4000/v1"), declared_provider="openai"
    )
    with scenario(recorder) as run:
        oai.expect(Reply(usage=(10, 5, 0)), Reply(usage=(10, 5, 0)))
        with run.label("proxied"):
            proxied.chat.completions.create(model=app.PLANNER, messages=[])
        with run.label("declared"):
            declared.chat.completions.create(model=app.PLANNER, messages=[])
    rows = {row.step_id: row for row in charges(engine, "modes")}
    assert rows["proxied"].cost_measurement == "unmeasured"
    assert rows["proxied"].event_metadata["pricing"] == "unknown_provider"
    assert rows["proxied"].event_metadata["provider"] == "unknown"
    assert rows["declared"].cost_measurement == "estimated"
    assert rows["declared"].event_metadata["provider"] == "openai"


def test_existing_http_hooks_and_spans_keep_working(engine, recorder):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    seen = []
    oai = fixtures.openai_replay()
    client = instrument_openai(fixtures.openai_client(
        oai, event_hooks={"response": [lambda response: seen.append(response.status_code)]}
    ))
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("customer-app")
    with scenario(recorder) as run:
        oai.expect(Reply(usage=(10, 5, 0)), Reply(usage=(10, 5, 0)))
        for name in ("first", "second"):
            with tracer.start_as_current_span(name), run.label(name):
                client.chat.completions.create(model=app.PLANNER, messages=[])
    assert seen == [200, 200]
    assert [span.name for span in exporter.get_finished_spans()] == ["first", "second"]
    assert len(charges(engine, "modes")) == 2


@pytest.mark.parametrize("pin", ["floor", "current"])
def test_clean_install_reconciles_the_reference_workload(engine, origin, tmp_path, pin):
    """A fresh environment installs the recipe's pins plus the SDK and reproduces v1."""
    import subprocess

    venv = tmp_path / pin
    python = venv / "bin" / "python"
    subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(python), str(HERE.parents[2] / "packaging" / "sdk"),
         "-r", str(RECIPE / f"requirements-{pin}.txt")],
        check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        [str(python), str(RECIPE / "check.py"), "--zeroth-url", origin, "--zeroth-key", server.token(),
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
    evidence = HERE / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / f"c01_direct-{pin}.json").write_text(json.dumps({
        "row": "C01", "pin": pin, "requirements": (RECIPE / f"requirements-{pin}.txt").read_text().split(),
        "installed": sorted(frozen), "report": report, "reconciled": "expected_ledger.json v1",
        "modes": "reference workload only; stream/retry/async/proxy/coexistence modes run at the "
                 "harness environment versions",
    }, indent=2) + "\n")
