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


@pytest.mark.parametrize("outcome", ["returned", "error", "cancelled"])
async def test_transaction_ownership_is_visible_and_removed_on_exit(tmp_path, monkeypatch, outcome):
    from contextlib import asynccontextmanager
    from tests.load_release.approval_diagnostics import Diagnostics

    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    original_error = ValueError("transaction-secret-canary")
    connection = SimpleNamespace(_conn=SimpleNamespace(info=SimpleNamespace(backend_pid=123)))

    class Database:
        @asynccontextmanager
        async def transaction(self, *, write_lock=False):
            try:
                yield connection
            finally:
                cleaned.set()

    sink = Diagnostics(tmp_path / "trace.jsonl")
    sink.instrument_transactions(monkeypatch, Database)

    async def owner():
        async with Database().transaction(write_lock=True) as acquired:
            assert acquired is connection
            entered.set()
            await release.wait()
            if outcome == "error":
                raise original_error

    task = asyncio.create_task(owner())
    await entered.wait()
    try:
        rows = sink.transaction_snapshot()
        assert len(rows) == 1
        assert rows[0]["pid"] == 123
        assert rows[0]["phase"] == "acquired"
        assert rows[0]["write_lock"] is True
        assert rows[0]["elapsed_ms"] >= 0
        assert any(frame["function"] == "owner" for frame in rows[0]["owner"])
        assert "secret-canary" not in json.dumps(rows)
    finally:
        if outcome == "cancelled":
            task.cancel()
        release.set()
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "error":
            with pytest.raises(ValueError) as captured:
                await task
            assert captured.value is original_error
        else:
            await task
    assert cleaned.is_set()
    assert sink.transaction_snapshot() == []


@pytest.mark.parametrize("phase", ["acquiring", "exiting"])
async def test_transaction_snapshot_distinguishes_acquisition_from_cleanup(tmp_path, monkeypatch, phase):
    from contextlib import asynccontextmanager
    from tests.load_release.approval_diagnostics import Diagnostics

    waiting = asyncio.Event()
    release = asyncio.Event()
    connection = SimpleNamespace(_conn=SimpleNamespace(info=SimpleNamespace(backend_pid=456)))

    async def pause():
        waiting.set()
        await release.wait()

    class Database:
        @asynccontextmanager
        async def transaction(self, *, write_lock=False):
            if phase == "acquiring":
                await pause()
            try:
                yield connection
            finally:
                if phase == "exiting":
                    await pause()

    sink = Diagnostics(tmp_path / "trace.jsonl")
    sink.instrument_transactions(monkeypatch, Database)

    async def owner():
        async with Database().transaction():
            pass

    task = asyncio.create_task(owner())
    await waiting.wait()
    try:
        row, = sink.transaction_snapshot()
        assert row["phase"] == phase
        assert row["pid"] == (456 if phase == "exiting" else None)
    finally:
        release.set()
        await task
    assert sink.transaction_snapshot() == []
