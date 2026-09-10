"""Run the frozen Phase 3 reference workload through the C01 application.

This is the instrumentation the recipe adds around ``app.Assistant``: instrument
the two clients once, open a run per request, label the steps you care about,
charge tools you price yourself, and record the outcome you own. Everything the
application does stays the same. ``main`` reproduces one workload version against
a Zeroth deployment so a clean environment can be checked without this repository's
test suite (the workload module itself is loaded from ``--workload``).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from zeroth.instrumentation import Recorder
from zeroth.instrumentation.anthropic import instrument_anthropic
from zeroth.instrumentation.openai import instrument_openai

sys.path.insert(0, str(Path(__file__).parent))
from app import PLANNER, QUICK, REVIEWER, WRITER, Assistant  # noqa: E402
from fixtures import (  # noqa: E402
    Reply,
    anthropic_client,
    anthropic_replay,
    openai_client,
    openai_replay,
)


def build(*, instrument: bool = True, **client_kwargs):
    """The application with replayed providers; instrumented unless asked not to."""
    oai, ant = openai_replay(), anthropic_replay()
    assistant = Assistant(
        openai_client(oai, **client_kwargs),
        anthropic_client(ant, **client_kwargs),
        search=lambda query: f"results for {query}",
    )
    if instrument:
        instrument_openai(assistant.openai)
        instrument_anthropic(assistant.anthropic)
    return assistant, oai, ant


def _reply(call: dict, version: str, **overrides) -> Reply:
    usage = call["usage"][version]
    return Reply(usage=None if usage is None else tuple(usage), model=call["model"], **overrides)


def drive(assistant: Assistant, run, spec: dict, version: str, oai, ant) -> None:
    """Map one reference family onto application operations."""
    calls, question = spec["calls"], f"question for {spec['id']}"
    family = spec["id"]
    if family == "success":
        oai.expect(_reply(calls[0], version))
        ant.expect(_reply(calls[1], version))
        with run.label("plan"):
            assistant.plan(question, model=PLANNER)
        with run.label("answer"):
            assistant.answer(question, model=WRITER)
    elif family in ("rejection",):
        oai.expect(_reply(calls[0], version))
        with run.label("answer"):
            assistant.review(question, model=REVIEWER)
    elif family in ("delayed_outcome", "duplicate_delivery"):
        ant.expect(_reply(calls[0], version))
        with run.label("answer"):
            assistant.answer(question, model=QUICK)
    elif family == "retry":
        oai.expect(
            _reply(calls[0], version, text="not json"),
            _reply(calls[1], version, text='{"ok": true}'),
        )
        with run.label("answer"):
            assistant.robust_extract(question)
    elif family == "parallel_calls":
        oai.expect(_reply(calls[0], version), _reply(calls[1], version))
        ant.expect(_reply(calls[2], version))
        with run.label("fanout"):
            assistant.fanout([question + " a", question + " b"])
        with run.label("merge"):
            assistant.answer(question, model=QUICK)
    elif family == "tool_cost":
        tool = calls[1]
        run.tool_charge(
            tool["step"], tool=tool["tool"], cost_usd=Decimal(tool["cost_usd"][version])
        )
        oai.expect(_reply(calls[0], version))
        with run.label("answer"):
            assistant.search_then_review(question)
    elif family == "fallback":
        ant.expect(Reply(status=503, model=calls[0]["model"]))
        oai.expect(_reply(calls[1], version))
        with run.label("answer"):
            assistant.answer_with_fallback(question)
    elif family == "missing_usage":
        oai.expect(_reply(calls[0], version))
        with run.label("answer"):
            assistant.plan(question, model=PLANNER)
    else:
        raise ValueError(f"unmapped family {family}")


def reference(recorder: Recorder, version: str, workload, *, instrument: bool = True) -> Assistant:
    """Replay every workload run through the application; return the assistant used."""
    assistant, oai, ant = build(instrument=instrument)
    for spec in workload.RUNS:
        at = workload.run_time(spec["id"])
        with recorder.run(workload.WORKFLOW, version, spec["id"]).active() as run:
            drive(assistant, run, spec, version, oai, ant)
            run.summary(spec["terminal"], recorded_at=at)
            if spec["outcome"]["delayed"]:
                run.outcome(None, maturity="provisional", occurred_at=at)
                at = at + timedelta(hours=1)
            run.outcome(spec["outcome"]["accepted"], occurred_at=at)
    assert not oai.queue and not ant.queue, "every replayed provider response was consumed"
    return assistant


def load_workload(path: str):
    spec = importlib.util.spec_from_file_location("reference_workload", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroth-url", required=True)
    parser.add_argument("--zeroth-key", required=True)
    parser.add_argument("--workload", required=True, help="path to the frozen workload.py")
    parser.add_argument("--version", default="v1")
    args = parser.parse_args(argv)
    import anthropic
    import openai
    from zeroth.sdk import ZerothClient

    workload = load_workload(args.workload)
    recorder = Recorder(ZerothClient(api_key=args.zeroth_key, base_url=args.zeroth_url))
    reference(recorder, args.version, workload)
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "openai": openai.__version__,
                "anthropic": anthropic.__version__,
                "lost": len(recorder.lost),
                "version": args.version,
            }
        )
    )
    return 0 if not recorder.lost else 1


if __name__ == "__main__":
    raise SystemExit(main())
