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
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
from pydantic import BaseModel

from tests.service.helpers import (
    TEST_API_KEYS,
    approval_resume_graph,
    deploy_service,
    scoped_auth_config,
)
from zeroth.governance.identity import ServiceRole
from zeroth.platform.config import settings as settings_module
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.service.bootstrap import bootstrap_app
from zeroth.service.bootstrap.migrations import run_migrations

APPROVAL_NODE = "approval-step"
FINISH_NODE = "finish-step"
ARTIFACT_CONTRACT = "contract://acceptance-artifact-output"
DEPLOYMENT_REF = "acceptance-candidate"
TENANT_ID = "acceptance-ephemeral-leg"
_START_DEADLINE_SECONDS = 20.0
_STOP_DEADLINE_SECONDS = 20.0


class ArtifactCarryingPayload(BaseModel):
    """Output contract wide enough to carry an artifact reference.

    The shared `RunInputPayload` declares no `model_config`, so pydantic's default
    `extra="ignore"` applies and an artifact emitted alongside `value` is dropped
    during contract validation — before the driver could externalise it. A scenario
    written against the narrow contract would assert on an artifact the run never
    kept, so the candidate registers a wider one rather than widening the shared
    helper the service suite depends on.
    """

    value: int
    artifact: dict[str, Any] | None = None


@dataclass
class ArtifactEmittingRunner:
    """Emit a real artifact from the approval-gated node, and count executions."""

    artifact_store: Any = None
    namespace: str = ""
    call_count: int = 0

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.call_count += 1
        artifact: dict[str, Any] | None = None
        if self.artifact_store is not None:
            payload = b"acceptance artifact"
            key = f"{self.namespace}-artifact"
            stored = await self.artifact_store.store(
                key, payload, content_type="application/octet-stream"
            )
            reference = stored if isinstance(stored, dict) else None
            artifact = reference or {
                "store": "filesystem",
                "key": key,
                "content_type": "application/octet-stream",
                "size": len(payload),
            }
        return SimpleNamespace(
            output_data={"value": int(input_payload["value"]) + 1, "artifact": artifact},
            audit_record={"thread_id": thread_id, "runtime_context": dict(runtime_context or {})},
        )


class CandidateError(RuntimeError):
    """The ephemeral candidate did not reach a usable serving state."""


@dataclass(slots=True)
class _Server:
    server: uvicorn.Server
    thread: threading.Thread


class EphemeralCandidate:
    """Serve, restart and drain the real application on a stable origin."""

    def __init__(
        self,
        workspace: Path,
        *,
        deployment_ref: str = DEPLOYMENT_REF,
        tenant_id: str = TENANT_ID,
    ) -> None:
        self.deployment_ref = deployment_ref
        self.tenant_id = tenant_id
        self._db_path = workspace / "candidate.db"
        self._listener: socket.socket | None = None
        self._running: _Server | None = None
        self.port: int | None = None
        # A fresh counter per boot. The durable evidence is the audit record the
        # deployment itself publishes, not this process-local number; the counter
        # exists so an in-process test can cross-check what the API reports.
        self.finish_runner = ArtifactEmittingRunner()
        self._previous_redis_mode: str | None = None
        self._previous_settings: Any = None
        self._settings_saved = False

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise CandidateError("candidate has no bound origin yet")
        return f"http://127.0.0.1:{self.port}"

    def _declare_no_redis(self) -> None:
        """Tell the candidate the truth: it has no Redis.

        `determine_readiness_status` treats a *configured* Redis that will not answer
        as unhealthy, and `RedisSettings.mode` defaults to "local". Left alone, this
        candidate advertises a Redis on 127.0.0.1 that does not exist and reports
        itself unhealthy for a dependency it never had. Declaring it disabled is a
        statement of fact about the deployment, not a relaxed assertion.
        """
        self._previous_redis_mode = os.environ.get("ZEROTH_REDIS__MODE")
        # Restoring the exact object, not just clearing the cache: dropping the
        # singleton makes the next reader re-derive settings from whatever the
        # environment looks like then, which is not necessarily what the rest of the
        # suite started with. Putting the original back leaves no trace.
        self._previous_settings = settings_module._settings_singleton
        self._settings_saved = True
        os.environ["ZEROTH_REDIS__MODE"] = "disabled"
        settings_module._settings_singleton = None

    async def provision(self) -> None:
        """Migrate the database and deploy the approval-gated graph exactly once."""
        self._declare_no_redis()
        run_migrations(f"sqlite:///{self._db_path}")
        database = AsyncSQLiteDatabase(path=str(self._db_path))
        try:
            await deploy_service(
                database,
                self._graph(),
                deployment_ref=self.deployment_ref,
                extra_contract_models={ARTIFACT_CONTRACT: ArtifactCarryingPayload},
                auth_config=self._auth_config(),
                tenant_id=self.tenant_id,
            )
        finally:
            await database.close()
        self._bind()

    def _graph(self):
        """The shared approval graph, with the gated node able to emit an artifact.

        `approval_resume_graph` is used by the service suite, so it is copied rather
        than widened: only this candidate's finish node points at the wider contract.
        """
        graph = approval_resume_graph(graph_id="acceptance-approval-graph")
        nodes = [
            node.model_copy(update={"output_contract_ref": ARTIFACT_CONTRACT})
            if node.node_id == FINISH_NODE
            else node
            for node in graph.nodes
        ]
        return graph.model_copy(update={"nodes": nodes})

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

    def _auth_config(self):
        """Authenticate inside the tenant this candidate claims to be.

        Zeroth scopes a request by the credential that made it and ignores the
        acceptance headers entirely, so credentials bound to `default` would make the
        tenant in the report a restatement of configuration rather than something the
        deployment ever confirmed.
        """
        return scoped_auth_config(
            *(
                (f"{role.value}-key", TEST_API_KEYS[role.value], role, self.tenant_id, None)
                for role in (ServiceRole.OPERATOR, ServiceRole.REVIEWER, ServiceRole.ADMIN)
            )
        )

    async def _build_app(self):
        database = AsyncSQLiteDatabase(path=str(self._db_path))
        app = await bootstrap_app(
            database,
            deployment_ref=self.deployment_ref,
            agent_runners={FINISH_NODE: self.finish_runner},
            auth_config=self._auth_config(),
        )
        # The store only exists once the service is built, so hand the runner the live
        # one. An artifact written anywhere else would not be retrievable through the
        # API the scenario reads it back from.
        self.finish_runner.artifact_store = getattr(app.state.bootstrap, "artifact_store", None)
        self.finish_runner.namespace = self.tenant_id
        return app

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
        self.finish_runner = ArtifactEmittingRunner()
        await self.serve()

    async def shutdown(self) -> None:
        """Withdraw the candidate from service."""
        await self.stop()

    async def aclose(self) -> None:
        await self.stop()
        if self._previous_redis_mode is None:
            os.environ.pop("ZEROTH_REDIS__MODE", None)
        else:
            os.environ["ZEROTH_REDIS__MODE"] = self._previous_redis_mode
        if self._settings_saved:
            settings_module._settings_singleton = self._previous_settings
            self._settings_saved = False
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self.port = None
