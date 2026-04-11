"""Getting Started Section 3 — service mode with a human approval gate.

Boots Zeroth in-process as a FastAPI service (same wiring as the
``python -m zeroth.core.service.entrypoint`` command in the tutorial),
POSTs a run against a graph that contains a :class:`HumanApprovalNode`,
waits for it to pause on the approval gate, prints the exact ``curl``
command a human operator would run to approve it manually, and then
resolves the approval over HTTP so the script is end-to-end runnable.

SKIPs (exit 0) when ``OPENAI_API_KEY`` is unset so forked-PR CI without
secrets does not fail. The HTTP path exercises the real
``POST /deployments/{ref}/approvals/{approval_id}/resolve`` endpoint
defined in :mod:`zeroth.core.service.approval_api`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from pydantic import BaseModel


class DemoPayload(BaseModel):
    """Trivial contract model for both the agent input and the tool output."""

    message: str = ""


# Fixed API key used so the printed curl command is copy-pasteable.
_DEMO_API_KEY = "demo-operator-key"  # noqa: S105 — tutorial constant, not a real secret


class _LiteLLMAgentRunner:
    """Minimal agent runner: one litellm call, returns ``output_data``/``audit_record``."""

    def __init__(self, model: str, instruction: str) -> None:
        self._model = model
        self._instruction = instruction

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,
    ) -> SimpleNamespace:
        from litellm import completion

        user_text = input_payload.get("message", "") if isinstance(input_payload, dict) else ""
        response = completion(
            model=self._model,
            messages=[
                {"role": "system", "content": self._instruction},
                {"role": "user", "content": user_text or "Say hello from zeroth-core."},
            ],
        )
        return SimpleNamespace(
            output_data={"message": response["choices"][0]["message"]["content"]},
            audit_record={"provider": self._model, "thread_id": thread_id},
        )


class _EchoExecutableUnitRunner:
    """Stub executable unit runner that echoes the payload."""

    async def run(self, manifest_ref: str, input_payload: Any) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(
            output_data=dict(input_payload) if isinstance(input_payload, dict) else {},
            audit_record={"manifest_ref": manifest_ref},
        )


def _print_curl(deployment_ref: str, approval_id: str) -> None:
    """Print the equivalent curl command the docs show for manual approval."""
    print()
    print("# Equivalent curl command (Section 3 of the Getting Started tutorial):")
    base = "http://localhost:8000"
    url = f"{base}/deployments/{deployment_ref}/approvals/{approval_id}/resolve"
    print(
        f"curl -X POST {url} \\\n"
        f'     -H "X-API-Key: {_DEMO_API_KEY}" \\\n'
        f'     -H "Content-Type: application/json" \\\n'
        f'     -d \'{{"decision": "approve"}}\''
    )
    print()


async def _run_demo() -> int:
    from zeroth.core.contracts import ContractRegistry
    from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
    from zeroth.core.examples.quickstart import build_demo_graph
    from zeroth.core.graph import GraphRepository
    from zeroth.core.identity import ServiceRole
    from zeroth.core.service.app import create_app
    from zeroth.core.service.auth import ServiceAuthConfig, StaticApiKeyCredential
    from zeroth.core.service.bootstrap import bootstrap_service, run_migrations
    from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "approval_demo.sqlite")
        run_migrations(f"sqlite:///{db_path}")
        database = AsyncSQLiteDatabase(path=db_path)

        contract_registry = ContractRegistry(database)
        await contract_registry.register(DemoPayload, name="contract://demo-input")
        await contract_registry.register(DemoPayload, name="contract://demo-output")

        graph_repository = GraphRepository(database)
        graph = await graph_repository.create(build_demo_graph(include_approval=True))
        await graph_repository.publish(graph.graph_id, graph.version)

        deployment_service = DeploymentService(
            graph_repository=graph_repository,
            deployment_repository=SQLiteDeploymentRepository(database),
            contract_registry=contract_registry,
        )
        deployment = await deployment_service.deploy("demo-approval", graph.graph_id, graph.version)

        auth_config = ServiceAuthConfig(
            api_keys=[
                StaticApiKeyCredential(
                    credential_id="demo-operator",
                    secret=_DEMO_API_KEY,
                    subject="demo-operator",
                    roles=[ServiceRole.OPERATOR, ServiceRole.REVIEWER],
                    tenant_id="default",
                    workspace_id=None,
                )
            ]
        )

        service = await bootstrap_service(
            database,
            deployment_ref=deployment.deployment_ref,
            auth_config=auth_config,
            executable_unit_runner=_EchoExecutableUnitRunner(),
            enable_durable_worker=False,
        )
        service.orchestrator.agent_runners = {
            "agent": _LiteLLMAgentRunner(
                model="openai/gpt-4o-mini",
                instruction="You are a friendly assistant. Reply in one short sentence.",
            )
        }
        app = create_app(service)
        app.state.bootstrap = service

        headers = {"X-API-Key": _DEMO_API_KEY}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Submit a run — it will run the agent, pause on the approval node.
            response = await client.post(
                "/runs",
                headers=headers,
                json={"input_payload": {"message": "Say hello from zeroth-core."}},
            )
            response.raise_for_status()
            run_state = response.json()
            run_id = run_state["run_id"]
            print(f"Run {run_id} status: {run_state['status']}")

            # The in-process orchestrator drives the run synchronously on POST /runs,
            # so it should already be paused_for_approval with the approval_id attached.
            approval_paused = run_state.get("approval_paused_state") or {}
            approval_id = approval_paused.get("approval_id")
            if approval_id is None:
                # Fall back to listing pending approvals for this run.
                listing = await client.get(
                    f"/deployments/{deployment.deployment_ref}/approvals",
                    headers=headers,
                    params={"run_id": run_id},
                )
                listing.raise_for_status()
                approval_id = listing.json()[0]["approval_id"]

            _print_curl(deployment.deployment_ref, approval_id)

            # 2. Resolve the approval via the real HTTP endpoint the curl command targets.
            resolve = await client.post(
                f"/deployments/{deployment.deployment_ref}/approvals/{approval_id}/resolve",
                headers=headers,
                json={"decision": "approve"},
            )
            resolve.raise_for_status()
            resolved = resolve.json()
            print(f"Run {run_id} final status: {resolved['run']['status']}")
            if resolved["run"].get("terminal_output") is not None:
                print(f"Final output: {resolved['run']['terminal_output']}")
        return 0


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "SKIP: set OPENAI_API_KEY to run examples/approval_demo.py against a real LLM",
            file=sys.stderr,
        )
        return 0
    return asyncio.run(_run_demo())


if __name__ == "__main__":
    raise SystemExit(main())
