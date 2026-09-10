"""Run the frozen Phase 3 reference workload through the AgentChat application.

The instrumentation the recipe adds: wrap each agent's model client in
``ZerothChatCompletionClient`` with the model and provider it bills, open a run
per request, charge tools you price yourself, record the outcome you own.
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

from zeroth.instrumentation import Recorder
from zeroth.instrumentation.autogen import ZerothChatCompletionClient


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app = _load("c05_autogen_app", Path(__file__).with_name("app.py"))
fakes = _load("c05_autogen_fakes", Path(__file__).with_name("fakes.py"))


def wrap(clients: fakes.Clients, model: str, step: str) -> ZerothChatCompletionClient:
    provider = "anthropic" if model.startswith("claude") else "openai"
    return ZerothChatCompletionClient(
        clients.fake(model), model=model, provider=provider, step=step
    )


async def drive(clients: fakes.Clients, run, spec: dict, version: str) -> None:
    calls, question = spec["calls"], f"question for {spec['id']}"

    def queued(index: int, text: str = "done", **overrides):
        call = calls[index]
        clients.fake(call["model"]).expect(fakes.reply(text, call["usage"][version], **overrides))

    family = spec["id"]
    if family == "success":
        queued(0, "plan"), queued(1, "TERMINATE done")
        await app.plan_then_write(
            wrap(clients, app.PLANNER, "planner"), wrap(clients, app.WRITER, "writer"), question
        )
    elif family == "rejection":
        queued(0)
        await app.run_single("reviewer", wrap(clients, app.REVIEWER, "reviewer"), question)
    elif family in ("delayed_outcome", "duplicate_delivery"):
        queued(0)
        await app.run_single("quick", wrap(clients, app.QUICK, "quick"), question)
    elif family == "retry":
        queued(0, "not json"), queued(1, '{"ok": true}')
        await app.robust_extract(wrap(clients, app.PLANNER, "planner"), question)
    elif family == "parallel_calls":
        queued(0), queued(1), queued(2)
        planner = wrap(clients, app.PLANNER, "planner")
        await app.fanout(planner, [question + " a", question + " b"])
        await app.run_single("quick", wrap(clients, app.QUICK, "quick"), "merge")
    elif family == "tool_cost":
        tool = calls[1]
        run.tool_charge(
            tool["step"], tool=tool["tool"], cost_usd=Decimal(tool["cost_usd"][version])
        )
        queued(0)
        await app.run_single("reviewer", wrap(clients, app.REVIEWER, "reviewer"), question)
    elif family == "fallback":
        clients.fake(calls[0]["model"]).expect(RuntimeError("provider unavailable"))
        queued(1)
        await app.answer_with_fallback(
            wrap(clients, app.WRITER, "writer"), wrap(clients, app.REVIEWER, "reviewer"), question
        )
    elif family == "missing_usage":
        queued(0)
        await app.run_single("planner", wrap(clients, app.PLANNER, "planner"), question)
    else:
        raise ValueError(f"unmapped family {family}")


async def reference(recorder: Recorder, version: str, workload) -> fakes.Clients:
    clients = fakes.Clients()
    for spec in workload.RUNS:
        at = workload.run_time(spec["id"])
        with recorder.run(workload.WORKFLOW, version, spec["id"]).active() as run:
            await drive(clients, run, spec, version)
            run.summary(
                spec["terminal"], recorded_at=at, metadata={"framework": "autogen-agentchat"}
            )
            if spec["outcome"]["delayed"]:
                run.outcome(None, maturity="provisional", occurred_at=at)
                at = at + timedelta(hours=1)
            run.outcome(spec["outcome"]["accepted"], occurred_at=at)
    assert clients.pending == 0, "every queued reply was consumed"
    return clients


async def modes(recorder: Recorder, workflow: str) -> dict:
    """Mode scenarios; the harness asserts their rows from the database."""
    from autogen_core import CancellationToken

    clients = fakes.Clients()
    report: dict = {}
    with recorder.run(workflow, "v1", "modes-team").active() as run:
        clients.fake(app.PLANNER).expect(fakes.reply("plan", [100, 10]))
        clients.fake(app.WRITER).expect(fakes.reply("TERMINATE done", [200, 20]))
        report["team"] = await app.plan_then_write(
            wrap(clients, app.PLANNER, "planner"), wrap(clients, app.WRITER, "writer"), "q"
        )
        run.summary("completed")
        run.outcome(True)
    with recorder.run(workflow, "v1", "modes-stream").active():
        clients.fake(app.QUICK).expect(fakes.reply("streamed reply", [30, 3]))
        text, chunks = await app.stream_single("quick", wrap(clients, app.QUICK, "quick"), "q")
        report["stream"] = {"text": text, "chunks": chunks}
    with recorder.run(workflow, "v1", "modes-missing").active():
        clients.fake(app.PLANNER).expect(fakes.reply("no usage", None))
        await app.run_single("planner", wrap(clients, app.PLANNER, "planner"), "q")
    with recorder.run(workflow, "v1", "modes-cached").active():
        clients.fake(app.PLANNER).expect(fakes.reply("from cache", [40, 4], cached=True))
        await app.run_single("planner", wrap(clients, app.PLANNER, "planner"), "q")
    with recorder.run(workflow, "v1", "modes-cancelled").active():
        token = CancellationToken()
        token.cancel()
        client = wrap(clients, app.PLANNER, "planner")
        clients.fake(app.PLANNER).expect(fakes.reply("never", [1, 1]))
        try:
            await client.create([], cancellation_token=token)
        except RuntimeError as error:
            report["cancelled"] = str(error)
        clients.fake(app.PLANNER).queue.clear()
    assert clients.pending == 0
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroth-url", required=True)
    parser.add_argument("--zeroth-key", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--scenario", choices=["reference", "modes"], default="reference")
    args = parser.parse_args(argv)
    import autogen_agentchat
    import autogen_core
    from zeroth.sdk import ZerothClient

    workload = _load("reference_workload", Path(args.workload))
    recorder = Recorder(ZerothClient(api_key=args.zeroth_key, base_url=args.zeroth_url))
    report: dict = {}
    if args.scenario == "reference":
        asyncio.run(reference(recorder, args.version, workload))
    else:
        report = asyncio.run(modes(recorder, workload.WORKFLOW))
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "autogen_agentchat": autogen_agentchat.__version__,
                "autogen_core": autogen_core.__version__,
                "lost": len(recorder.lost),
                "version": args.version,
                "scenario": args.scenario,
                "report": report,
            }
        )
    )
    return 0 if not recorder.lost else 1


if __name__ == "__main__":
    raise SystemExit(main())
