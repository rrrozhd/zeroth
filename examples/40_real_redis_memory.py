"""40 — Persistent conversation memory backed by a *real* Redis.

What this shows
---------------
A multi-turn conversation whose history lives in Redis, so it survives the
process exiting. Run this script once and it appends the turn to Redis;
run it **again as a brand-new process** with the same ``thread_id`` and
the agent reads the earlier turns back and answers with them in context.
That cross-process recall is what a real store buys you.

The store is the witness: the script prints the prior turns it read from
Redis before the model is ever called, so you can see history persist
independently of what the LLM says.

Note on the shape
-----------------
This drives the :class:`RedisThreadMemoryConnector` **directly** rather
than relying on the orchestrator's automatic memory injection. The
auto-injection path (the ``memory_refs`` machinery ``05_memory.py``
shows) is *run-scoped* today -- each run reads/writes under its own run
id -- so it does not carry conversation state across runs. Reading the
thread's history and appending each turn yourself, as below, is how you
get real cross-process conversation memory on top of the connector.

Requirements
------------
* ``OPENAI_API_KEY`` in the environment (uses ``openai/gpt-4o-mini``).
* A reachable Redis. Start a throwaway one on a non-standard port:

      docker run -d --name zeroth-ex-redis -p 16379:6379 redis:7-alpine

Run it (two separate processes, same thread)
--------------------------------------------
    # Turn 1 -- writes to Redis:
    OPENAI_API_KEY=sk-... ZEROTH_EXAMPLE_REDIS_URL=redis://localhost:16379/0 \
        uv run python examples/40_real_redis_memory.py "My name is Dato and my favorite color is teal."

    # Turn 2 -- a fresh process that recalls what turn 1 stored:
    OPENAI_API_KEY=sk-... ZEROTH_EXAMPLE_REDIS_URL=redis://localhost:16379/0 \
        uv run python examples/40_real_redis_memory.py "What is my favorite color?"

Override the conversation with ``ZEROTH_EXAMPLE_THREAD`` and the model with
``ZEROTH_EXAMPLE_MODEL``. Tear down with ``docker rm -f zeroth-ex-redis``.
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import os
import sys

from examples._common import print_run_summary, require_env, running_service
from examples._contracts import Answer, Question
from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
)
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.redis_thread import RedisThreadMemoryConnector
from zeroth.runtime.agents import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
)

KEY_PREFIX = "zeroth:example:chat"
HISTORY_KEY = "messages"


def build_graph(model_name: str) -> Graph:
    return Graph(
        graph_id="real-redis-memory",
        name="Real Redis memory demo",
        version=1,
        entry_step="assistant",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="assistant",
                graph_version_ref="real-redis-memory@1",
                display=DisplayMetadata(title="Assistant with persistent memory"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction=(
                        "You are continuing a conversation. Use the prior transcript provided "
                        "in the message to answer the user's latest line. Reply in one short "
                        "sentence as JSON matching the output schema."
                    ),
                    model_provider=model_name,
                    model_params={"temperature": 0.2, "max_tokens": 120},
                ),
            ),
        ],
        edges=[],
    )


async def main() -> int:
    if not require_env("OPENAI_API_KEY"):
        return 0

    import redis.asyncio as aioredis

    model_name = os.environ.get("ZEROTH_EXAMPLE_MODEL", "openai/gpt-4o-mini")
    redis_url = os.environ.get("ZEROTH_EXAMPLE_REDIS_URL", "redis://localhost:6379/0")
    thread_id = os.environ.get("ZEROTH_EXAMPLE_THREAD", "demo-conversation")
    question = sys.argv[1] if len(sys.argv) > 1 else "What do you remember about me?"

    redis_client = aioredis.from_url(redis_url)
    try:
        await redis_client.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: Redis not reachable at {redis_url}: {exc}", file=sys.stderr)
        await redis_client.aclose()
        return 0

    connector = RedisThreadMemoryConnector(redis_client, key_prefix=KEY_PREFIX)
    try:
        # 1) Read this thread's history back from Redis (the persistence witness).
        #    search() returns newest-first; reverse for a chronological transcript.
        history = await connector.search(
            {"limit": 50}, MemoryScope.THREAD, target=thread_id
        )
        turns = [e.value for e in reversed(history) if isinstance(e.value, dict)]
        print(f"\nthread={thread_id!r}  question={question!r}")
        print(f"  [from Redis] {len(turns)} prior turn(s) on this thread:")
        for turn in turns:
            print(f"      {turn.get('role')}: {turn.get('content')}")

        # 2) Build the grounded prompt from the recovered transcript.
        transcript = "\n".join(f"{t.get('role')}: {t.get('content')}" for t in turns)
        grounded = (
            "Prior conversation on this thread:\n"
            f"{transcript or '(this is the first turn)'}\n\n"
            f"The user now says: {question}"
        )

        # 3) Answer with a real agent through the real orchestrator.
        runner = AgentRunner(
            AgentConfig(
                name="assistant",
                description="Continues a conversation using Redis-backed history.",
                instruction="Answer using the prior transcript.",
                model_name=model_name,
                input_model=Question,
                output_model=Answer,
            ),
            LiteLLMProviderAdapter(),
        )
        async with running_service(
            build_graph(model_name),
            contracts={
                "contract://question": Question,
                "contract://answer": Answer,
            },
            agent_runners={"assistant": runner},
        ) as demo:
            run = await demo.service.orchestrator.run_graph(
                demo.service.graph,
                {"question": grounded},
                deployment_ref=demo.deployment_ref,
            )
            print_run_summary(run, label="redis-memory")

        # 4) Append this turn (user + assistant) so the next process sees it.
        answer = (run.final_output or {}).get("answer", "") if run.final_output else ""
        await connector.write(
            HISTORY_KEY, {"role": "user", "content": question}, MemoryScope.THREAD, target=thread_id
        )
        await connector.write(
            HISTORY_KEY,
            {"role": "assistant", "content": answer},
            MemoryScope.THREAD,
            target=thread_id,
        )
        print(
            "\nRun this script again (a fresh process) with the same thread to see the "
            "agent recall this turn from Redis."
        )
    finally:
        await redis_client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
