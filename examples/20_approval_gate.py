"""20 — Human approval gate pausing a real run, resolved via the real HTTP API.

What this shows
---------------
A three-node graph: agent → :class:`HumanApprovalNode` → tool. The
orchestrator drives the agent, hits the approval node, and parks the
run with status ``WAITING_APPROVAL``. The example then:

1. Lists pending approvals via the real :class:`ApprovalService`.
2. Resolves the approval by calling the real HTTP endpoint
   (``POST /v1/deployments/{ref}/approvals/{id}/resolve``) against an
   in-process uvicorn server we boot just for this demo — so the curl
   command printed at the top is the *actual* command a human would
   run in another terminal.
3. Polls the run to completion after the approval clears.

The reason we stand up a real uvicorn instead of :class:`ASGITransport`
is that the HTTP flow — durable worker, approval resume, graph
continuation — is the whole point. A test-transport shortcut would
hide half of it.

Run
---
    uv run python examples/20_approval_gate.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import contextlib
import sys
from pathlib import Path

import httpx
import uvicorn

from examples._common import DEMO_API_KEY, demo_auth_config
from examples._contracts import ToolInput, ToolOutput, Topic
from examples._tools import build_demo_tool_registry
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.core.contracts import ContractRegistry
from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    GraphRepository,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.core.mappings.models import EdgeMapping, PassthroughMappingOperation
from zeroth.core.service.app import create_app
from zeroth.core.service.bootstrap import bootstrap_service, run_migrations
from zeroth.core.storage import AsyncSQLiteDatabase

DEPLOYMENT_REF = "approval-demo"
DB_PATH = Path("examples_approval.sqlite")
PORT = 8021


def build_graph() -> Graph:
    graph_id = "approval-demo"
    ref = f"{graph_id}@1"
    return Graph(
        graph_id=graph_id,
        name="Approval demo",
        version=1,
        entry_step="drafter",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="drafter",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Drafter"),
                input_contract_ref="contract://topic",
                output_contract_ref="contract://tool-input",
                agent=AgentNodeData(
                    instruction="Draft a short body. JSON only.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
            HumanApprovalNode(
                node_id="approval",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Human approval"),
                input_contract_ref="contract://tool-input",
                output_contract_ref="contract://tool-input",
                human_approval=HumanApprovalNodeData(
                    approval_policy_config={"allow_edits": True},
                ),
            ),
            ExecutableUnitNode(
                node_id="publisher",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Publisher"),
                input_contract_ref="contract://tool-input",
                output_contract_ref="contract://tool-output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://format_article",
                    execution_mode="native",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="drafter-to-approval",
                source_node_id="drafter",
                target_node_id="approval",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="topic", target_path="topic"),
                        PassthroughMappingOperation(source_path="body", target_path="body"),
                    ]
                ),
            ),
            Edge(
                edge_id="approval-to-publisher",
                source_node_id="approval",
                target_node_id="publisher",
            ),
        ],
    )


async def seed_and_build_app():
    if DB_PATH.exists():
        DB_PATH.unlink()
    run_migrations(f"sqlite:///{DB_PATH}")
    database = AsyncSQLiteDatabase(path=str(DB_PATH))

    contract_registry = ContractRegistry(database)
    await contract_registry.register(Topic, name="contract://topic")
    await contract_registry.register(ToolInput, name="contract://tool-input")
    await contract_registry.register(ToolOutput, name="contract://tool-output")

    graph_repository = GraphRepository(database)
    saved = await graph_repository.create(build_graph())
    await graph_repository.publish(saved.graph_id, saved.version)

    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(database),
        contract_registry=contract_registry,
    )
    await deployment_service.deploy(DEPLOYMENT_REF, saved.graph_id, saved.version)

    runner = AgentRunner(
        AgentConfig(
            name="drafter",
            description="Deterministic drafter for the approval demo.",
            instruction="Draft.",
            model_name="openai/gpt-4o-mini",
            input_model=Topic,
            output_model=ToolInput,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(
                    content={
                        "topic": "approvals",
                        "body": "Human approval gates pause runs mid-graph.",
                    }
                )
            ]
        ),
    )

    bootstrap = await bootstrap_service(
        database,
        deployment_ref=DEPLOYMENT_REF,
        agent_runners={"drafter": runner},
        executable_unit_runner=ExecutableUnitRunner(build_demo_tool_registry()),
        auth_config=demo_auth_config(),
        enable_durable_worker=True,
    )
    return create_app(bootstrap)


async def run_client(base_url: str) -> None:
    """Submit a run, poll until paused, print curl, resolve approval."""
    headers = {"X-API-Key": DEMO_API_KEY}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15.0) as client:
        create = await client.post(
            "/v1/runs",
            json={"input_payload": {"topic": "approvals"}},
        )
        create.raise_for_status()
        run_id = create.json()["run_id"]
        print(f"created run {run_id}")

        approval_id: str | None = None
        for _ in range(60):
            await asyncio.sleep(0.25)
            current = await client.get(f"/v1/runs/{run_id}")
            current.raise_for_status()
            body = current.json()
            if body["status"] == "paused_for_approval":
                approval_id = body["approval_paused_state"]["approval_id"]
                break
        assert approval_id is not None, "expected run to pause at the approval node"
        print(f"run paused at approval {approval_id}")

        # Equivalent curl a human operator would run in another terminal.
        print()
        print("# Equivalent curl command a human operator would run:")
        print(
            f"curl -X POST {base_url}/v1/deployments/{DEPLOYMENT_REF}/"
            f"approvals/{approval_id}/resolve \\\n"
            f'     -H "X-API-Key: {DEMO_API_KEY}" \\\n'
            f'     -H "Content-Type: application/json" \\\n'
            f"     -d '{{\"decision\": \"approve\"}}'"
        )
        print()

        resolve = await client.post(
            f"/v1/deployments/{DEPLOYMENT_REF}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
        )
        resolve.raise_for_status()
        final = resolve.json()["run"]
        print(f"run {run_id} final status: {final['status']}")
        if final.get("terminal_output") is not None:
            print(f"terminal output: {final['terminal_output']}")


async def main_async() -> int:
    app = await seed_and_build_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for uvicorn to be ready.
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)

    try:
        await run_client(f"http://127.0.0.1:{PORT}")
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        if DB_PATH.exists():
            DB_PATH.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
