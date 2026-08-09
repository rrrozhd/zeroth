"""An ephemeral acceptance candidate that is the real service, not a stand-in.

The point of a black-box acceptance suite is that the thing under test is the product.
A hand-written server whose responses are authored to satisfy the contract proves only
that the runner speaks HTTP; it cannot fail for any reason the product could fail for.

So this candidate boots the real Zeroth app over real HTTP, against a file-backed
database carrying real Alembic migrations, with a real approval-gated deployment. The
only stubbed component is the node runner, which stands in for a model provider — the
same seam the service suite already uses. Restart tears the process down and brings a
freshly built app up **against the same database file**, which is what makes durability
evidence mean anything: a candidate that restarted onto an empty store would satisfy
before/after anchors for reasons that have nothing to do with durability.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from tests.service.helpers import (
    CountingFinishRunner,
    approval_resume_graph,
    default_service_auth_config,
    deploy_service,
)
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.service.bootstrap import bootstrap_app
from zeroth.service.bootstrap.migrations import run_migrations

APPROVAL_NODE = "approval-step"
FINISH_NODE = "finish-step"
DEPLOYMENT_REF = "acceptance-candidate"
_START_DEADLINE_SECONDS = 20.0
_STOP_DEADLINE_SECONDS = 20.0


class CandidateError(RuntimeError):
    """The ephemeral candidate did not reach a usable serving state."""


@dataclass(slots=True)
class _Server:
    server: uvicorn.Server
    thread: threading.Thread


class EphemeralCandidate:
    """Serve, restart and drain the real application on a stable origin."""

    def __init__(self, workspace: Path, *, deployment_ref: str = DEPLOYMENT_REF) -> None:
        self.deployment_ref = deployment_ref
        self._db_path = workspace / "candidate.db"
        self._listener: socket.socket | None = None
        self._running: _Server | None = None
        self.port: int | None = None
        # A fresh counter per boot. The durable evidence is the audit record the
        # deployment itself publishes, not this process-local number; the counter
        # exists so an in-process test can cross-check what the API reports.
        self.finish_runner = CountingFinishRunner()

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise CandidateError("candidate has no bound origin yet")
        return f"http://127.0.0.1:{self.port}"

    async def provision(self) -> None:
        """Migrate the database and deploy the approval-gated graph exactly once."""
        run_migrations(f"sqlite:///{self._db_path}")
        database = AsyncSQLiteDatabase(path=str(self._db_path))
        try:
            await deploy_service(
                database,
                approval_resume_graph(graph_id="acceptance-approval-graph"),
                deployment_ref=self.deployment_ref,
            )
        finally:
            await database.close()
        self._bind()

    def _bind(self) -> None:
        """Bind the candidate's origin, keeping the same port across restarts.

        Callers configure a base URL once. If a restart landed on a new port the
        suite would be probing a different origin than the one it was pointed at,
        and every post-restart assertion would be meaningless. uvicorn closes the
        socket it was handed on shutdown, so each boot binds a fresh socket to the
        port the first boot claimed rather than reusing a closed descriptor.
        """
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port or 0))
        listener.listen()
        self._listener = listener
        self.port = listener.getsockname()[1]

    async def _build_app(self):
        database = AsyncSQLiteDatabase(path=str(self._db_path))
        return await bootstrap_app(
            database,
            deployment_ref=self.deployment_ref,
            agent_runners={FINISH_NODE: self.finish_runner},
            auth_config=default_service_auth_config(),
        )

    async def serve(self) -> None:
        """Start the application and return only once it is accepting requests."""
        if self._running is not None:
            raise CandidateError("candidate is already serving")
        if self.port is None:
            raise CandidateError("candidate was never provisioned")
        if self._listener is None:
            self._bind()
        app = await self._build_app()
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        listener = self._listener
        thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(sockets=[listener])),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + _START_DEADLINE_SECONDS
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise CandidateError(
                    f"candidate did not start within {_START_DEADLINE_SECONDS:g}s "
                    f"(thread alive: {thread.is_alive()})"
                )
            await asyncio.sleep(0.02)
        self._running = _Server(server=server, thread=thread)

    async def stop(self) -> None:
        """Stop the serving process, leaving the database intact."""
        running = self._running
        if running is None:
            return
        self._running = None
        running.server.should_exit = True
        deadline = time.monotonic() + _STOP_DEADLINE_SECONDS
        while running.thread.is_alive():
            if time.monotonic() >= deadline:
                raise CandidateError(f"candidate did not stop within {_STOP_DEADLINE_SECONDS:g}s")
            await asyncio.sleep(0.02)
        # uvicorn closed the descriptor it was serving on; drop our handle so the
        # next boot binds a fresh socket to the same port.
        self._listener = None

    # LifecycleController -------------------------------------------------

    async def restart(self) -> None:
        """Replace the serving process against the same database and port."""
        await self.stop()
        # A restart that reuses the live counter would let post-restart evidence be
        # satisfied by pre-restart in-process state.
        self.finish_runner = CountingFinishRunner()
        await self.serve()

    async def shutdown(self) -> None:
        """Withdraw the candidate from service."""
        await self.stop()

    async def aclose(self) -> None:
        await self.stop()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self.port = None
