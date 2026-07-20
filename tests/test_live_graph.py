"""Live graph-orchestration tests: every variant driven through ``run_graph``.

These call the real OpenAI API and are gated behind ``@pytest.mark.live``; they
only run when ``OPENAI_API_KEY`` is present.  Skip in CI: ``pytest -m "not live"``.
Run live: ``pytest -m live``.

``tests/test_live_llm.py`` proves the provider + a standalone ``AgentRunner``.
This module proves the *orchestrator*: each test wires a real ``Graph`` and drives
it through ``RuntimeOrchestrator.run_graph`` against ``gpt-4o-mini``, upgrading an
orchestration variant from "validated only with fake providers" to "validated
against a live model" -- sequential, conditional, cyclic, subgraph, agent+code
interop, human-approval pause/resume, parallel fan-out/fan-in, and retrieval->agent.

Determinism strategy (a live suite must still be reliable):

* Live agents use structured output and are asked deterministic-enough questions
  (capital of a country, "is red warm", extract an integer) at temperature 0.
* Deterministic ``ExecutableUnit``/connector nodes isolate the single live variable
  per test, so assertions key off run status, routing, step counts, and data flow
  rather than exact free-text.
* Every *live* output model keeps all fields required (no defaults): OpenAI strict
  structured output rejects schemas that omit any property from ``required`` -- the
  same fault that bit ``JudgeVerdict.rationale``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from zeroth.core.governed.memory.models import MemoryEntry, MemoryScope
from pydantic import BaseModel

from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.models import ModelParams
from zeroth.runtime.agents.provider import (
    CallableProviderAdapter,
    LiteLLMProviderAdapter,
    ProviderResponse,
)
from zeroth.governance.approvals import ApprovalDecision, ApprovalRepository, ApprovalService
from zeroth.governance.audit import AuditRepository
from zeroth.core.deployments.models import Deployment
from zeroth.integrations.execution import (
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
)
from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
    RetrievalNode,
    RetrievalNodeData,
)
from zeroth.contracts.graph.models import SubgraphNode
from zeroth.contracts.graph.serialization import serialize_graph
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.runtime.parallel.models import ParallelConfig
from zeroth.core.runs import RunRepository, RunStatus
from zeroth.runtime.subgraphs.executor import SubgraphExecutor
from zeroth.runtime.subgraphs.models import SubgraphNodeData
from zeroth.runtime.subgraphs.resolver import SubgraphResolver

pytestmark = pytest.mark.live

MODEL = "openai/gpt-4o-mini"

requires_openai = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set"
)


# --------------------------------------------------------------------------- #
# Shared input/output contracts (live output models: all fields REQUIRED)
# --------------------------------------------------------------------------- #


class CountryIn(BaseModel):
    country: str


class CityOut(BaseModel):
    city: str


class CityIn(BaseModel):
    city: str


class CountryOut(BaseModel):
    country: str


class TextIn(BaseModel):
    text: str


class Route(BaseModel):
    route: str


class Label(BaseModel):
    label: str


class NumberIn(BaseModel):
    value: int


class NumberOut(BaseModel):
    value: int


class WordIn(BaseModel):
    word: str


class ShoutOut(BaseModel):
    shout: str


class ColorIn(BaseModel):
    color: str


class WarmOut(BaseModel):
    warm: bool


class SeedIn(BaseModel):
    seed: int = 0


class ItemsOut(BaseModel):
    items: list[dict[str, Any]]


class QuestionContextIn(BaseModel):
    question: str
    context: list[dict[str, Any]]


class AnswerOut(BaseModel):
    answer: str


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _live_runner(
    name: str,
    instruction: str,
    *,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
) -> AgentRunner:
    """An ``AgentRunner`` backed by the real OpenAI provider at temperature 0."""
    return AgentRunner(
        AgentConfig(
            name=name,
            instruction=instruction,
            model_name=MODEL,
            input_model=input_model,
            output_model=output_model,
            model_params=ModelParams(temperature=0.0),
        ),
        LiteLLMProviderAdapter(),
    )


def _agent_node(
    node_id: str, graph_id: str, *, parallel_config: ParallelConfig | None = None
) -> AgentNode:
    """An ``AgentNode`` whose runner is resolved by ``node_id`` at dispatch."""
    return AgentNode(
        node_id=node_id,
        graph_version_ref=f"{graph_id}:v1",
        agent=AgentNodeData(instruction="see runner", model_provider=MODEL),
        parallel_config=parallel_config,
    )


def _native_manifest(unit_id: str) -> NativeUnitManifest:
    """A native (in-process) executable-unit manifest for deterministic nodes."""
    ref = f"demo.{unit_id}:handler"
    return NativeUnitManifest(
        unit_id=unit_id,
        onboarding_mode=ExecutionMode.NATIVE,
        runtime="python",
        artifact_source=PythonModuleArtifactSource(ref=ref),
        callable_ref=ref,
        entrypoint_type="python_callable",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        cache_identity_fields={"python": "3.12"},
    )


def _unit_node(node_id: str, graph_id: str, manifest_ref: str) -> ExecutableUnitNode:
    """An ``ExecutableUnitNode`` bound to a registered native unit."""
    return ExecutableUnitNode(
        node_id=node_id,
        graph_version_ref=f"{graph_id}:v1",
        executable_unit=ExecutableUnitNodeData(manifest_ref=manifest_ref, execution_mode="native"),
    )


class _RecordingConnector:
    """In-memory connector returning canned entries (no real similarity search)."""

    def __init__(self, entries: list[MemoryEntry]) -> None:
        self._entries = entries
        self.search_calls: list[dict] = []

    async def search(self, query, scope, *, target=None):  # noqa: ANN001
        """Record the query and return the canned entries verbatim."""
        self.search_calls.append(dict(query))
        return list(self._entries)

    async def write(self, key, value, scope, *, target=None):  # noqa: ANN001
        """No-op (protocol completeness)."""

    async def read(self, key, scope, *, target=None):  # noqa: ANN001
        """No-op (protocol completeness)."""
        return None

    async def delete(self, key, scope, *, target=None):  # noqa: ANN001
        """No-op (protocol completeness)."""


# --------------------------------------------------------------------------- #
# 1. Sequential multi-agent graph (A -> B), live data flow
# --------------------------------------------------------------------------- #


@requires_openai
async def test_live_sequential_two_agents(sqlite_db) -> None:
    """A live agent's output is consumed by a second live agent via ``run_graph``."""
    a = _live_runner(
        "capital",
        "The input has a 'country'. Return ONLY that country's capital city in 'city'.",
        input_model=CountryIn,
        output_model=CityOut,
    )
    b = _live_runner(
        "country",
        "The input has a 'city'. Return ONLY the country it is the capital of in 'country'.",
        input_model=CityIn,
        output_model=CountryOut,
    )
    graph = Graph(
        graph_id="live-seq",
        name="sequential",
        entry_step="a",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[_agent_node("a", "live-seq"), _agent_node("b", "live-seq")],
        edges=[Edge(edge_id="ab", source_node_id="a", target_node_id="b")],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
        agent_runners={"a": a, "b": b},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {"country": "France"})

    assert run.status is RunStatus.COMPLETED
    assert [e.node_id for e in run.execution_history] == ["a", "b"]
    # France -> Paris -> France: the second agent only sees the first agent's output.
    assert "france" in run.final_output["country"].lower()


