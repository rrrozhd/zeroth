"""Run the frozen Phase 3 reference workload through the LangGraph application.

The instrumentation the recipe adds: one ``ZerothCallbackHandler`` in the
config of every graph invocation, a run per request, a tool charge the
application prices itself, and the outcome the application owns. Fake chat
models with queued replies stand in for providers so a clean environment can
reproduce the workload without keys or network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import deque
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.langchain import ZerothCallbackHandler


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and pickling resolve the module by name
    spec.loader.exec_module(module)
    return module


app = _load("c02_langgraph_app", Path(__file__).with_name("app.py"))

PLANNER, REVIEWER, WRITER, QUICK = (
    "gpt-4.1-mini",
    "gpt-4.1",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
)


class FakeModel(GenericFakeChatModel):
    """A fake chat model fed from a queue; an Exception in the queue is raised as the reply."""

    def __init__(self, name: str) -> None:
        super().__init__(messages=iter(()))
        self._name, self._queue = name, deque()

    def expect(self, *items) -> None:
        self._queue.extend(items)

    @property
    def pending(self) -> int:
        return len(self._queue)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        item = self._queue.popleft()
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


def reply(model: str, usage: list | None, text: str = "ok") -> AIMessage:
    provider = "anthropic" if model.startswith("claude") else "openai"
    metadata = {"model_name": model, "model_provider": provider, "id": "chatcmpl-fixture"}
    if usage is None:
        return AIMessage(content=text, response_metadata=metadata)
    inp, out, cache = usage
    return AIMessage(
        content=text,
        response_metadata=metadata,
        usage_metadata=UsageMetadata(
            input_tokens=inp + cache,
            output_tokens=out,
            total_tokens=inp + cache + out,
            input_token_details={"cache_read": cache},
        ),
    )


def models() -> dict[str, FakeModel]:
    return {name: FakeModel(name) for name in (PLANNER, REVIEWER, WRITER, QUICK)}


def drive(fakes: dict[str, FakeModel], run, spec: dict, version: str, handler) -> None:
    """Map one reference family onto graph operations."""
    calls, question = spec["calls"], f"question for {spec['id']}"
    config = {"callbacks": [handler]}

    def queued(index: int, **overrides):
        call = calls[index]
        fakes[call["model"]].expect(reply(call["model"], call["usage"][version], **overrides))

    family = spec["id"]
    if family == "success":
        queued(0), queued(1)
        app.plan_then_answer(fakes[PLANNER], fakes[WRITER]).invoke(app.initial(question), config)
    elif family == "rejection":
        queued(0)
        app.single(fakes[REVIEWER]).invoke(app.initial(question), config)
    elif family in ("delayed_outcome", "duplicate_delivery"):
        queued(0)
        app.single(fakes[QUICK]).invoke(app.initial(question), config)
    elif family == "retry":
        queued(0, text="not json"), queued(1, text='{"ok": true}')
        with_callbacks = fakes[PLANNER].with_config(config)
        app.robust_extract(with_callbacks, question)
    elif family == "parallel_calls":
        queued(0), queued(1), queued(2)
        app.fanout_then_merge(fakes[PLANNER], fakes[QUICK]).invoke(app.initial(question), config)
    elif family == "tool_cost":
        tool = calls[1]
        run.tool_charge(
            tool["step"], tool=tool["tool"], cost_usd=Decimal(tool["cost_usd"][version])
        )
        queued(0)
        app.single(fakes[REVIEWER]).invoke(app.initial(question), config)
    elif family == "fallback":
        fakes[WRITER].expect(RuntimeError("provider unavailable"))
        queued(1)
        chain = app.with_fallback(fakes[WRITER], fakes[REVIEWER]).with_config(config)
        chain.invoke(question)
    elif family == "missing_usage":
        queued(0)
        app.single(fakes[PLANNER]).invoke(app.initial(question), config)
    else:
        raise ValueError(f"unmapped family {family}")


def reference(recorder: Recorder, version: str, workload) -> dict[str, FakeModel]:
    fakes = models()
    for spec in workload.RUNS:
        at = workload.run_time(spec["id"])
        with recorder.run(workload.WORKFLOW, version, spec["id"]).active() as run:
            drive(fakes, run, spec, version, ZerothCallbackHandler())
            run.summary(spec["terminal"], recorded_at=at)
            if spec["outcome"]["delayed"]:
                run.outcome(None, maturity="provisional", occurred_at=at)
                at = at + timedelta(hours=1)
            run.outcome(spec["outcome"]["accepted"], occurred_at=at)
    assert all(fake.pending == 0 for fake in fakes.values()), "every queued reply was consumed"
    return fakes


def load_workload(path: str):
    return _load("reference_workload", Path(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroth-url", required=True)
    parser.add_argument("--zeroth-key", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--version", default="v1")
    args = parser.parse_args(argv)
    import langchain_core
    import langgraph.version
    from zeroth.sdk import ZerothClient

    workload = load_workload(args.workload)
    recorder = Recorder(ZerothClient(api_key=args.zeroth_key, base_url=args.zeroth_url))
    reference(recorder, args.version, workload)
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "langchain_core": langchain_core.__version__,
                "langgraph": langgraph.version.__version__,
                "lost": len(recorder.lost),
                "version": args.version,
            }
        )
    )
    return 0 if not recorder.lost else 1


if __name__ == "__main__":
    raise SystemExit(main())
