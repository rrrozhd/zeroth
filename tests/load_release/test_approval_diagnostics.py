"""Failure diagnostics identify waits without recording application data."""

import asyncio
import json
from types import SimpleNamespace

import pytest


async def test_failure_records_await_chain_without_locals_or_exception_text(tmp_path, monkeypatch):
    from tests.load_release.approval_diagnostics import Diagnostics

    entered = asyncio.Event()

    async def blocked():
        secret = "diagnostic-secret-canary"
        entered.set()
        await asyncio.Event().wait()
        return secret

    task = asyncio.create_task(blocked())
    await entered.wait()
    sink = Diagnostics(tmp_path / "trace.jsonl")

    async def database(_dsn):
        raise RuntimeError("database-secret-canary")

    monkeypatch.setattr(sink, "database_waits", database)
    try:
        await sink.capture_failure(ValueError("request-secret-canary"), "soak", 7, "dsn")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    text = sink.path.read_text()
    row = json.loads(text)
    assert row["error"] == "ValueError"
    assert row["database_diagnostic_error"] == "RuntimeError"
    assert any(frame["function"] == "blocked" for chain in row["tasks"] for frame in chain)
    assert "secret-canary" not in text


async def test_stage_wrapper_preserves_failure_identity_and_records_only_type(tmp_path, monkeypatch):
    from tests.load_release.approval_diagnostics import Diagnostics

    original = ValueError("secret-canary")

    async def fail():
        raise original

    owner = SimpleNamespace(stage=fail)
    sink = Diagnostics(tmp_path / "trace.jsonl")
    sink.instrument(monkeypatch, owner, "stage")
    with pytest.raises(ValueError) as captured:
        await owner.stage()
    assert captured.value is original
    row = json.loads(sink.path.read_text())
    assert row["operation"] == "stage"
    assert row["outcome"] == "ValueError"
    assert row["elapsed_ms"] >= 0
    assert "secret-canary" not in sink.path.read_text()