# --------------------------------------------------------------------------- #
# 2. Conditional branching: live decider routes to a deterministic branch
# --------------------------------------------------------------------------- #


@requires_openai
async def test_live_conditional_branch(sqlite_db) -> None:
    """A live sentiment decision drives ``Condition`` edge routing through run_graph."""
    registry = ExecutableUnitRegistry()
    for branch in ("positive", "negative"):
        registry.register(
            f"eu://{branch}",
            _native_manifest(branch),
            input_model=Route,
            output_model=Label,
            handler=lambda _ctx, _data, b=branch: {"label": f"{b}-branch"},
        )
    decide = _live_runner(
        "decide",
        "Classify the sentiment of the input 'text'. Return route='positive' or "
        "route='negative' (lowercase, no other value).",
        input_model=TextIn,
        output_model=Route,
    )
    graph = Graph(
        graph_id="live-branch",
        name="branch",
        entry_step="decide",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            _agent_node("decide", "live-branch"),
            _unit_node("positive", "live-branch", "eu://positive"),
            _unit_node("negative", "live-branch", "eu://negative"),
        ],
        edges=[
            Edge(
                edge_id="edge-positive",
                source_node_id="decide",
                target_node_id="positive",
                condition=Condition(expression="payload.route == 'positive'"),
            ),
            Edge(
                edge_id="edge-negative",
                source_node_id="decide",
                target_node_id="negative",
                condition=Condition(expression="payload.route == 'negative'"),
            ),
        ],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"decide": decide},
        executable_unit_runner=ExecutableUnitRunner(registry),
    )

    run = await orch.run_graph(
        graph, {"text": "I absolutely love this — it's wonderful and made my whole day!"}
    )

    assert run.status is RunStatus.COMPLETED
    assert run.final_output == {"label": "positive-branch"}
    assert any(r.selected_edge_id == "edge-positive" for r in run.condition_results)
    assert [e.node_id for e in run.execution_history] == ["decide", "positive"]


