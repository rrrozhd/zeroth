"""Run the frozen Phase 3 reference workload and the C04 mode scenarios through CrewAI.

The instrumentation the recipe adds: ``instrument_crewai()`` once at startup, a run
per request, tool charges the application prices itself, and the outcome the
application owns. Fake LLMs emit CrewAI's own events with queued usage, so a
clean environment reproduces everything without keys or network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
from contextvars import copy_context
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from zeroth.instrumentation import Recorder
from zeroth.instrumentation.crewai import instrument_crewai


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app = _load("c04_crewai_app", Path(__file__).with_name("app.py"))
fakes = _load("c04_crewai_fakes", Path(__file__).with_name("fakes.py"))


def drive(replies: fakes.Replies, run, spec: dict, version: str) -> None:
    calls, question = spec["calls"], f"question for {spec['id']}"

    def queued(index: int, text: str = "done"):
        call = calls[index]
        tokens = call["usage"][version]
        replies.expect(
            call["model"] if "/" in call["model"] else _prefixed(call["model"]),
            (fakes.final(text), None if tokens is None else fakes.usage(*tokens)),
        )

    family = spec["id"]
    if family == "success":
        queued(0), queued(1)
        app.plan_then_write(replies.llm(app.PLANNER), replies.llm(app.WRITER), question)
    elif family == "rejection":
        queued(0)
        app.single("reviewer", replies.llm(app.REVIEWER), question)
    elif family in ("delayed_outcome", "duplicate_delivery"):
        queued(0)
        app.single("quick", replies.llm(app.QUICK), question)
    elif family == "retry":
        queued(0, text="not json"), queued(1, text='{"ok": true}')
        app.robust_extract(replies.llm(app.PLANNER), question)
    elif family == "parallel_calls":
        queued(0), queued(1), queued(2)
        app.parallel_then_merge(replies.llm(app.PLANNER), replies.llm(app.QUICK), question)
    elif family == "tool_cost":
        tool = calls[1]
        run.tool_charge(
            tool["step"], tool=tool["tool"], cost_usd=Decimal(tool["cost_usd"][version])
        )
        queued(0)
        app.single("reviewer", replies.llm(app.REVIEWER), question)
    elif family == "fallback":
        replies.expect(_prefixed(calls[0]["model"]), RuntimeError("provider unavailable"))
        queued(1)
        app.answer_with_fallback(replies.llm(app.WRITER), replies.llm(app.REVIEWER), question)
    elif family == "missing_usage":
        queued(0)
        app.single("planner", replies.llm(app.PLANNER), question)
    else:
        raise ValueError(f"unmapped family {family}")


def _prefixed(model: str) -> str:
    return f"{'anthropic' if model.startswith('claude') else 'openai'}/{model}"


def reference(recorder: Recorder, version: str, workload) -> fakes.Replies:
    listener = instrument_crewai()
    replies = fakes.Replies()
    for spec in workload.RUNS:
        at = workload.run_time(spec["id"])
        with recorder.run(workload.WORKFLOW, version, spec["id"]).active() as run:
            drive(replies, run, spec, version)
            run.summary(spec["terminal"], recorded_at=at, metadata=listener.aggregates(run))
            if spec["outcome"]["delayed"]:
                run.outcome(None, maturity="provisional", occurred_at=at)
                at = at + timedelta(hours=1)
            run.outcome(spec["outcome"]["accepted"], occurred_at=at)
    assert replies.pending == 0, "every queued reply was consumed"
    return replies


def modes(recorder: Recorder, workflow: str) -> dict:
    """Mode scenarios; the harness asserts their rows from the database."""
    first = instrument_crewai()
    assert instrument_crewai() is first, "registration is idempotent"
    replies = fakes.Replies()
    report = {}

    def concurrent(label: str):
        replies.expect(app.PLANNER, (fakes.final(label), fakes.usage(100, 10)))
        with recorder.run(workflow, "v1", f"modes-concurrent-{label}").active():
            app.single("planner", replies.llm(app.PLANNER), f"question {label}")

    threads = [
        threading.Thread(target=copy_context().run, args=(concurrent, label)) for label in "AB"
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with recorder.run(workflow, "v1", "modes-retry").active():
        replies.expect(
            app.PLANNER, RuntimeError("transient"), (fakes.final("ok"), fakes.usage(50, 5))
        )
        app.single("planner", replies.llm(app.PLANNER), "retry me", retries=1)

    with recorder.run(workflow, "v1", "modes-flow").active():
        replies.expect(app.QUICK, ("draft", fakes.usage(30, 3)), ("polished", fakes.usage(40, 4)))
        report["flow"] = app.SummaryFlow(replies.llm(app.QUICK), "q").kickoff()

    with recorder.run(workflow, "v1", "modes-parallel").active():
        replies.expect(
            app.PLANNER,
            (fakes.final("a"), fakes.usage(60, 6)),
            (fakes.final("b"), fakes.usage(61, 6)),
        )
        replies.expect(app.QUICK, (fakes.final("merged"), fakes.usage(70, 7)))
        app.parallel_then_merge(replies.llm(app.PLANNER), replies.llm(app.QUICK), "parallel")

    with recorder.run(workflow, "v1", "modes-bare").active():
        replies.expect("gpt-4.1-mini", (fakes.final("bare"), fakes.usage(10, 1)))
        app.single("planner", replies.llm("gpt-4.1-mini"), "bare model name")

    assert replies.pending == 0
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroth-url", required=True)
    parser.add_argument("--zeroth-key", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--scenario", choices=["reference", "modes"], default="reference")
    args = parser.parse_args(argv)
    import crewai
    from zeroth.sdk import ZerothClient

    workload = _load("reference_workload", Path(args.workload))
    recorder = Recorder(ZerothClient(api_key=args.zeroth_key, base_url=args.zeroth_url))
    if args.scenario == "reference":
        reference(recorder, args.version, workload)
    else:
        modes(recorder, workload.WORKFLOW)
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "crewai": crewai.__version__,
                "lost": len(recorder.lost),
                "version": args.version,
                "scenario": args.scenario,
            }
        )
    )
    return 0 if not recorder.lost else 1


if __name__ == "__main__":
    raise SystemExit(main())
