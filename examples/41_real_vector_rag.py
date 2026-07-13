"""41 — Semantic recall (RAG) over a *real* Chroma vector store.

What this shows
---------------
A retrieval-augmented answer wired from real parts end to end:

1. seed a tiny knowledge base into a real :class:`ChromaDBMemoryConnector`
   (every ``write`` generates a real OpenAI embedding via litellm);
2. ``connector.search(...)`` runs a real cosine-similarity query and
   returns the most relevant facts (this is the path the pgvector/Chroma
   connector fixes make work end to end);
3. the retrieved facts are handed to a real :class:`AgentRunner`
   (:class:`LiteLLMProviderAdapter`/OpenAI) which answers grounded in them.

The store is the witness: the script prints exactly what
``connector.search`` returned *before* the LLM answers, so you can see
retrieval working independently of the model.

Note on the shape
-----------------
This is **direct-connector retrieval feeding an agent** -- not the agent
runtime's automatic memory injection. The auto-injection path (see
``05_memory.py`` / ``40_real_redis_memory.py``) only does
``read("latest")`` by key; it does not do semantic search. So semantic
retrieval is driven explicitly here, which is how you'd build RAG on top
of the memory connectors today.

Requirements
------------
* ``OPENAI_API_KEY`` in the environment (embeddings + chat completion).
* A reachable ChromaDB server. Start a throwaway one:

      docker run -d --name zeroth-ex-chroma -p 18000:8000 chromadb/chroma:1.5.6

Run
---
    OPENAI_API_KEY=sk-... ZEROTH_EXAMPLE_CHROMA_PORT=18000 \
        uv run python examples/41_real_vector_rag.py "What does Zeroth do?"

    # Try a question that targets a different seeded fact:
    OPENAI_API_KEY=sk-... ZEROTH_EXAMPLE_CHROMA_PORT=18000 \
        uv run python examples/41_real_vector_rag.py "How do plants make energy?"

Tear down with ``docker rm -f zeroth-ex-chroma``.
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
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
)
from zeroth.core.governed.memory.models import MemoryScope
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
)
from zeroth.core.memory.chroma_connector import ChromaDBMemoryConnector

# A tiny knowledge base. Each fact is stored as its own document so the
# vector search has something to discriminate between.
KNOWLEDGE_BASE = {
    "zeroth": "Zeroth is a governed platform for running multi-agent systems as standalone API services.",
    "france": "The capital of France is Paris, on the river Seine.",
    "photosynthesis": "Photosynthesis lets plants convert sunlight, water, and CO2 into chemical energy.",
    "redis": "The Redis thread connector stores conversation history in Redis sorted sets.",
    "pgvector": "pgvector adds vector similarity search with HNSW indexes to PostgreSQL.",
}
SCOPE_TARGET = "kb"


def build_graph(model_name: str) -> Graph:
    return Graph(
        graph_id="real-vector-rag",
        name="Real vector RAG demo",
        version=1,
        entry_step="answerer",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="answerer",
                graph_version_ref="real-vector-rag@1",
                display=DisplayMetadata(title="Grounded answerer"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction=(
                        "Answer the user's question using ONLY the provided context. If the "
                        "context does not contain the answer, say you don't know. Reply in one "
                        "short sentence as JSON matching the output schema."
                    ),
                    model_provider=model_name,
                    model_params={"temperature": 0.0, "max_tokens": 150},
                ),
            ),
        ],
        edges=[],
    )


async def main() -> int:
    if not require_env("OPENAI_API_KEY"):
        return 0

    import chromadb

    model_name = os.environ.get("ZEROTH_EXAMPLE_MODEL", "openai/gpt-4o-mini")
    host = os.environ.get("ZEROTH_EXAMPLE_CHROMA_HOST", "localhost")
    port = int(os.environ.get("ZEROTH_EXAMPLE_CHROMA_PORT", "8000"))
    question = sys.argv[1] if len(sys.argv) > 1 else "What does Zeroth do?"

    try:
        chroma_client = chromadb.HttpClient(host=host, port=port)
        chroma_client.heartbeat()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: ChromaDB not reachable at {host}:{port}: {exc}", file=sys.stderr)
        return 0

    connector = ChromaDBMemoryConnector(chroma_client, collection_prefix="zeroth_example")

    # 1) Seed the knowledge base (real embeddings, idempotent upserts).
    print(f"\nSeeding {len(KNOWLEDGE_BASE)} facts into Chroma (real embeddings)...")
    for key, fact in KNOWLEDGE_BASE.items():
        await connector.write(key, {"text": fact}, MemoryScope.SHARED, target=SCOPE_TARGET)

    # 2) Semantic search -- the store is the witness.
    hits = await connector.search(
        {"text": question, "limit": 3}, MemoryScope.SHARED, target=SCOPE_TARGET
    )
    contexts = [h.value["text"] for h in hits if isinstance(h.value, dict) and "text" in h.value]
    print(f"\nconnector.search({question!r}) returned {len(contexts)} fact(s):")
    for i, ctx in enumerate(contexts, 1):
        print(f"  {i}. {ctx}")

    # 3) Hand the retrieved context to a real agent to answer.
    grounded_question = (
        "Use ONLY the context below to answer the question.\n\n"
        "Context:\n" + "\n".join(f"- {c}" for c in contexts) + f"\n\nQuestion: {question}"
    )
    runner = AgentRunner(
        AgentConfig(
            name="answerer",
            description="Answers a question grounded in retrieved context.",
            instruction="Answer using only the provided context.",
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
        agent_runners={"answerer": runner},
    ) as demo:
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"question": grounded_question},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="vector-rag")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
