"""Governance Walkthrough — approval gate, auditor, policy block.

A single runnable script that exercises the three Zeroth governance
primitives end-to-end against one in-process bootstrap:

1. **Approval gate.** A graph containing a :class:`HumanApprovalNode`
   pauses on the approval step; the example resolves it over the real
   ``POST /deployments/{ref}/approvals/{id}/resolve`` HTTP endpoint and
   observes the run complete.
2. **Auditor.** After the approval scenario succeeds, the example fetches
   ``GET /runs/{run_id}/timeline`` and prints each
   :class:`NodeAuditRecord`'s node id, status, and any policy metadata.
3. **Policy block.** A variant graph whose tool node declares a
   ``NETWORK_WRITE`` capability binding is deployed against a runtime
   wired with a :class:`PolicyGuard` and a :class:`PolicyDefinition` that
   denies ``NETWORK_WRITE``. A run against that deployment terminates
   with ``RunStatus.FAILED`` / reason ``policy_violation`` — the public
   API surfaces this as ``RunPublicStatus.TERMINATED_BY_POLICY``. The
   example then fetches ``GET /deployments/{ref}/audits?run_id=...`` to
   surface the denial audit record.

SKIPs (exit 0) when ``OPENAI_API_KEY`` is unset so forked-PR CI stays
green. Uses the same in-process ``httpx.ASGITransport`` pattern as
``examples/approval_demo.py``.
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
    """Trivial contract model used as both agent input and tool output."""

    message: str = ""


# Fixed key so the example is hermetic — tutorial constant, not a secret.
_DEMO_API_KEY = "demo-operator-key"  # noqa: S105


class _LiteLLMAgentRunner:
    """Minimal agent runner that calls litellm once and returns a payload."""

    def __init__(self, model: str, instruction: str) -> None:
        self._model = model
        self._instruction = instruction

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,  # noqa: ARG002
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


def _auth_config() -> Any:
    from zeroth.core.identity import ServiceRole
    from zeroth.core.service.auth import ServiceAuthConfig, StaticApiKeyCredential

    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="demo-operator",
                secret=_DEMO_API_KEY,
                subject="demo-operator",
                roles=[ServiceRole.OPERATOR, ServiceRole.REVIEWER, ServiceRole.AUDITOR],
                tenant_id="default",
                workspace_id=None,
            )
        ]
    )


async def _run_demo() -> int:
    from zeroth.core.contracts import ContractRegistry
    from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
    from zeroth.core.examples.quickstart import build_demo_graph, build_demo_graph_with_policy
    from zeroth.core.graph import GraphRepository
    from zeroth.core.policy import PolicyGuard
    from zeroth.core.policy.models import Capability, PolicyDefinition
    from zeroth.core.policy.registry import CapabilityRegistry, PolicyRegistry
    from zeroth.core.runs import RunStatus
    from zeroth.core.service.app import create_app
    from zeroth.core.service.bootstrap import bootstrap_service, run_migrations
    from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "governance_walkthrough.sqlite")
        run_migrations(f"sqlite:///{db_path}")
        database = AsyncSQLiteDatabase(path=db_path)

        contract_registry = ContractRegistry(database)
        await contract_registry.register(DemoPayload, name="contract://demo-input")
        await contract_registry.register(DemoPayload, name="contract://demo-output")

        graph_repository = GraphRepository(database)
        deployment_service = DeploymentService(
            graph_repository=graph_repository,
            deployment_repository=SQLiteDeploymentRepository(database),
            contract_registry=contract_registry,
        )

        # ── Scenario 1 + 2 bootstrap: approval-gated graph ─────────────
        approval_graph = await graph_repository.create(build_demo_graph(include_approval=True))
        await graph_repository.publish(approval_graph.graph_id, approval_graph.version)
        approval_deployment = await deployment_service.deploy(
            "demo-governance-approval",
            approval_graph.graph_id,
            approval_graph.version,
        )

        approval_service = await bootstrap_service(
            database,
            deployment_ref=approval_deployment.deployment_ref,
            auth_config=_auth_config(),
            executable_unit_runner=_EchoExecutableUnitRunner(),
            enable_durable_worker=False,
        )
        approval_service.orchestrator.agent_runners = {
            "agent": _LiteLLMAgentRunner(
                model="openai/gpt-4o-mini",
                instruction="You are a friendly assistant. Reply in one short sentence.",
            )
        }
        approval_app = create_app(approval_service)
        approval_app.state.bootstrap = approval_service

        headers = {"X-API-Key": _DEMO_API_KEY}
        transport = httpx.ASGITransport(app=approval_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # ── Scenario 1: approval gate ─────────────────────────────
            print("── Scenario 1 — Approval gate ─────────────────────────────")
            paused_run = await approval_service.orchestrator.run_graph(
                approval_service.graph,
                {"message": "Summarize the Zeroth governance demo in one sentence."},
                deployment_ref=approval_deployment.deployment_ref,
            )
            print(f"Run {paused_run.run_id} status after first drive: {paused_run.status.value}")
            assert paused_run.status is RunStatus.WAITING_APPROVAL, (
                f"expected WAITING_APPROVAL, got {paused_run.status}"
            )

            pending = await approval_service.approval_service.list_pending(
                run_id=paused_run.run_id,
                deployment_ref=approval_deployment.deployment_ref,
            )
            assert pending, "no pending approval produced by the approval node"
            approval_id = pending[0].approval_id
            print(f"Pending approval {approval_id} — resolving via HTTP…")

            resolve_url = (
                f"/deployments/{approval_deployment.deployment_ref}/approvals/{approval_id}/resolve"
            )
            resolve = await client.post(
                resolve_url,
                headers=headers,
                json={"decision": "approve"},
            )
            resolve.raise_for_status()
            resolved_run = resolve.json()["run"]
            print(
                f"Run {paused_run.run_id} final status: {resolved_run['status']} — "
                "approval gate cleared\n"
            )

            # ── Scenario 2: auditor reviews the trail ─────────────────
            print("── Scenario 2 — Auditor reviews the trail ─────────────────")
            timeline = await client.get(
                f"/runs/{paused_run.run_id}/timeline",
                headers=headers,
            )
            timeline.raise_for_status()
            entries = timeline.json()["entries"]
            print(f"GET /runs/{paused_run.run_id}/timeline → {len(entries)} audit records")
            for entry in entries:
                node_id = entry.get("node_id")
                entry_status = entry.get("status")
                metadata = entry.get("execution_metadata") or {}
                enforcement = metadata.get("enforcement")
                policy_note = ""
                if isinstance(enforcement, dict):
                    decision = enforcement.get("decision")
                    reason = enforcement.get("reason")
                    policy_note = f" — policy: {decision}"
                    if reason:
                        policy_note += f" ({reason})"
                print(f"  [{node_id}] status={entry_status}{policy_note}")
            print()

        # ── Scenario 3 bootstrap: policy-blocked graph ─────────────────
        print("── Scenario 3 — Policy block ──────────────────────────────")
        blocked_graph = await graph_repository.create(
            build_demo_graph_with_policy(denied_capabilities=[Capability.NETWORK_WRITE])
        )
        await graph_repository.publish(blocked_graph.graph_id, blocked_graph.version)
        blocked_deployment = await deployment_service.deploy(
            "demo-governance-blocked",
            blocked_graph.graph_id,
            blocked_graph.version,
        )

        blocked_service = await bootstrap_service(
            database,
            deployment_ref=blocked_deployment.deployment_ref,
            auth_config=_auth_config(),
            executable_unit_runner=_EchoExecutableUnitRunner(),
            enable_durable_worker=False,
        )
        blocked_service.orchestrator.agent_runners = {
            "agent": _LiteLLMAgentRunner(
                model="openai/gpt-4o-mini",
                instruction="You are a friendly assistant. Reply in one short sentence.",
            )
        }

        # Wire a PolicyGuard that denies NETWORK_WRITE for the tool node.
        # The quickstart helper sets capability_bindings to the raw Capability
        # values (e.g. "network_write"); register each value as its own ref.
        capability_registry = CapabilityRegistry()
        for cap in Capability:
            capability_registry.register(cap.value, cap)
        policy_registry = PolicyRegistry()
        policy_registry.register(
            PolicyDefinition(
                policy_id="block-demo-caps",
                denied_capabilities=[Capability.NETWORK_WRITE],
            )
        )
        blocked_service.orchestrator.policy_guard = PolicyGuard(
            policy_registry=policy_registry,
            capability_registry=capability_registry,
        )

        blocked_app = create_app(blocked_service)
        blocked_app.state.bootstrap = blocked_service
        transport2 = httpx.ASGITransport(app=blocked_app)
        async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client:
            blocked_run = await blocked_service.orchestrator.run_graph(
                blocked_service.graph,
                {"message": "This run should be blocked before the tool fires."},
                deployment_ref=blocked_deployment.deployment_ref,
            )
            print(f"Run {blocked_run.run_id} terminal status: {blocked_run.status.value}")
            if blocked_run.failure_state is not None:
                print(
                    "Failure reason: "
                    f"{blocked_run.failure_state.reason} — {blocked_run.failure_state.message}"
                )
            assert blocked_run.status is RunStatus.FAILED, (
                f"expected FAILED (policy_violation), got {blocked_run.status}"
            )
            assert (
                blocked_run.failure_state is not None
                and blocked_run.failure_state.reason == "policy_violation"
            ), "expected failure_state.reason == 'policy_violation'"

            audits = await client.get(
                f"/deployments/{blocked_deployment.deployment_ref}/audits",
                headers=headers,
                params={"run_id": blocked_run.run_id},
            )
            audits.raise_for_status()
            records = audits.json()["records"]
            print(f"GET /deployments/.../audits → {len(records)} audit records")
            denials = 0
            for rec in records:
                metadata = rec.get("execution_metadata") or {}
                enforcement = metadata.get("enforcement")
                if isinstance(enforcement, dict) and enforcement.get("decision") == "deny":
                    denials += 1
                    node_id = rec.get("node_id")
                    reason = enforcement.get("reason")
                    print(f"  DENIED at [{node_id}] — {reason}")
            assert denials >= 1, "expected at least one policy-denial audit record"
            print()

        print("All three governance scenarios passed.")
        return 0


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "SKIP: set OPENAI_API_KEY to run examples/governance_walkthrough.py against a real LLM",
            file=sys.stderr,
        )
        return 0
    return asyncio.run(_run_demo())


if __name__ == "__main__":
    raise SystemExit(main())
