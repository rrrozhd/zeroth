"""Local-only runtime boundaries for an explicitly authorized evaluation campaign.

Importing this module is inert.  Mutation and provider execution remain disabled
unless the caller supplies the corresponding environment gates.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import urlparse

import httpx

from zeroth.contracts.graph.models import Graph, GraphStatus
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.contracts.registry.errors import ContractNotFoundError
from zeroth.platform.storage import AsyncDatabase
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.service.deployments.repository import SQLiteDeploymentRepository
from zeroth.service.deployments.service import DeploymentService

from .campaign_execution import ContractSpec
from .campaign_http import PublishedGraph, provider_acknowledgement
from .evidence import EvidenceStore


def mutation_acknowledgement(campaign_id: str) -> str:
    """Return the exact phrase authorizing local campaign mutations."""
    return f"I_ACKNOWLEDGE_LOCAL_CAMPAIGN_MUTATIONS:{campaign_id}"


def _contract_identity(ref: str) -> tuple[str, int]:
    name, separator, raw_version = ref.rpartition("@")
    if not separator or not name or not raw_version.isdecimal() or int(raw_version) < 1:
        raise ValueError(f"contract reference must pin a positive version: {ref!r}")
    return name, int(raw_version)


def _authored_graph(graph: Graph) -> dict[str, object]:
    """Return immutable authored content, excluding lifecycle timestamps/status."""
    return graph.model_dump(
        mode="json",
        exclude={"status", "created_at", "updated_at"},
    )


class RepositoryTenantGraphPublisher:
    """Register exact tenant contracts and publish exact graph identities."""

    def __init__(self, database: AsyncDatabase) -> None:
        self.database = database
        # The validator is rebound to the exact tenant registry for each call.
        self.graph_repository = GraphRepository(database)
        self.deployment_repository = SQLiteDeploymentRepository(database)

    def contract_registry(self, tenant_id: str, workspace_id: str | None) -> ContractRegistry:
        return ContractRegistry.scoped(
            self.database,
            contract_scope_context(tenant_id, workspace_id),
        )

    def deployment_service(self, tenant_id: str, workspace_id: str | None) -> DeploymentService:
        """Expose the same scoped services used by a later deployment boundary."""
        registry = self.contract_registry(tenant_id, workspace_id)
        repository = GraphRepository(
            self.database,
            validator=GraphValidator(contract_registry=registry),
        )
        return DeploymentService(
            graph_repository=repository,
            deployment_repository=self.deployment_repository,
            contract_registry=registry,
        )

    async def publish_async(
        self,
        *,
        graphs: tuple[Graph, ...],
        contracts: tuple[ContractSpec, ...],
        tenant_id: str,
        workspace_id: str | None,
    ) -> tuple[PublishedGraph, ...]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        registry = self.contract_registry(tenant_id, workspace_id)
        for contract in contracts:
            name, version = _contract_identity(contract.ref)
            try:
                existing = await registry.get(name, version)
            except ContractNotFoundError:
                registered = await registry.register(
                    contract.model,
                    name=name,
                    version=version,
                )
                if (registered.name, registered.version) != (name, version):
                    raise RuntimeError("contract registry changed the requested identity") from None
            else:
                expected_model_path = f"{contract.model.__module__}:{contract.model.__qualname__}"
                if (
                    existing.model_path != expected_model_path
                    or existing.json_schema != contract.model.model_json_schema()
                ):
                    raise RuntimeError(f"existing contract {contract.ref} differs")

        repository = GraphRepository(
            self.database,
            validator=GraphValidator(contract_registry=registry),
        )
        # Keep the inspectable repository aligned with the actual validated one.
        self.graph_repository = repository
        published: list[PublishedGraph] = []
        seen: set[tuple[str, int]] = set()
        for graph in graphs:
            identity = (graph.graph_id, graph.version)
            if identity in seen:
                raise ValueError(f"duplicate graph identity: {graph.graph_id}@{graph.version}")
            seen.add(identity)
            if (graph.tenant_id, graph.workspace_id) != (tenant_id, workspace_id):
                raise ValueError("graph owner does not match requested tenant/workspace scope")
            existing = await repository.get(
                graph.graph_id,
                graph.version,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            if existing is None:
                if graph.status is not GraphStatus.DRAFT:
                    raise ValueError("new campaign graph must be a draft")
                await repository.create(
                    graph,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
                existing = graph
            elif _authored_graph(existing) != _authored_graph(graph):
                raise RuntimeError(
                    f"stored graph {graph.graph_id}@{graph.version} differs from campaign graph"
                )

            if existing.status is GraphStatus.DRAFT:
                existing = await repository.publish(
                    graph.graph_id,
                    graph.version,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
            elif existing.status is not GraphStatus.PUBLISHED:
                raise RuntimeError("campaign graph is not publishable")
            if (existing.graph_id, existing.version) != identity:
                raise RuntimeError("graph repository changed the requested identity")
            published.append(PublishedGraph(existing.graph_id, existing.version))
        return tuple(published)

    def publish(
        self,
        *,
        graphs: tuple[Graph, ...],
        contracts: tuple[ContractSpec, ...],
        tenant_id: str,
        workspace_id: str | None,
    ) -> tuple[PublishedGraph, ...]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.publish_async(
                    graphs=graphs,
                    contracts=contracts,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
            )
        raise RuntimeError("synchronous publisher cannot run inside an active event loop")


class _Process(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int | None: ...
    def communicate(self, timeout: float | None = None) -> tuple[str | None, str | None]: ...


@dataclass(slots=True)
class _OwnedProcess:
    process: _Process
    argv: tuple[str, ...]
    stdout_spool: IO[str]
    stderr_spool: IO[str]


class LocalDeploymentSupervisor:
    """Own only evaluation subprocesses it started and evidence their lifecycle."""

    def __init__(
        self,
        *,
        campaign_config: Path,
        evidence_store: EvidenceStore,
        environment: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        popen_factory: Any = subprocess.Popen,
        health_timeout_seconds: float = 20.0,
        health_poll_seconds: float = 0.1,
        process_timeout_seconds: float = 5.0,
        working_directory: Path | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self.campaign_config = campaign_config.resolve(strict=True)
        if not self.campaign_config.is_file():
            raise ValueError("campaign config must be a regular file")
        if health_timeout_seconds <= 0 or health_timeout_seconds > 60:
            raise ValueError("health wait must be bounded to 60 seconds")
        if health_poll_seconds < 0 or process_timeout_seconds <= 0:
            raise ValueError("process polling limits must be positive and bounded")
        self.evidence_store = evidence_store
        self.environment = dict(environment or {})
        self.client = client or httpx.Client()
        self.popen_factory = popen_factory
        self.health_timeout_seconds = health_timeout_seconds
        self.health_poll_seconds = health_poll_seconds
        self.process_timeout_seconds = process_timeout_seconds
        self.working_directory = (working_directory or Path.cwd()).resolve()
        self.workspace_id = workspace_id
        self._owned: dict[str, _OwnedProcess] = {}
        self._command_sequence = 0

    @staticmethod
    def _service_endpoint(service_url: str) -> tuple[str, int, str]:
        parsed = urlparse(service_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("deployment service must be an explicit loopback HTTP origin")
        return parsed.hostname, parsed.port, service_url.rstrip("/")

    def restart(self, *, deployment_ref: str, service_url: str) -> None:
        if not deployment_ref or any(character.isspace() for character in deployment_ref):
            raise ValueError("deployment_ref must be a non-empty token")
        host, port, origin = self._service_endpoint(service_url)
        if deployment_ref in self._owned:
            self.stop(deployment_ref)
        argv = (
            sys.executable,
            "-m",
            "release.live_evaluation.service",
            "--campaign-config",
            str(self.campaign_config),
            "--deployment-ref",
            deployment_ref,
            *(("--workspace-id", self.workspace_id) if self.workspace_id else ()),
            "--host",
            host,
            "--port",
            str(port),
        )
        secret_values = {
            value
            for key, value in self.environment.items()
            if value
            and any(token in key.lower() for token in ("secret", "token", "key", "password"))
        }
        if any(secret in argument for secret in secret_values for argument in argv):
            raise ValueError("secret material must never be passed on argv")
        # These handles intentionally span the child lifetime and close in stop().
        stdout_spool = tempfile.TemporaryFile(  # noqa: SIM115
            mode="w+t", encoding="utf-8"
        )
        stderr_spool = tempfile.TemporaryFile(  # noqa: SIM115
            mode="w+t", encoding="utf-8"
        )
        try:
            process = self.popen_factory(
                argv,
                cwd=self.working_directory,
                env={**os.environ, **self.environment},
                stdin=subprocess.DEVNULL,
                stdout=stdout_spool,
                stderr=stderr_spool,
                text=True,
                shell=False,
            )
        except BaseException:
            stdout_spool.close()
            stderr_spool.close()
            raise
        owned = _OwnedProcess(process, argv, stdout_spool, stderr_spool)
        self._owned[deployment_ref] = owned
        self.evidence_store.append_event(
            "deployment.process.started",
            {"deployment_ref": deployment_ref, "pid": process.pid, "port": port},
        )
        deadline = time.monotonic() + self.health_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("deployment service exited before becoming healthy")
                try:
                    response = self.client.get(
                        f"{origin}/health",
                        timeout=min(1.0, self.health_timeout_seconds),
                    )
                except httpx.HTTPError:
                    response = None
                if response is not None and response.status_code == 200:
                    payload = response.json()
                    if payload.get("deployment_ref") != deployment_ref:
                        raise RuntimeError("health response has the wrong deployment identity")
                    return
                time.sleep(self.health_poll_seconds)
            raise TimeoutError("deployment service did not become healthy before deadline")
        except BaseException:
            self.stop(deployment_ref)
            raise

    @staticmethod
    def _read_spool(spool: IO[str]) -> str:
        spool.flush()
        spool.seek(0)
        return spool.read()

    def stop(self, deployment_ref: str) -> None:
        try:
            owned = self._owned[deployment_ref]
        except KeyError:
            raise KeyError(f"deployment process is not owned: {deployment_ref}") from None
        process = owned.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.process_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                kill = getattr(process, "kill", None)
                if kill is None:
                    raise RuntimeError(
                        "owned process did not terminate and cannot be killed"
                    ) from exc
                kill()
                process.wait(timeout=self.process_timeout_seconds)
        communicated_stdout, communicated_stderr = process.communicate(
            timeout=self.process_timeout_seconds
        )
        stdout = communicated_stdout or self._read_spool(owned.stdout_spool)
        stderr = communicated_stderr or self._read_spool(owned.stderr_spool)
        owned.stdout_spool.close()
        owned.stderr_spool.close()
        self._command_sequence += 1
        safe_ref = "".join(
            character if character.isalnum() or character in "_-" else "-"
            for character in deployment_ref.lower()
        ).strip("-")
        self.evidence_store.record_command(
            sequence=self._command_sequence,
            name=f"service-{safe_ref or 'deployment'}",
            argv=owned.argv,
            working_directory=self.working_directory,
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
        )
        del self._owned[deployment_ref]

    def stop_all(self) -> None:
        """Stop every process owned by this supervisor, preserving first failure."""
        first_error: BaseException | None = None
        for deployment_ref in tuple(self._owned):
            try:
                self.stop(deployment_ref)
            except BaseException as exc:  # cleanup must continue for the remaining children
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise RuntimeError(
                "one or more owned deployment processes failed to stop"
            ) from first_error


@dataclass(frozen=True, slots=True)
class LiveExecutionOptions:
    campaign_id: str
    artifact_root: Path
    action_sink_root: Path
    allow_mutation: bool = False
    mutation_acknowledgement: str | None = None
    allow_provider: bool = False
    paid_acknowledgement: str | None = None


def _env_flag(environ: Mapping[str, str], name: str) -> bool:
    raw = environ.get(name, "false")
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be exactly 'true' or 'false'")
    return raw == "true"


def _external_directory(raw: str | None, *, name: str, repository_root: Path) -> Path:
    if raw is None:
        raise ValueError(f"{name} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute external path")
    resolved = path.resolve()
    repository = repository_root.resolve()
    if resolved == repository or repository in resolved.parents:
        raise ValueError(f"{name} must remain outside the repository")
    return resolved


def load_live_execution_options(
    environ: Mapping[str, str], *, repository_root: Path
) -> LiveExecutionOptions:
    """Load explicit gates; absent gates are read-only and provider-disabled."""
    campaign_id = environ.get("ZEROTH_EVALUATION_CAMPAIGN_ID")
    if not campaign_id:
        raise ValueError("ZEROTH_EVALUATION_CAMPAIGN_ID is required")
    artifact_root = _external_directory(
        environ.get("ZEROTH_EVALUATION_ARTIFACT_ROOT"),
        name="artifact root",
        repository_root=repository_root,
    )
    action_sink_root = _external_directory(
        environ.get("ZEROTH_EVALUATION_ACTION_SINK_ROOT"),
        name="action sink root",
        repository_root=repository_root,
    )
    if artifact_root != action_sink_root and artifact_root not in action_sink_root.parents:
        raise ValueError("action sink root must be campaign-scoped under artifact root")
    allow_mutation = _env_flag(environ, "ZEROTH_EVALUATION_ALLOW_MUTATION")
    allow_provider = _env_flag(environ, "ZEROTH_EVALUATION_ALLOW_PROVIDER")
    acknowledgement = environ.get("ZEROTH_EVALUATION_PAID_ACK")
    mutation_ack = environ.get("ZEROTH_EVALUATION_MUTATION_ACK")
    if allow_mutation and mutation_ack != mutation_acknowledgement(campaign_id):
        raise ValueError("mutation execution requires the exact campaign acknowledgement")
    if allow_provider and acknowledgement != provider_acknowledgement(campaign_id):
        raise ValueError("provider execution requires the exact campaign acknowledgement")
    if allow_provider and not allow_mutation:
        raise ValueError("provider execution requires mutation enablement")
    return LiveExecutionOptions(
        campaign_id=campaign_id,
        artifact_root=artifact_root,
        action_sink_root=action_sink_root,
        allow_mutation=allow_mutation,
        mutation_acknowledgement=mutation_ack,
        allow_provider=allow_provider,
        paid_acknowledgement=acknowledgement,
    )


def build_live_execution_parser() -> argparse.ArgumentParser:
    """Build an inert configuration parser; it never executes a campaign."""
    parser = argparse.ArgumentParser(prog="zeroth-live-evaluation")
    parser.add_argument("--campaign-id")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--action-sink-root", type=Path)
    parser.add_argument("--allow-mutation", action="store_true", default=None)
    parser.add_argument("--mutation-ack")
    parser.add_argument("--allow-provider", action="store_true", default=None)
    parser.add_argument("--paid-ack")
    return parser


def load_live_execution_cli(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    repository_root: Path,
) -> LiveExecutionOptions:
    """Merge explicit CLI values over environment values and apply one gate path."""
    args = build_live_execution_parser().parse_args(list(argv))
    merged = dict(environ)
    scalar_values = {
        "ZEROTH_EVALUATION_CAMPAIGN_ID": args.campaign_id,
        "ZEROTH_EVALUATION_ARTIFACT_ROOT": (
            str(args.artifact_root) if args.artifact_root is not None else None
        ),
        "ZEROTH_EVALUATION_ACTION_SINK_ROOT": (
            str(args.action_sink_root) if args.action_sink_root is not None else None
        ),
        "ZEROTH_EVALUATION_PAID_ACK": args.paid_ack,
        "ZEROTH_EVALUATION_MUTATION_ACK": args.mutation_ack,
    }
    for name, value in scalar_values.items():
        if value is not None:
            merged[name] = value
    if args.allow_mutation is not None:
        merged["ZEROTH_EVALUATION_ALLOW_MUTATION"] = "true" if args.allow_mutation else "false"
    if args.allow_provider is not None:
        merged["ZEROTH_EVALUATION_ALLOW_PROVIDER"] = "true" if args.allow_provider else "false"
    return load_live_execution_options(merged, repository_root=repository_root)
