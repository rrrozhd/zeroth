"""Opt-in load diagnostics: metadata only, without changing probe outcomes."""

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from tests.load_release.cpu_sampling import CPUSampler


def await_chain(task):
    """Describe suspended code locations without frame locals or arguments."""
    current = task.get_coro()
    frames = []
    seen = set()
    while current is not None and id(current) not in seen and len(frames) < 32:
        seen.add(id(current))
        frame = getattr(current, "cr_frame", None) or getattr(current, "gi_frame", None)
        if frame is not None:
            frames.append({"file": frame.f_code.co_filename,
                           "function": frame.f_code.co_name, "line": frame.f_lineno})
        current = getattr(current, "cr_await", None) or getattr(current, "gi_yieldfrom", None)
    return frames


class Diagnostics:
    """Retain stage durations and one failure-time wait inventory."""

    def __init__(self, path: Path):
        self.path = path
        self.captured = False
        self.active = {}
        self.sequence = 0
        self.transactions = {}
        self.loop_samples = deque(maxlen=128)
        self.loop_count = 0
        self.loop_max_lag = 0
        self.loop_started = time.perf_counter()
        self.cpu_started = time.process_time()
        self.cpu_sampler = CPUSampler()

    def loop_snapshot(self):
        """Report elapsed CPU and scheduler delays without inspecting application data."""
        return {"elapsed_seconds": time.perf_counter() - self.loop_started,
                "cpu_seconds": time.process_time() - self.cpu_started,
                "max_lag_ms": self.loop_max_lag,
                "samples": self.loop_count, "recent_lag": list(self.loop_samples),
                "cpu_samples": self.cpu_sampler.snapshot()}

    @asynccontextmanager
    async def monitor_loop(self, profile):
        """Own one low-frequency timing observer for the exact profile lifetime."""
        self.loop_samples.clear()
        self.loop_count = 0
        self.loop_max_lag = 0
        self.loop_started = time.perf_counter()
        self.cpu_started = time.process_time()

        loop = asyncio.get_running_loop()
        previous = time.perf_counter()

        def sample():
            nonlocal previous, observer
            now = time.perf_counter()
            lag = max(0, (now - previous - .05) * 1000)
            self.loop_count += 1
            self.loop_max_lag = max(self.loop_max_lag, lag)
            self.loop_samples.append({"at_ms": (now-self.loop_started)*1000,
                                      "lag_ms": lag})
            previous = now
            observer = loop.call_later(.05, sample)

        observer = loop.call_later(.05, sample)
        self.cpu_sampler = CPUSampler()
        try:
            with self.cpu_sampler:
                yield
        finally:
            observer.cancel()
            self.record({"operation": "profile_timing", "profile": profile,
                         **self.loop_snapshot()})

    def instrument_transactions(self, monkeypatch, owner):
        """Associate PostgreSQL backend IDs with tasks without reading SQL or locals."""
        original = owner.transaction

        @asynccontextmanager
        async def measured(database, *, write_lock=False):
            token = object()
            state = {"started": time.perf_counter(), "task": asyncio.current_task(),
                     "pid": None, "phase": "acquiring", "write_lock": write_lock}
            self.transactions[token] = state
            try:
                async with original(database, write_lock=write_lock) as connection:
                    state.update(pid=connection._conn.info.backend_pid, phase="acquired")
                    try:
                        yield connection
                    except asyncio.CancelledError:
                        state["cancel_started"] = time.perf_counter()
                        raise
                    finally:
                        state["phase"] = "exiting"
            finally:
                self.transactions.pop(token, None)
                if "cancel_started" in state:
                    self.record({"operation": "transaction_cancellation_cleanup",
                                 "pid": state["pid"],
                                 "elapsed_ms": (time.perf_counter()-state["cancel_started"])*1000})

        monkeypatch.setattr(owner, "transaction", measured)

    def transaction_snapshot(self):
        """Capture pending acquisitions and holders, including bounded owner stacks."""
        now = time.perf_counter()
        return [{"pid": state["pid"], "phase": state["phase"],
                 "write_lock": state["write_lock"],
                 "elapsed_ms": (now-state["started"])*1000,
                 "cancelling": state["task"].cancelling() if state["task"] else 0,
                 "cancellation_cleanup_ms": (now-state["cancel_started"])*1000
                 if "cancel_started" in state else None,
                 "owner": await_chain(state["task"]) if state["task"] else []}
                for state in list(self.transactions.values())[:512]]

    def record(self, row):
        """Diagnostic output failure must not replace the product failure."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as stream:
                stream.write(json.dumps(row) + "\n")
        except OSError as error:
            logging.getLogger(__name__).warning("diagnostic write failed: %s", type(error).__name__)

    def instrument(self, monkeypatch, owner, name):
        original = getattr(owner, name)

        async def measured(*args, **kwargs):
            started = time.perf_counter()
            self.sequence += 1
            token = self.sequence
            self.active[token] = (name, started, asyncio.current_task())
            outcome = "returned"
            try:
                return await original(*args, **kwargs)
            except BaseException as error:
                outcome = type(error).__name__
                raise
            finally:
                self.active.pop(token, None)
                self.record({"operation": name, "elapsed_ms": (time.perf_counter()-started)*1000,
                             "outcome": outcome})

        monkeypatch.setattr(owner, name, measured)

    async def database_waits(self, dsn):
        """Read bounded PostgreSQL wait metadata; never query statement text."""
        import psycopg
        from psycopg.rows import dict_row

        async with asyncio.timeout(3):
            async with await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row) as connection:
                cursor = await connection.execute(
                    "SELECT pid, state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers "
                    "FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() ORDER BY pid LIMIT 64"
                )
                return await cursor.fetchall()

    async def capture_failure(self, error, profile, sequence, dsn):
        if self.captured:
            return
        self.captured = True
        now = time.perf_counter()
        row = {"operation": "settle_failure", "error": type(error).__name__,
               "profile": profile, "sequence": sequence,
               "tasks": [await_chain(task) for task in list(asyncio.all_tasks())[:256]],
               "transactions": self.transaction_snapshot(),
               "event_loop": self.loop_snapshot(),
               "active": [{"operation": name, "elapsed_ms": (now-started)*1000,
                           "cancelling": task.cancelling() if task else 0,
                           "owner": await_chain(task) if task else []}
                          for name, started, task in self.active.values()]}
        # Retain the captured stack even if the bounded database query fails.
        try:
            row["database_waits"] = await self.database_waits(dsn)
        except Exception as diagnostic_error:
            row["database_diagnostic_error"] = type(diagnostic_error).__name__
        finally:
            self.record(row)


def install(monkeypatch, path, postgres_dsn):
    """Attach diagnostics only to the explicitly configured product probe."""
    from zeroth.service.api import approval_api
    from zeroth.governance.approvals.service import ApprovalService
    from tests.load_release import workload_probe
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

    sink = Diagnostics(path)
    sink.instrument_transactions(monkeypatch, AsyncPostgresDatabase)
    for name in ("_wait_for_worker_run", "_require_visible_approval", "_wake_worker"):
        sink.instrument(monkeypatch, approval_api, name)
    for name in ("resolve", "schedule_continuation"):
        sink.instrument(monkeypatch, ApprovalService, name)
    original = workload_probe._settle_run

    async def settle(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except Exception as error:
            await sink.capture_failure(error, args[1], args[2], postgres_dsn)
            raise

    monkeypatch.setattr(workload_probe, "_settle_run", settle)
    original_profile = workload_probe._run_profile

    async def profile(targets, name, settings):
        async with sink.monitor_loop(name):
            return await original_profile(targets, name, settings)

    monkeypatch.setattr(workload_probe, "_run_profile", profile)
