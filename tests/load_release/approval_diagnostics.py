"""Opt-in load diagnostics: metadata only, without changing probe outcomes."""

import asyncio
import json
import logging
import time
from pathlib import Path


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
            self.active[token] = (name, started)
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
               "active": [{"operation": name, "elapsed_ms": (now-started)*1000}
                          for name, started in self.active.values()]}
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

    sink = Diagnostics(path)
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
