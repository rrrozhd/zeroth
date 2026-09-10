"""The frozen reference workload every recipe must reproduce.

Nine families: success, rejection, delayed outcome, retry, parallel calls,
tool cost, fallback, missing usage and duplicate delivery (the last is a
delivery behaviour: the harness sends every event more than once). Two
versions of the same workflow carry different usage so version comparison
has something to compare. Usage triples are ``[input, output, cache_read]``
with ``input`` excluding cached tokens. Expected totals live in
``expected_ledger.json``; this module never computes them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zeroth.instrumentation import Recorder, Usage

WORKFLOW = "phase3-reference"
VERSIONS = ("v1", "v2")
BASE_TIME = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
FAMILIES = (
    "success", "rejection", "delayed_outcome", "retry", "parallel_calls",
    "tool_cost", "fallback", "missing_usage", "duplicate_delivery",
)


def _model(step, model, v1, v2, *, attempt=1, error=None):
    provider = "anthropic" if model.startswith("claude") else "openai"
    return {
        "step": step, "attempt": attempt, "kind": "model", "provider": provider,
        "model": model, "usage": {"v1": v1, "v2": v2}, "error": error,
    }


RUNS = (
    {
        "id": "success", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": False},
        "calls": [
            _model("plan", "gpt-4.1-mini", [1200, 300, 0], [900, 240, 0]),
            _model("answer", "claude-sonnet-4-20250514", [2000, 500, 800], [1500, 420, 600]),
        ],
    },
    {
        "id": "rejection", "terminal": "completed",
        "outcome": {"accepted": False, "delayed": False},
        "calls": [_model("answer", "gpt-4.1", [1500, 400, 0], [1100, 350, 0])],
    },
    {
        "id": "delayed_outcome", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": True},
        "calls": [_model("answer", "claude-haiku-4-5-20251001", [700, 200, 0], [600, 180, 0])],
    },
    {
        "id": "retry", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": False},
        "calls": [
            _model("answer", "gpt-4.1-mini", [800, 40, 0], [700, 30, 0], error="APIError"),
            _model("answer", "gpt-4.1-mini", [800, 260, 0], [700, 220, 0], attempt=2),
        ],
    },
    {
        "id": "parallel_calls", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": False},
        "calls": [
            _model("fanout-a", "gpt-4.1-mini", [600, 150, 0], [500, 120, 0]),
            _model("fanout-b", "gpt-4.1-mini", [650, 160, 0], [520, 130, 0]),
            _model("merge", "claude-haiku-4-5-20251001", [900, 200, 0], [700, 160, 0]),
        ],
    },
    {
        "id": "tool_cost", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": False},
        "calls": [
            _model("answer", "gpt-4.1", [1000, 300, 0], [800, 240, 0]),
            {"step": "search", "attempt": 1, "kind": "tool", "tool": "web_search",
             "cost_usd": {"v1": "0.00500000", "v2": "0.00500000"}},
        ],
    },
    {
        "id": "fallback", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": False},
        "calls": [
            _model("answer", "claude-sonnet-4-20250514", None, None, error="ServiceUnavailable"),
            _model("answer", "gpt-4.1", [1300, 350, 0], [1000, 280, 0], attempt=2),
        ],
    },
    {
        "id": "missing_usage", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": False},
        "calls": [_model("answer", "gpt-4.1-mini", None, None)],
    },
    {
        "id": "duplicate_delivery", "terminal": "completed",
        "outcome": {"accepted": True, "delayed": False},
        "calls": [_model("answer", "claude-haiku-4-5-20251001", [500, 120, 0], [400, 100, 0])],
    },
)


_MINUTE = {spec["id"]: index for index, spec in enumerate(RUNS)}


def run_time(run_id: str) -> datetime:
    """Each run's fixed timestamp; identity must not depend on replay order."""
    return BASE_TIME + timedelta(minutes=_MINUTE[run_id])


def replay(recorder: Recorder, version: str, *, runs=RUNS) -> None:
    """Drive the workload through the shared capture contract (the C07 Python path)."""
    for spec in runs:
        at = run_time(spec["id"])
        run = recorder.run(WORKFLOW, version, spec["id"])
        for call in spec["calls"]:
            if call["kind"] == "tool":
                run.tool_charge(
                    call["step"], tool=call["tool"], attempt=call["attempt"],
                    cost_usd=Decimal(call["cost_usd"][version]), recorded_at=at,
                )
                continue
            usage = call["usage"][version]
            run.charge(
                call["step"], attempt=call["attempt"], model=call["model"],
                provider=call["provider"], error=call["error"], recorded_at=at,
                usage=None if usage is None else Usage(usage[0], usage[1], usage[2]),
            )
        run.summary(spec["terminal"], recorded_at=at)
        if spec["outcome"]["delayed"]:
            run.outcome(None, maturity="provisional", occurred_at=at)
            at = at + timedelta(hours=1)
        run.outcome(spec["outcome"]["accepted"], occurred_at=at)
