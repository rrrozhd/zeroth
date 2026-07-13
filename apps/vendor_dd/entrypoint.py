"""Production-shaped service entrypoint for a vendor-dd deployment.

One process serves one deployment (``ZEROTH_DEPLOYMENT_REF``; default
``vendor-dd``). The runbook starts two: the main workflow and the chat
deployment, sharing one SQLite database.

Wiring beyond the stock entrypoint:

* agent runners built declaratively from every deployed graph
  (``build_agent_runners`` — model_provider/tool_bindings/contracts honored);
* the executable-unit registry with the app's native/project/wrapped units;
* a PolicyGuard registering the ``policy://vendor-dd/sandboxed-units`` policy
  the unit nodes bind;
* the policy corpus ingested into the shared ``key_value`` memory connector
  (the retrieval node's backing store);
* bundled Regulus econ plane enabled (cost events + budget enforcement),
  pointed at this process's own ``/regulus`` mount.

Run
---
    uv run python -m apps.vendor_dd.entrypoint
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

PORT = int(os.environ.get("PORT", "8730"))
HOST = os.environ.get("HOST", "127.0.0.1")

os.environ.setdefault("ZEROTH_DATABASE__SQLITE_PATH", str(APP_DIR / "vendor_dd.sqlite"))
os.environ.setdefault("ZEROTH_REGULUS__ENABLED", "true")
os.environ.setdefault("ZEROTH_REGULUS__BASE_URL", f"http://{HOST}:{PORT}/regulus/v1")
# Short budget cache so cap changes take effect promptly in the runbook.
os.environ.setdefault("ZEROTH_REGULUS__BUDGET_CACHE_TTL", "2")
os.environ.setdefault("ECP_ALLOW_INSECURE_JWT_SECRET", "1")  # local dev only
os.environ.setdefault("ECP_DATABASE_URL", f"sqlite:///{APP_DIR / 'vendor_dd_econ.sqlite'}")
os.environ.setdefault("ZEROTH_WEBHOOK__ENABLED", "true")
os.environ.setdefault("ZEROTH_APPROVAL_SLA__ENABLED", "false")
os.environ.setdefault("ZEROTH_REDIS__MODE", "disabled")

import uvicorn

from apps.vendor_dd.fixtures.policy_corpus import POLICY_CORPUS
from apps.vendor_dd.graphs import (
    CHAT_DEPLOYMENT_REF,
    DIMENSION_DEPLOYMENT_REF,
    MAIN_DEPLOYMENT_REF,
    UNITS_POLICY_ID,
)
from apps.vendor_dd.providers import is_hermetic, select_provider
from apps.vendor_dd.units import build_unit_registry
from zeroth.core.agent_runtime.factory import build_agent_runners
from zeroth.core.config.settings import get_settings
from zeroth.core.contracts import ContractRegistry
from zeroth.core.deployments import SQLiteDeploymentRepository
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.governed.memory.models import MemoryScope
from zeroth.core.graph.serialization import deserialize_graph
from zeroth.core.identity import ServiceRole
from zeroth.core.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.core.service.app import create_app
from zeroth.core.service.auth import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.core.service.bootstrap import bootstrap_service
from zeroth.core.storage.factory import create_database

API_KEY = os.environ.get("VENDOR_DD_API_KEY", "vendor-dd-ops-key")
TENANT_ID = os.environ.get("VENDOR_DD_TENANT", "tenant-acme")

ALL_DEPLOYMENT_REFS = (MAIN_DEPLOYMENT_REF, DIMENSION_DEPLOYMENT_REF, CHAT_DEPLOYMENT_REF)


def build_policy_guard() -> PolicyGuard:
    """Allow-list policy for the sandboxed unit nodes."""
    capability_registry = CapabilityRegistry()
    for capability in Capability:
        capability_registry.register(capability.value, capability)
    policy_registry = PolicyRegistry()
    policy_registry.register(
        PolicyDefinition(
            policy_id=UNITS_POLICY_ID,
            allowed_capabilities=[Capability.PROCESS_SPAWN],
        )
    )
    return PolicyGuard(
        policy_registry=policy_registry,
        capability_registry=capability_registry,
    )


def build_auth_config() -> ServiceAuthConfig:
    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="vendor-dd-ops",
                secret=API_KEY,
                subject="vendor-dd-ops",
                roles=[ServiceRole.OPERATOR, ServiceRole.REVIEWER, ServiceRole.ADMIN],
                tenant_id=TENANT_ID,
                workspace_id=None,
            )
        ]
    )


async def build_app_async():
    settings = get_settings()
    database = await create_database(settings)
    deployment_ref = os.environ.get("ZEROTH_DEPLOYMENT_REF", MAIN_DEPLOYMENT_REF)

    # Declarative runner factory: one AgentRunner per agent node, for every
    # deployed graph in this database (main + dimension child + chat), so the
    # subgraph executor finds its child runners in the same orchestrator.
    provider = select_provider()
    contract_registry = ContractRegistry(database)
    deployment_repository = SQLiteDeploymentRepository(database)
    agent_runners = {}
    for ref in ALL_DEPLOYMENT_REFS:
        deployment = await deployment_repository.get(ref)
        if deployment is None:
            continue
        graph = deserialize_graph(deployment.serialized_graph)
        agent_runners.update(await build_agent_runners(graph, contract_registry, provider=provider))

    bootstrap = await bootstrap_service(
        database,
        deployment_ref=deployment_ref,
        agent_runners=agent_runners,
        executable_unit_runner=ExecutableUnitRunner(build_unit_registry()),
        auth_config=build_auth_config(),
        enable_durable_worker=True,
    )

    bootstrap.orchestrator.policy_guard = build_policy_guard()

    # Ingest the policy corpus into the shared key-value connector backing the
    # retrieval node. In-memory connector: ingest at bootstrap, every start.
    # The resolver's ScopedMemoryConnector maps SHARED reads to the
    # "__shared__" target bucket, so ingest into that bucket explicitly.
    _manifest, connector = bootstrap.memory_registry.list()["key_value"]
    for key, text in POLICY_CORPUS.items():
        await connector.write(key, text, MemoryScope.SHARED, target="__shared__")

    mode = "hermetic (scripted provider)" if is_hermetic() else "real LLM (LiteLLM)"
    print(
        f"vendor-dd entrypoint: deployment={deployment_ref} port={PORT} "
        f"tenant={TENANT_ID} runners={sorted(agent_runners)} mode={mode}"
    )
    return create_app(bootstrap)


async def _serve() -> None:
    # Bootstrap and uvicorn must share one event loop (the durable worker and
    # webhook delivery tasks bind to it) — same pattern as the stock
    # zeroth.core.service.entrypoint.
    app = await build_app_async()
    config = uvicorn.Config(app, host=HOST, port=PORT, proxy_headers=True)
    await uvicorn.Server(config).serve()


def main() -> int:
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