# --------------------------------------------------------------------------- #
# 3. Loop / cycle: a real agent driven repeatedly, bounded by a guard
# --------------------------------------------------------------------------- #


@requires_openai
async def test_live_bounded_cycle_fails_closed(sqlite_db) -> None:
    """A self-looping live agent is driven repeatedly and fails closed at the guard.

    Mirrors the platform's canonical cycle test (``test_runtime.py``) with a real
    model in the loop: the cycle machinery dispatches the live agent ``max_total_steps``
    times, then stops the run rather than looping forever. (Clean conditional-exit
    semantics are covered deterministically by ``tests/conditions/test_branch.py``.)
    """
    loop = _live_runner(
        "loop",
        "Return the input 'value' unchanged in 'value'.",
        input_model=NumberIn,
        output_model=NumberOut,
    )
    graph = Graph(
        graph_id="live-cycle",
        name="cycle",
        entry_step="loop",
        execution_settings=ExecutionSettings(max_total_steps=2),
        nodes=[_agent_node("loop", "live-cycle")],
        edges=[Edge(edge_id="edge-loop", source_node_id="loop", target_node_id="loop")],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"loop": loop},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert run.failure_state.reason == "max_total_steps"
    assert len(run.execution_history) == 2  # the live agent really ran twice


# --------------------------------------------------------------------------- #
# 4. Subgraph composition: a live child agent inside a parent graph
# --------------------------------------------------------------------------- #


class _FakeDeploymentService:
    """Resolves deployments from an in-memory dict (stands in for the real service)."""

    def __init__(self, deployments: dict[str, Deployment]) -> None:
        self._deployments = deployments

    async def get(self, ref: str, version: int | None = None) -> Deployment | None:
        """Return the deployment registered under ``ref`` (version ignored)."""
        return self._deployments.get(ref)


