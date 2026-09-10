"""Run the frozen Phase 3 reference workload through the Agents SDK application.

The instrumentation the recipe adds: a ``ZerothRunHooks`` per request passed to
``Runner.run``, a run per request, tool charges the application prices itself,
and the outcome the application owns. Fake models via ``RunConfig.model_provider``
stand in for providers so a clean environment can reproduce the workload.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from agents import RunConfig
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.openai_agents import ZerothRunHooks


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app = _load("c03_openai_agents_app", Path(__file__).with_name("app.py"))
fakes = _load("c03_openai_agents_fakes", Path(__file__).with_name("fakes.py"))


async def drive(provider: fakes.FakeProvider, run, spec: dict, version: str) -> None:
    """Map one reference family onto agent runs."""
    calls, question = spec["calls"], f"question for {spec['id']}"
    config = RunConfig(model_provider=provider, tracing_disabled=True)

    def queued(index: int, output=None, text: str = "ok"):
        call = calls[index]
        provider.get_model(call["model"]).expect(fakes.reply(output, call["usage"][version], text))

    family = spec["id"]
    with ZerothRunHooks(model_provider=provider) as hooks:
        if family == "success":
            queued(0, [fakes.tool_call("transfer_to_writer", {})])
            queued(1)
            await app.run(
                app.planner(handoff_to=app.writer()), question, hooks=hooks, config=config
            )
        elif family == "rejection":
            queued(0)
            await app.run(app.reviewer(), question, hooks=hooks, config=config)
        elif family in ("delayed_outcome", "duplicate_delivery"):
            queued(0)
            await app.run(app.quick(), question, hooks=hooks, config=config)
        elif family == "retry":
            queued(0, text="not json")
            queued(1, text='{"ok": true}')
            await app.robust_extract(app.planner(), question, hooks=hooks, config=config)
        elif family == "parallel_calls":
            queued(0), queued(1), queued(2)
            await app.fanout(
                app.planner(), [question + " a", question + " b"], hooks=hooks, config=config
            )
            await app.run(app.quick(), "merge", hooks=hooks, config=config)
        elif family == "tool_cost":
            tool = calls[1]
            run.tool_charge(
                tool["step"], tool=tool["tool"], cost_usd=Decimal(tool["cost_usd"][version])
            )
            queued(0)
            await app.run(app.reviewer(), question, hooks=hooks, config=config)
        elif family == "fallback":
            provider.get_model(calls[0]["model"]).expect(RuntimeError("provider unavailable"))
            queued(1)
            await app.answer_with_fallback(
                app.writer(), app.reviewer(), question, hooks=hooks, config=config
            )
        elif family == "missing_usage":
            queued(0)
            await app.run(app.planner(), question, hooks=hooks, config=config)
        else:
            raise ValueError(f"unmapped family {family}")
    return hooks


async def reference(recorder: Recorder, version: str, workload) -> fakes.FakeProvider:
    provider = fakes.FakeProvider()
    for spec in workload.RUNS:
        at = workload.run_time(spec["id"])
        with recorder.run(workload.WORKFLOW, version, spec["id"]).active() as run:
            hooks = await drive(provider, run, spec, version)
            run.summary(spec["terminal"], recorded_at=at, metadata=hooks.aggregates())
            if spec["outcome"]["delayed"]:
                run.outcome(None, maturity="provisional", occurred_at=at)
                at = at + timedelta(hours=1)
            run.outcome(spec["outcome"]["accepted"], occurred_at=at)
    assert provider.pending == 0, "every queued reply was consumed"
    return provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroth-url", required=True)
    parser.add_argument("--zeroth-key", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--version", default="v1")
    args = parser.parse_args(argv)
    import agents
    import openai
    from zeroth.sdk import ZerothClient

    workload = _load("reference_workload", Path(args.workload))
    recorder = Recorder(ZerothClient(api_key=args.zeroth_key, base_url=args.zeroth_url))
    asyncio.run(reference(recorder, args.version, workload))
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "openai_agents": agents.__version__,
                "openai": openai.__version__,
                "lost": len(recorder.lost),
                "version": args.version,
            }
        )
    )
    return 0 if not recorder.lost else 1


if __name__ == "__main__":
    raise SystemExit(main())
