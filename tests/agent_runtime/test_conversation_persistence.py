"""Persistent conversation: thread-state history replayed and rolled forward."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pydantic import BaseModel

from zeroth.contracts.graph.models import AgentNode, AgentNodeData
from zeroth.contracts.graph.validation_errors import ValidationCode
from zeroth.runtime.agents import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.runtime.agents.models import InMemoryThreadStateStore, PromptConfig
from zeroth.runtime.agents.prompt import PromptAssembler
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import Run


class ChatInput(BaseModel):
    messages: list[dict]


class ChatOutput(BaseModel):
    answer: str


def _config(**overrides) -> AgentConfig:
    defaults = dict(
        name="chat-agent",
        instruction="Answer helpfully.",
        model_name="governai:test",
        input_model=ChatInput,
        output_model=ChatOutput,
        prompt_config=PromptConfig(messages_key="messages", persist_conversation=True),
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


class TestAssembler:
    _THREAD_STATE = {
        "output": {"answer": "earlier"},
        "conversation": [
            {"role": "human", "content": "What is my balance?"},
            {"role": "ai", "content": '{"answer": "42 EUR"}'},
        ],
    }

    def test_stored_turns_replay_before_incoming(self) -> None:
        assembly = PromptAssembler().assemble(
            _config(),
            {"messages": [{"role": "human", "content": "And in USD?"}]},
            thread_state=self._THREAD_STATE,
        )
        turns = assembly.messages[2:]
        assert [t.role for t in turns] == ["user", "assistant", "user"]
        assert turns[0].content == "What is my balance?"
        assert turns[2].content == "And in USD?"
        assert assembly.metadata["conversation_stored_turns"] == 2
        # The thread-state block must not render the same turns twice.
        assert "What is my balance?" not in assembly.messages[1].content

    def test_max_turns_trims_oldest(self) -> None:
        config = _config(
            prompt_config=PromptConfig(
                messages_key="messages",
                persist_conversation=True,
                conversation_max_turns=2,
            )
        )
        assembly = PromptAssembler().assemble(
            config,
            {"messages": [{"role": "human", "content": "And in USD?"}]},
            thread_state=self._THREAD_STATE,
        )
        turns = assembly.messages[2:]
        assert [t.role for t in turns] == ["assistant", "user"]
        assert turns[1].content == "And in USD?"

    def test_no_replay_when_persistence_off(self) -> None:
        config = _config(prompt_config=PromptConfig(messages_key="messages"))
        assembly = PromptAssembler().assemble(
            config,
            {"messages": [{"role": "human", "content": "And in USD?"}]},
            thread_state=self._THREAD_STATE,
        )
        assert [t.role for t in assembly.messages[2:]] == ["user"]


class TestRunner:
    async def test_conversation_rolls_forward_across_runs(self) -> None:
        store = InMemoryThreadStateStore()
        provider = DeterministicProviderAdapter(
            [
                ProviderResponse(content='{"answer": "42 EUR"}'),
                ProviderResponse(content='{"answer": "48 USD"}'),
            ]
        )
        runner = AgentRunner(_config(), provider, thread_state_store=store)

        await runner.run(
            {"messages": [{"role": "human", "content": "What is my balance?"}]},
            thread_id="thread-1",
        )
        state = store.latest("thread-1")
        assert state is not None
        assert [t["role"] for t in state["conversation"]] == ["human", "ai"]
        assert json.loads(state["conversation"][1]["content"]) == {"answer": "42 EUR"}

        second = await runner.run(
            {"messages": [{"role": "human", "content": "And in USD?"}]},
            thread_id="thread-1",
        )
        state = store.latest("thread-1")
        assert [t["role"] for t in state["conversation"]] == ["human", "ai", "human", "ai"]
        assert json.loads(state["conversation"][3]["content"]) == {"answer": "48 USD"}
        # The second prompt replayed the first exchange as real turns.
        replayed = [m.content for m in second.prompt.messages[2:]]
        assert replayed[0] == "What is my balance?"
        assert second.prompt.metadata["conversation_stored_turns"] == 2

    async def test_max_turns_caps_stored_history(self) -> None:
        store = InMemoryThreadStateStore()
        provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer": "ok"}')] * 3)
        config = _config(
            prompt_config=PromptConfig(
                messages_key="messages",
                persist_conversation=True,
                conversation_max_turns=3,
            )
        )
        runner = AgentRunner(config, provider, thread_state_store=store)
        for i in range(3):
            await runner.run(
                {"messages": [{"role": "human", "content": f"turn {i}"}]},
                thread_id="thread-1",
            )
        conversation = store.latest("thread-1")["conversation"]
        assert len(conversation) == 3
        # The newest exchange survives; the oldest turns fell off.
        assert conversation[-2]["content"] == "turn 2"

    async def test_no_conversation_key_when_persistence_off(self) -> None:
        store = InMemoryThreadStateStore()
        provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer": "ok"}')])
        config = _config(prompt_config=PromptConfig(messages_key="messages"))
        runner = AgentRunner(config, provider, thread_state_store=store)
        await runner.run(
            {"messages": [{"role": "human", "content": "hi"}]},
            thread_id="thread-1",
        )
        assert "conversation" not in store.latest("thread-1")


class TestWiring:
    async def test_persist_conversation_resolves_a_thread(self) -> None:
        class _Resolver:
            async def resolve(self, thread_id, **kwargs):
                return SimpleNamespace(thread=SimpleNamespace(thread_id="thread-new"))

        node = AgentNode(
            node_id="agent",
            graph_version_ref="g@1",
            agent=AgentNodeData(
                instruction="chat",
                model_provider="provider://test",
                input_messages_key="messages",
                persist_conversation=True,
            ),
        )
        orchestrator = RuntimeOrchestrator(
            run_repository=None,  # type: ignore[arg-type]
            agent_runners={},
            executable_unit_runner=None,  # type: ignore[arg-type]
            thread_resolver=_Resolver(),  # type: ignore[arg-type]
        )
        run = Run(graph_version_ref="g@1", deployment_ref="dep")
        assert await orchestrator._resolve_thread(node, run) == "thread-new"

    async def test_thread_participation_none_still_skips_without_persistence(self) -> None:
        node = AgentNode(
            node_id="agent",
            graph_version_ref="g@1",
            agent=AgentNodeData(instruction="chat", model_provider="provider://test"),
        )
        orchestrator = RuntimeOrchestrator(
            run_repository=None,  # type: ignore[arg-type]
            agent_runners={},
            executable_unit_runner=None,  # type: ignore[arg-type]
        )
        run = Run(graph_version_ref="g@1", deployment_ref="dep")
        assert await orchestrator._resolve_thread(node, run) is None

    async def test_persist_without_messages_key_fails_validation(self) -> None:
        from zeroth.contracts.graph.models import Graph

        graph = Graph(
            graph_id="g",
            name="g",
            entry_step="agent",
            nodes=[
                AgentNode(
                    node_id="agent",
                    graph_version_ref="g@1",
                    input_contract_ref="contract://in",
                    output_contract_ref="contract://out",
                    agent=AgentNodeData(
                        instruction="chat",
                        model_provider="provider://test",
                        persist_conversation=True,
                    ),
                )
            ],
            edges=[],
        )
        report = await GraphValidator().validate(graph)
        messages = [
            i.message for i in report.issues if i.code == ValidationCode.INVALID_NODE_ATTACHMENT
        ]
        assert any("persist_conversation" in m for m in messages)
