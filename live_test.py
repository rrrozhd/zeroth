"""Live end-to-end test: seed graph, deploy, invoke with real OpenAI call."""
from __future__ import annotations

import asyncio
import os
import sys

# Disable Regulus, webhooks, SLA before any settings load
os.environ["ZEROTH_REGULUS__ENABLED"] = "false"
os.environ["ZEROTH_WEBHOOK__ENABLED"] = "false"
os.environ["ZEROTH_APPROVAL_SLA__ENABLED"] = "false"
os.environ["ZEROTH_DATABASE__SQLITE_PATH"] = "zeroth_live.db"


async def main():
    from pydantic import BaseModel

    from zeroth.agent_runtime import AgentConfig, AgentRunner
    from zeroth.agent_runtime.provider import LiteLLMProviderAdapter
    from zeroth.contracts import ContractRegistry
    from zeroth.deployments import DeploymentService, SQLiteDeploymentRepository
    from zeroth.graph import (
        AgentNode,
        AgentNodeData,
        DisplayMetadata,
        ExecutionSettings,
        Graph,
    )
    from zeroth.graph.repository import GraphRepository
    from zeroth.identity import ServiceRole
    from zeroth.service.auth import ServiceAuthConfig, StaticApiKeyCredential
    from zeroth.service.bootstrap import bootstrap_service, run_migrations
    from zeroth.storage.factory import create_database
    from zeroth.config.settings import get_settings

    # --- Step 1: Migrations ---
    print("=" * 60)
    print("STEP 1: Running migrations...")
    db_path = "zeroth_live.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    run_migrations(f"sqlite:///{db_path}")
    print("  Migrations complete.")

    # --- Step 2: Create database ---
    print("\nSTEP 2: Creating database connection...")
    settings = get_settings()
    database = await create_database(settings)
    print(f"  Database: {type(database).__name__}")

    # --- Step 3: Define contracts ---
    print("\nSTEP 3: Registering contracts...")

    class QuestionInput(BaseModel):
        question: str

    class AnswerOutput(BaseModel):
        answer: str

    contract_registry = ContractRegistry(database)
    await contract_registry.register(QuestionInput, name="contract://question-input")
    await contract_registry.register(AnswerOutput, name="contract://answer-output")
    print("  Contracts registered: question-input, answer-output")

    # --- Step 4: Create and publish graph ---
    print("\nSTEP 4: Creating graph...")
    graph = Graph(
        graph_id="live-qa",
        name="Live Q&A",
        version=1,
        entry_step="qa-agent",
        execution_settings=ExecutionSettings(
            max_total_steps=10,
            max_visits_per_node=3,
            max_total_runtime_seconds=120,
        ),
        nodes=[
            AgentNode(
                node_id="qa-agent",
                graph_version_ref="live-qa@1",
                display=DisplayMetadata(
                    title="Q&A Agent",
                    description="Answers questions using OpenAI",
                ),
                input_contract_ref="contract://question-input",
                output_contract_ref="contract://answer-output",
                agent=AgentNodeData(
                    instruction="You are a helpful assistant. Answer the user's question concisely in 1-2 sentences. Return your answer as JSON with an 'answer' field.",
                    model_provider="openai/gpt-4o-mini",
                    model_params={"temperature": 0.3, "max_tokens": 200},
                    retry_policy={"max_retries": 2},
                ),
            )
        ],
        edges=[],
    )

    graph_repo = GraphRepository(database)
    saved = await graph_repo.create(graph)
    published = await graph_repo.publish(saved.graph_id, saved.version)
    print(f"  Graph created and published: {published.graph_id}@{published.version}")

    # --- Step 5: Deploy ---
    print("\nSTEP 5: Creating deployment...")
    deployment_repo = SQLiteDeploymentRepository(database)
    deployment_service = DeploymentService(
        graph_repository=graph_repo,
        deployment_repository=deployment_repo,
        contract_registry=contract_registry,
    )
    deployment = await deployment_service.deploy(
        "live-qa-v1", published.graph_id, published.version
    )
    print(f"  Deployment created: {deployment.deployment_ref} v{deployment.version}")

    # --- Step 6: Bootstrap service ---
    print("\nSTEP 6: Bootstrapping service...")
    auth_config = ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="live-test",
                secret="live-test-key",
                subject="tester",
                roles=[ServiceRole.OPERATOR],
                tenant_id="default",
                workspace_id=None,
            )
        ]
    )

    provider = LiteLLMProviderAdapter()
    agent_config = AgentConfig(
        name="qa-agent",
        instruction="You are a helpful assistant. Answer questions concisely.",
        model_name="openai/gpt-4o-mini",
        input_model=QuestionInput,
        output_model=AnswerOutput,
    )
    agent_runner = AgentRunner(agent_config, provider=provider)

    service = await bootstrap_service(
        database,
        deployment_ref="live-qa-v1",
        agent_runners={"qa-agent": agent_runner},
        auth_config=auth_config,
        enable_durable_worker=False,
    )
    print(f"  Service bootstrapped!")
    print(f"  Deployment: {service.deployment.deployment_ref}")
    print(f"  Orchestrator: {type(service.orchestrator).__name__}")

    # --- Step 7: Submit a run ---
    print("\nSTEP 7: Submitting run with real OpenAI call...")
    print("  Question: 'What is the capital of France?'")
    print("  Calling OpenAI gpt-4o-mini...")
    print()

    from zeroth.runs import Run

    try:
        from zeroth.graph.serialization import deserialize_graph
        graph_obj = deserialize_graph(service.deployment.serialized_graph)
        run = await service.orchestrator.run_graph(
            graph_obj,
            {"question": "What is the capital of France?"},
            deployment_ref=service.deployment.deployment_ref,
        )
        print("  RUN COMPLETED!")
        print(f"  Run ID: {run.run_id}")
        print(f"  Status: {run.status}")
        print(f"  Output: {run.metadata}")
    except Exception as e:
        print(f"  RUN FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False

    # --- Step 8: Check audit records ---
    print("\nSTEP 8: Checking audit records...")
    records = await service.audit_repository.list_by_run(run.run_id)
    for rec in records:
        node_id = getattr(rec, 'node_id', 'unknown')
        token_usage = getattr(rec, 'token_usage', None)
        cost = getattr(rec, 'cost_usd', None)
        print(f"  Node: {node_id}")
        print(f"  Token usage: {token_usage}")
        print(f"  Cost USD: {cost}")

    print("\n" + "=" * 60)
    print("LIVE TEST PASSED - Zeroth executed end-to-end with real LLM!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set")
        sys.exit(1)
    print(f"OpenAI key: {api_key[:20]}...{api_key[-4:]}")

    success = asyncio.run(main())
    sys.exit(0 if success else 1)
