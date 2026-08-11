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
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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
SHELL_GRAPH = "release.langgraph.shell_graph:graph"
POLICY_ID = "acceptance-assistant-allowlist"
ALLOWED_ASSISTANT = "shell"
_START_DEADLINE_SECONDS = 20.0
_AGENT_SERVER_DEADLINE_SECONDS = 60.0
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


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _last_error_line(errors: Path, interpreter: str) -> str:
    """Why a child process died, in one line, for a message that has to name a cause.

    A Python traceback ends with the exception, so the last non-empty line is the
    part worth reporting. Falls back to naming the interpreter that was used --
    a missing dependency is a property of *that* environment, and knowing which
    one it was is the whole diagnosis when the same tree runs under two.
    """
    try:
        lines = [line.strip() for line in errors.read_text(errors="replace").splitlines()]
    except OSError:  # pragma: no cover - the workdir was cleaned up under us
        lines = []
    reported = [line for line in lines if line]
    if not reported:
        return f"it wrote nothing to stderr (interpreter: {interpreter})"
    return f"{reported[-1]} (interpreter: {interpreter})"


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
        with_agent_server: bool = False,
    ) -> None:
        # Off by default: booting an Agent Server costs real seconds, and only the
        # scenarios that need something upstream to govern should pay for it.
        self.with_agent_server = with_agent_server
        self._agent_server: subprocess.Popen | None = None
        self._agent_server_port: int | None = None
        self.agent_server_url: str | None = None
        # The origin this candidate declares as its Regulus, known only once it has
        # bound a port. Exposed so a test can assert the declaration is served by a
        # real control plane rather than merely answered; see `_declare_bundled_regulus`.
        self.regulus_base_url: str | None = None
        self.deployment_ref = deployment_ref
        self.tenant_id = tenant_id
        self._db_path = workspace / "candidate.db"
        self._artifacts_dir = workspace / "artifacts"
        self._listener: socket.socket | None = None
        self._running: _Server | None = None
        self.port: int | None = None
        # A fresh counter per boot. The durable evidence is the audit record the
        # deployment itself publishes, not this process-local number; the counter
        # exists so an in-process test can cross-check what the API reports.
        self.finish_runner = ArtifactEmittingRunner()
        # Every environment key this candidate touches, with the value it had before.
        # Restoring one variable and forgetting the rest is how a candidate leaks its
        # configuration into the rest of the suite: enabling the gateway mounts a
        # catch-all route, so a leaked flag changes what unrelated tests are talking to.
        self._environment_before: dict[str, str | None] = {}
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
        # Restoring the exact object, not just clearing the cache: dropping the
        # singleton makes the next reader re-derive settings from whatever the
        # environment looks like then, which is not necessarily what the rest of the
        # suite started with. Putting the original back leaves no trace.
        if not self._settings_saved:
            self._previous_settings = settings_module._settings_singleton
            self._settings_saved = True
        self._set_environment(
            {
                "ZEROTH_REDIS__MODE": "disabled",
                # Own the artifact storage too. The candidate resets the settings
                # singleton so the service re-reads its configuration, and a store
                # left on its default writes .zeroth/ into the repository root, where
                # it outlives the session and trips the residue guard on whichever
                # unrelated test runs next.
                "ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR": str(self._artifacts_dir),
                # Same reasoning for the econ plane, which binds its database URL when
                # its module is first imported — and enabling the gateway is what
                # imports it, via the budget enforcer.
                "ECP_DATABASE_URL": f"sqlite+pysqlite:///{self._artifacts_dir}/econ_plane.db",
            }
        )

    def _declare_bundled_regulus(self) -> None:
        """Tell the candidate where its own Regulus actually is.

        `RegulusSettings.base_url` defaults to `http://localhost:8000/v1` — an
        *external* control-plane topology this candidate does not have. Left alone,
        `check_regulus` probes a port nothing is listening on, reports `unavailable`,
        and `determine_readiness_status` degrades the whole probe. The `readiness`
        scenario then fails for a dependency this deployment never deployed, and the
        `approvals` scenario, which polls readiness back to `ok` after its restart,
        fails with it.

        Worse, it passed anyway on any machine with something unrelated bound to port
        8000, because `check_regulus` asks only whether the origin answers. Declaring
        the real origin is what makes the result mean the same thing everywhere.

        The candidate runs the bundled plane in-process, mounted at `/regulus`, so its
        Regulus lives at its own origin. That is the topology the shipped reference app
        declares (`apps/vendor_dd/entrypoint.py`) and the deployment guide documents —
        a statement of fact about this deployment, not a relaxed assertion.
        """
        self.regulus_base_url = f"{self.base_url}/regulus/v1"
        self._set_environment({"ZEROTH_REGULUS__BASE_URL": self.regulus_base_url})

    def _start_agent_server(self) -> None:
        self._start_agent_server_on(_free_port())

    def _start_agent_server_on(self, port: int) -> None:
        """Run the real Agent Server on the shell application.

        The same recipe the conformance harness uses, which is green in CI on every
        push. Deliberately the real `langgraph_api` package rather than something that
        answers like it: the gateway fingerprints the upstream's OpenAPI document
        against a pinned hash, and a hand-written server can only pass that by being
        handed the answer.
        """
        self._agent_server_port = port
        environment = os.environ.copy()
        environment.update(
            {
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
                "LANGGRAPH_CLOUD_LICENSE_KEY": "",
                "PYTHONPATH": os.pathsep.join(
                    filter(None, (str(_REPO_ROOT), environment.get("PYTHONPATH")))
                ),
            }
        )
        script = (
            "from langgraph_api.cli import run_server; "
            f"run_server(host='127.0.0.1', port={port}, reload=False, "
            f"graphs={{'shell': '{SHELL_GRAPH}'}}, "
            "disable_persistence=True, open_browser=False, server_level='ERROR')"
        )
        # Its own cwd: the server writes state beside itself, and the repository root
        # has a residue tripwire that treats stray files as cross-test contamination.
        workdir = tempfile.mkdtemp(prefix="zeroth-agent-server-")
        # stderr to a file rather than DEVNULL. Discarded, the most common failure --
        # `langgraph_api` absent, because it is a `gateway-conformance` dev-group pin
        # and not in a venv built from the wheel -- surfaced as "the Agent Server
        # exited before serving", which names the symptom and hides the cause.
        errors = Path(workdir) / "agent-server.stderr"
        with errors.open("wb") as stream:
            self._agent_server = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=workdir,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=stream,
            )
        self.agent_server_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + _AGENT_SERVER_DEADLINE_SECONDS
        while True:
            if self._agent_server.poll() is not None:
                raise CandidateError(
                    "the Agent Server exited before serving: "
                    + _last_error_line(errors, sys.executable)
                )
            if time.monotonic() >= deadline:
                raise CandidateError(
                    f"Agent Server did not answer /ok within {_AGENT_SERVER_DEADLINE_SECONDS:g}s"
                )
            try:
                with urllib.request.urlopen(f"{self.agent_server_url}/ok", timeout=1):
                    break
            except OSError:
                time.sleep(0.25)

    def _declare_gateway(self) -> None:
        """Point the candidate's real gateway at the Agent Server we just started."""
        self._set_environment(
            {
                "ZEROTH_LANGGRAPH_GATEWAY__ENABLED": "true",
                "ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_URL": str(self.agent_server_url),
                "ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_AUDIENCE": "acceptance-shell",
                "ZEROTH_LANGGRAPH_GATEWAY__DEPLOYMENT_REF": self.deployment_ref,
                # A real policy, evaluated by the real guard. The allow-list omits the
                # -deny assistant, so the refusal comes from admission logic rather
                # than from anything this harness returns. There is no denied_assistants
                # field: admission denies by absence from an allow-list.
                "ZEROTH_LANGGRAPH_GATEWAY__POLICY_BINDINGS": json.dumps([POLICY_ID]),
                "ZEROTH_POLICY__DEFINITIONS": json.dumps(
                    [{"policy_id": POLICY_ID, "allowed_assistants": [ALLOWED_ASSISTANT]}]
                ),
                # Bootstrap refuses to build a gateway behind a NullSigner, and it is
                # right to: an unsigned deployment cannot attest anything it proxies.
                "SIGNING_DEPLOYMENT": secrets.token_hex(32),
            }
        )

    def _set_environment(self, values: dict[str, str]) -> None:
        """Apply environment overrides, remembering what to put back."""
        for key, value in values.items():
            self._environment_before.setdefault(key, os.environ.get(key))
            os.environ[key] = value
        settings_module._settings_singleton = None

    def _restore_environment(self) -> None:
        for key, previous in self._environment_before.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self._environment_before.clear()
        if self._settings_saved:
            settings_module._settings_singleton = self._previous_settings
            self._settings_saved = False

    async def provision(self) -> None:
        """Migrate the database and deploy the approval-gated graph exactly once."""
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._declare_no_redis()
        if self.with_agent_server:
            self._start_agent_server()
            self._declare_gateway()
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
        # After `_bind`: the declaration names this candidate's own origin, which does
        # not exist until a port is claimed. Before `serve`: `_set_environment` drops
        # the settings singleton, so the app built there reads the declared value.
        self._declare_bundled_regulus()

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
        listener = self._listener
        # Build the app on the loop that will serve it. Bootstrap creates loop-bound
        # objects — the gateway's HTTP client among them — so constructing here and
        # serving on the thread's loop makes the first proxied request fail with
        # "bound to a different event loop", which the gateway reports as a 503
        # misconfiguration. The blame lands on the product for a fault in the harness.
        started: dict[str, uvicorn.Server | BaseException] = {}

        def serve_forever() -> None:
            async def run() -> None:
                try:
                    app = await self._build_app()
                    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                    started["server"] = server
                except BaseException as error:  # noqa: BLE001 - reported to the caller
                    started["error"] = error
                    return
                await server.serve(sockets=[listener])

            asyncio.run(run())

        thread = threading.Thread(target=serve_forever, daemon=True)
        thread.start()
        server = await self._await_serving(started, thread)
        self._running = _Server(server=server, thread=thread)

    async def _await_serving(self, started: dict, thread: threading.Thread):
        """Block until the thread reports a serving server, or explain why not."""
        deadline = time.monotonic() + _START_DEADLINE_SECONDS
        while True:
            error = started.get("error")
            if error is not None:
                raise CandidateError(f"candidate failed to build: {error}") from error
            server = started.get("server")
            if server is not None and getattr(server, "started", False):
                return server
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise CandidateError(
                    f"candidate did not start within {_START_DEADLINE_SECONDS:g}s "
                    f"(thread alive: {thread.is_alive()})"
                )
            await asyncio.sleep(0.02)

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

    async def start_upstream(self) -> None:
        """Put the Agent Server back on the port the gateway is configured for."""
        if self._agent_server is not None:
            return
        self._start_agent_server_on(self._agent_server_port)

    async def stop_upstream(self) -> None:
        await self.stop_agent_server()

    async def stop_agent_server(self) -> None:
        """Take the upstream away, for real.

        The 502 the contract asserts is `zeroth.upstream_unavailable`, which the proxy
        raises when the transport cannot reach the Agent Server at all. A healthy server
        cannot produce it and no response body can fake it, so the only honest way to
        exercise that path is to genuinely remove the upstream and let the gateway
        discover it is gone.
        """
        if self._agent_server is None:
            raise CandidateError("no Agent Server is running")
        self._agent_server.terminate()
        try:
            self._agent_server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._agent_server.kill()
            self._agent_server.wait(timeout=10)
        self._agent_server = None

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
        if self._agent_server is not None:
            self._agent_server.terminate()
            try:
                self._agent_server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._agent_server.kill()
            self._agent_server = None
        self._restore_environment()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self.port = None
