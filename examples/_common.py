"""Shared bootstrap helpers for the examples.

The examples all need to do the same dozen-line dance: spin up an
in-memory SQLite database, run migrations, register contracts, persist a
graph, deploy it, and bootstrap the service. That dance is plumbing, not
the point of any given example — so it lives here.

Every public helper in this module is a thin wrapper around the real
library API (:mod:`zeroth.core.service.bootstrap`,
:mod:`zeroth.core.graph`, :mod:`zeroth.core.contracts`). There are no
stubs, no fake runners, no shortcuts. Read this file once and you'll
know exactly what every ``00_…`` through ``33_…`` example is doing
under the hood.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentRunner
from zeroth.core.contracts import ContractRegistry
from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import Graph, GraphRepository
from zeroth.core.identity import ServiceRole
from zeroth.core.service.auth import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.core.service.bootstrap import (
    ServiceBootstrap,
    bootstrap_service,
    run_migrations,
)
from zeroth.core.storage import AsyncSQLiteDatabase

# ---------------------------------------------------------------------------
# Env guards so the whole examples suite runs hermetically in CI.
# ---------------------------------------------------------------------------
#
# Webhooks, Regulus cost tracking, and the approval-SLA checker all spin up
# real background tasks (and want Redis / HTTP endpoints) when enabled. None
# of the examples need them by default, and setting them before ``get_settings``
# is first called keeps the bootstrap deterministic.
for _var in (
    "ZEROTH_REGULUS__ENABLED",
    "ZEROTH_WEBHOOK__ENABLED",
    "ZEROTH_APPROVAL_SLA__ENABLED",
):
    os.environ.setdefault(_var, "false")
os.environ.setdefault("ZEROTH_REDIS__MODE", "disabled")


DEMO_API_KEY = "demo-operator-key"  # noqa: S105 — deterministic tutorial key
DEMO_DEPLOYMENT_REF = "examples-demo"
DEMO_GRAPH_ID = "examples-demo"


def require_env(*names: str) -> bool:
    """Print a SKIP and return False when any required env var is missing.

    Used at the top of examples that need a real LLM key to run. Exits
    cleanly with status 0 so forked-PR CI without secrets stays green.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print(
            f"SKIP: set {', '.join(missing)} to run this example against a real LLM",
            file=sys.stderr,
        )
        return False
    return True


def demo_auth_config(*, extra_roles: list[ServiceRole] | None = None) -> ServiceAuthConfig:
    """Build a one-API-key auth config for the examples' HTTP flows."""
    roles = [ServiceRole.OPERATOR, ServiceRole.REVIEWER]
    if extra_roles:
        for role in extra_roles:
            if role not in roles:
                roles.append(role)
    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="examples-demo",
                secret=DEMO_API_KEY,
                subject="examples-demo",
                roles=roles,
                tenant_id="default",
                workspace_id=None,
            )
        ]
    )


@dataclass(slots=True)
class DemoService:
    """Return value of :func:`bootstrap_examples_service`.

    Bundles the :class:`ServiceBootstrap` with the on-disk SQLite path so
    callers that care (e.g. the HTTP examples) can hand the same file to a
    follow-up client.
    """

    service: ServiceBootstrap
    database: AsyncSQLiteDatabase
    db_path: Path
    deployment_ref: str


async def register_contracts(
    contract_registry: ContractRegistry,
    contracts: Mapping[str, type[BaseModel]],
) -> None:
    """Register every ``name -> model`` entry against the contract registry."""
    for name, model in contracts.items():
        await contract_registry.register(model, name=name)


async def bootstrap_examples_service(
    graph: Graph,
    *,
    contracts: Mapping[str, type[BaseModel]],
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    deployment_ref: str = DEMO_DEPLOYMENT_REF,
    db_path: Path | None = None,
    auth_config: ServiceAuthConfig | None = None,
    enable_durable_worker: bool = False,
) -> DemoService:
    """Run migrations, register contracts, publish the graph, deploy it, and bootstrap.

    This replaces the ~40 lines of boilerplate the old examples each
    re-invented. The full chain is:

    1. Migrate a fresh SQLite database (provided or a temp file).
    2. Register every contract in ``contracts`` against a
       :class:`ContractRegistry`.
    3. Persist the graph via :class:`GraphRepository`, publish it, and
       create a :class:`Deployment` for ``deployment_ref``.
    4. Call :func:`bootstrap_service` with the supplied agent runners and
       executable-unit runner.

    The returned :class:`DemoService` exposes the real
    :class:`ServiceBootstrap` — examples drive it the same way production
    code would.
    """
    db_path = Path(db_path) if db_path is not None else Path(
        tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
    )
    run_migrations(f"sqlite:///{db_path}")

    database = AsyncSQLiteDatabase(path=str(db_path))

    contract_registry = ContractRegistry(database)
    await register_contracts(contract_registry, contracts)

    graph_repository = GraphRepository(database)
    saved = await graph_repository.create(graph)
    await graph_repository.publish(saved.graph_id, saved.version)

    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(database),
        contract_registry=contract_registry,
    )
    await deployment_service.deploy(deployment_ref, saved.graph_id, saved.version)

    service = await bootstrap_service(
        database,
        deployment_ref=deployment_ref,
        agent_runners=dict(agent_runners or {}),
        executable_unit_runner=executable_unit_runner,
        auth_config=auth_config or demo_auth_config(),
        enable_durable_worker=enable_durable_worker,
    )

    return DemoService(
        service=service,
        database=database,
        db_path=db_path,
        deployment_ref=deployment_ref,
    )


@asynccontextmanager
async def running_service(
    graph: Graph,
    *,
    contracts: Mapping[str, type[BaseModel]],
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    deployment_ref: str = DEMO_DEPLOYMENT_REF,
    db_path: Path | None = None,
    auth_config: ServiceAuthConfig | None = None,
    enable_durable_worker: bool = False,
):
    """Async context manager wrapper around :func:`bootstrap_examples_service`.

    Yields a :class:`DemoService` and cleans up the temp SQLite file on
    exit. Use this when you want "get me a wired-up service, run some
    stuff, throw it away" in four lines of code.
    """
    demo = await bootstrap_examples_service(
        graph,
        contracts=contracts,
        agent_runners=agent_runners,
        executable_unit_runner=executable_unit_runner,
        deployment_ref=deployment_ref,
        db_path=db_path,
        auth_config=auth_config,
        enable_durable_worker=enable_durable_worker,
    )
    try:
        yield demo
    finally:
        # Best-effort cleanup — tests use a temp file we own.
        try:
            demo.db_path.unlink(missing_ok=True)
        except Exception:
            pass


def print_run_summary(run: Any, *, label: str = "run") -> None:
    """Pretty-print a :class:`Run` so the examples have a consistent ending."""
    print(f"\n── {label} summary ─────────────────────────────")
    print(f"  run_id : {run.run_id}")
    print(f"  status : {getattr(run.status, 'value', run.status)}")
    if getattr(run, "final_output", None) is not None:
        print(f"  output : {run.final_output}")
    if getattr(run, "failure_state", None) is not None:
        print(f"  failure: {run.failure_state.reason} — {run.failure_state.message}")