@requires_openai
async def test_live_subgraph_with_live_child_agent(sqlite_db) -> None:
    """The orchestrator dispatches a live subgraph child agent given a keyed runner.

    SCOPE: this proves orchestrator-level subgraph dispatch — a child ``AgentNode``
    really calls the model and its output propagates to the parent — NOT production
    runner-population. The executor namespaces child node IDs to
    ``subgraph:<ref>:<depth>:<id>`` and looks runners up by that id (``runtime.py``),
    but nothing in core builds ``agent_runners`` from a deployed graph (``AgentRunner``
    is never constructed in ``src/``), so a caller has no public way to derive the
    namespaced child key. We register it explicitly; the test passing is itself proof
    that the *namespaced* key — not the bare ``c1`` — is the one dispatch resolves.
    The missing public key-derivation path is tracked separately as a gap.
    """
    child = _live_runner(
        "child",
        "The input has a 'country'. Return ONLY its capital city in 'answer'.",
        input_model=CountryIn,
        output_model=AnswerOut,
    )
    child_graph = Graph(
        graph_id="child-g",
        name="child",
        version=1,
        entry_step="c1",
        nodes=[_agent_node("c1", "child-g")],
        edges=[],
    )
    parent_graph = Graph(
        graph_id="parent-g",
        name="parent",
        version=1,
        entry_step="s1",
        nodes=[
            SubgraphNode(
                node_id="s1",
                graph_version_ref="parent-g:v1",
                subgraph=SubgraphNodeData(graph_ref="child-g"),
            )
        ],
        edges=[],
    )
    deployment = Deployment(
        deployment_id="dep-child-g",
        deployment_ref="child-g",
        graph_id="child-g",
        graph_version=1,
        graph_version_ref="child-g:v1",
        serialized_graph=serialize_graph(child_graph),
    )
    resolver = SubgraphResolver(deployment_service=_FakeDeploymentService({"child-g": deployment}))
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={
            # Top-level parent -> child depth is 1, no parallel branch, so the
            # executor's namespaced node id is "subgraph:child-g:1:c1". This is the
            # ONLY key registered: the run completing proves dispatch resolves the
            # namespaced id (a bare "c1" would never match the namespaced node).
            "subgraph:child-g:1:c1": child,
        },
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=SubgraphExecutor(resolver=resolver),
    )

    run = await orch.run_graph(parent_graph, {"country": "Japan"})

    assert run.status is RunStatus.COMPLETED
    # Parent with only a SubgraphNode adopts the child's (live) final output.
    assert "tokyo" in run.final_output["answer"].lower()


# --------------------------------------------------------------------------- #
# 5. Agent + code interop: live agent feeds a deterministic executable unit
# --------------------------------------------------------------------------- #


@requires_openai
async def test_live_agent_to_executable_unit(sqlite_db) -> None:
    """A live agent extracts a number; a native unit deterministically doubles it."""
    registry = ExecutableUnitRegistry()
    registry.register(
        "eu://double",
        _native_manifest("double"),
        input_model=NumberIn,
        output_model=NumberOut,
        handler=lambda _ctx, data: {"value": data.value * 2},
    )
    extract = _live_runner(
        "extract",
        "Extract the integer count mentioned in the input 'text'. Return it in 'value'.",
        input_model=TextIn,
        output_model=NumberOut,
    )
    graph = Graph(
        graph_id="live-interop",
        name="interop",
        entry_step="extract",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            _agent_node("extract", "live-interop"),
            _unit_node("double", "live-interop", "eu://double"),
        ],
        edges=[Edge(edge_id="e1", source_node_id="extract", target_node_id="double")],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"extract": extract},
        executable_unit_runner=ExecutableUnitRunner(registry),
    )

    run = await orch.run_graph(graph, {"text": "There are exactly 7 apples in the basket."})

    assert run.status is RunStatus.COMPLETED
    assert run.final_output == {"value": 14}  # 7 (live) -> 14 (deterministic)


# --------------------------------------------------------------------------- #
# 6. Human-approval pause / resume, with a live downstream agent
# --------------------------------------------------------------------------- #


@requires_openai
async def test_live_human_approval_pause_resume(sqlite_db) -> None:
    """Run pauses at an approval gate, then a live agent runs on the edited payload."""
    finish = _live_runner(
        "finish",
        "Return the input 'word' converted to UPPERCASE in 'shout'.",
        input_model=WordIn,
        output_model=ShoutOut,
    )
    graph = Graph(
        graph_id="live-approval",
        name="approval",
        entry_step="approval",
        nodes=[
            HumanApprovalNode(
                node_id="approval",
                graph_version_ref="live-approval:v1",
                output_contract_ref="contract://word",
                human_approval=HumanApprovalNodeData(approval_policy_config={"allow_edits": True}),
            ),
            _agent_node("finish", "live-approval"),
        ],
        edges=[Edge(edge_id="e1", source_node_id="approval", target_node_id="finish")],
    )
    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    orch = RuntimeOrchestrator(
        approval_service=approval_service,
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={"finish": finish},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    paused = await orch.run_graph(graph, {"word": "placeholder"})
    assert paused.status is RunStatus.WAITING_APPROVAL
    assert paused.current_node_ids == ["approval"]

    approval_id = paused.metadata["pending_approval"]["approval_id"]
    await approval_service.resolve(
        approval_id,
        decision=ApprovalDecision.EDIT_AND_APPROVE,
        actor=ActorIdentity(subject="tester", auth_method=AuthMethod.API_KEY),
        edited_payload={"word": "banana"},
    )

    resumed = await approval_service.continue_run(approval_id, graph=graph, orchestrator=orch)

    assert resumed.status is RunStatus.COMPLETED
    assert resumed.final_output["shout"].upper() == "BANANA"


# --------------------------------------------------------------------------- #
# 7. Parallel fan-out / fan-in: deterministic source, live branch agents
# --------------------------------------------------------------------------- #


@requires_openai
async def test_live_parallel_fanout(sqlite_db) -> None:
    """A fixed list fans out to live branch agents that each call the real model.

    The source is an ``AgentNode`` with a deterministic provider (fixed 3-item list)
    so the branch count is stable; the branches use the live provider, so real model
    calls run concurrently across the fan-out.
    """
    source = AgentRunner(
        AgentConfig(
            name="source",
            instruction="emit items",
            model_name=MODEL,
            input_model=SeedIn,
            output_model=ItemsOut,
        ),
        CallableProviderAdapter(
            lambda _req: ProviderResponse(
                content={"items": [{"color": "red"}, {"color": "blue"}, {"color": "green"}]}
            )
        ),
    )
    sink = _live_runner(
        "sink",
        "Is the input 'color' a warm color? Return warm=true or warm=false.",
        input_model=ColorIn,
        output_model=WarmOut,
    )
    graph = Graph(
        graph_id="live-fanout",
        name="fanout",
        entry_step="source",
        execution_settings=ExecutionSettings(max_total_steps=50),
        nodes=[
            _agent_node(
                "source", "live-fanout", parallel_config=ParallelConfig(split_path="items")
            ),
            _agent_node("sink", "live-fanout"),
        ],
        edges=[Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"source": source, "sink": sink},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {"seed": 1})

    assert run.status is RunStatus.COMPLETED
    # The live sink agent ran once per fanned-out item (branch node ids carry "sink").
    sink_runs = [e for e in run.execution_history if "sink" in e.node_id]
    assert len(sink_runs) == 3


# --------------------------------------------------------------------------- #
# 8. Retrieval -> agent: a live agent grounds its answer on retrieved context
# --------------------------------------------------------------------------- #


@requires_openai
async def test_live_retrieval_then_agent(sqlite_db) -> None:
    """A retrieval node injects a planted fact that a live agent must ground on."""
    planted = MemoryEntry(
        key="facts#0",
        value="Zeroth's official mascot is a platypus named Otto.",
        scope=MemoryScope.SHARED,
        scope_target="",
        metadata={"source_id": "facts"},
    )
    connector = _RecordingConnector([planted])
    registry = InMemoryConnectorRegistry()
    registry.register(
        "docs",
        ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED),
        connector,
    )
    resolver = MemoryConnectorResolver(registry=registry, workflow_name="live-rag")

    answerer = _live_runner(
        "answer",
        "Answer the 'question' using ONLY the provided 'context'. Reply with the "
        "answer in 'answer'.",
        input_model=QuestionContextIn,
        output_model=AnswerOut,
    )
    graph = Graph(
        graph_id="live-rag",
        name="rag",
        entry_step="retrieve",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            RetrievalNode(
                node_id="retrieve",
                graph_version_ref="live-rag:v1",
                retrieval=RetrievalNodeData(
                    connector_ref="docs", query_key="question", top_k=3, as_name="context"
                ),
            ),
            _agent_node("answer", "live-rag"),
        ],
        edges=[Edge(edge_id="e1", source_node_id="retrieve", target_node_id="answer")],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"answer": answerer},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        memory_resolver=resolver,
    )

    run = await orch.run_graph(graph, {"question": "What is the name of Zeroth's mascot?"})

    assert run.status is RunStatus.COMPLETED
    assert connector.search_calls  # retrieval really executed
    assert "otto" in run.final_output["answer"].lower()  # answer grounded on context
