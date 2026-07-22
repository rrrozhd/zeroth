from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from langgraph.pregel.remote import RemoteGraph
from langgraph_sdk import get_client

from .harness import ConformanceServers


pytestmark = pytest.mark.langgraph_conformance


def test_conformance_servers_change_only_url_and_api_credential() -> None:
    direct = ConformanceServers.client_configuration("http://direct", "direct-key")
    proxied = ConformanceServers.client_configuration("http://gateway", "gateway-key")

    assert set(direct) == {"url", "api_key"}
    assert set(proxied) == {"url", "api_key"}


@pytest.fixture(scope="module")
def servers() -> Iterator[ConformanceServers]:
    with ConformanceServers() as running:
        yield running


async def _exercise_sdk(
    url: str, api_key: str, *, observation_url: str | None = None
) -> dict[str, object]:
    async with get_client(url=url, api_key=api_key) as client:
        assistant = await client.assistants.create("conformance", name="sdk-parity")
        assert (await client.assistants.get(assistant["assistant_id"]))["graph_id"] == "conformance"
        assert await client.assistants.search(graph_id="conformance", limit=10)

        thread = await client.threads.create(metadata={"suite": "sdk-parity"})
        thread_id = thread["thread_id"]
        assert (await client.threads.get(thread_id))["thread_id"] == thread_id
        assert await client.threads.search(metadata={"suite": "sdk-parity"}, limit=10)

        waited = await client.runs.wait(
            thread_id,
            assistant["assistant_id"],
            input={"mode": "echo", "text": "sdk-wait"},
        )
        state = await client.threads.get_state(thread_id)
        await client.threads.update_state(thread_id, {"text": "sdk-updated"})
        updated = await client.threads.get_state(thread_id)
        history = await client.threads.get_history(thread_id, limit=10)

        streamed = [
            part
            async for part in client.runs.stream(
                None,
                assistant["assistant_id"],
                input={"mode": "echo", "text": "sdk-stream"},
                stream_mode=["custom", "values"],
            )
        ]
        stateless = await client.runs.wait(
            None,
            assistant["assistant_id"],
            input={"mode": "echo", "text": "sdk-stateless"},
        )

        lifecycle_thread = await client.threads.create()
        background = await client.runs.create(
            lifecycle_thread["thread_id"],
            assistant["assistant_id"],
            input={"mode": "echo", "text": "sdk-background"},
            stream_mode="values",
            stream_resumable=True,
        )
        fetched = await client.runs.get(lifecycle_thread["thread_id"], background["run_id"])
        joined = await client.runs.join(lifecycle_thread["thread_id"], background["run_id"])
        joined_stream = [
            part
            async for part in client.runs.join_stream(
                lifecycle_thread["thread_id"], background["run_id"]
            )
        ]

        cancel_thread = await client.threads.create()
        cancellable = await client.runs.create(
            cancel_thread["thread_id"],
            assistant["assistant_id"],
            input={"mode": "cancel"},
        )
        await client.runs.cancel(cancel_thread["thread_id"], cancellable["run_id"], wait=True)
        cancelled = await client.runs.get(cancel_thread["thread_id"], cancellable["run_id"])

        continue_thread = await client.threads.create()
        continuing_stream = client.runs.stream(
            continue_thread["thread_id"],
            assistant["assistant_id"],
            input={"mode": "echo", "text": "continue-after-disconnect"},
            stream_mode="values",
            on_disconnect="continue",
        )
        await anext(continuing_stream)
        await continuing_stream.aclose()
        continue_status = "pending"
        async with httpx.AsyncClient(base_url=observation_url or url, timeout=10) as observer:
            for _ in range(100):
                response = await observer.get(f"/threads/{continue_thread['thread_id']}/runs")
                response.raise_for_status()
                continue_runs = response.json()
                if continue_runs and continue_runs[0]["status"] in {
                    "success",
                    "error",
                    "interrupted",
                }:
                    continue_status = continue_runs[0]["status"]
                    break
                await asyncio.sleep(0.01)

    return {
        "wait": waited["result"],
        "state": state["values"]["result"],
        "updated": updated["values"]["text"],
        "history_nonempty": bool(history),
        "stream_modes": [part.event for part in streamed],
        "stateless": stateless["result"],
        "fetched_status": fetched["status"],
        "joined": joined["result"],
        "joined_stream_modes": [part.event for part in joined_stream],
        "cancelled_status": cancelled["status"],
        "continue_status": continue_status,
    }


@pytest.mark.asyncio
async def test_official_sdk_parity_covers_inventory_modes(servers: ConformanceServers) -> None:
    direct = await _exercise_sdk(servers.direct_url, "direct-key")
    proxied = await _exercise_sdk(
        servers.gateway_url,
        "gateway-key",
        observation_url=servers.direct_url,
    )
    assert proxied == direct


def _sync_remote_summary(url: str, api_key: str) -> dict[str, object]:
    remote = RemoteGraph("conformance", url=url, api_key=api_key)
    invoked = remote.invoke({"mode": "echo", "text": "remote-invoke"})
    streamed = list(
        remote.stream(
            {"mode": "echo", "text": "remote-stream"},
            stream_mode=["custom", "values"],
        )
    )
    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    remote.invoke({"mode": "echo", "text": "remote-state"}, config=config)
    state = remote.get_state(config)
    history = list(remote.get_state_history(config))
    remote.update_state(config, {"text": "remote-updated"})
    updated = remote.get_state(config)
    return {
        "invoke": invoked["result"],
        "stream": streamed,
        "state": state.values["result"],
        "history_nonempty": bool(history),
        "updated": updated.values["text"],
    }


def test_remote_graph_sync_parity(servers: ConformanceServers) -> None:
    assert _sync_remote_summary(servers.gateway_url, "gateway-key") == _sync_remote_summary(
        servers.direct_url, "direct-key"
    )


@pytest.mark.asyncio
async def test_remote_graph_async_parity(servers: ConformanceServers) -> None:
    async def summary(url: str, key: str) -> tuple[object, list[object]]:
        remote = RemoteGraph("conformance", url=url, api_key=key)
        invoked = await remote.ainvoke({"mode": "echo", "text": "remote-ainvoke"})
        streamed = [
            part
            async for part in remote.astream(
                {"mode": "echo", "text": "remote-astream"},
                stream_mode=["custom", "values"],
            )
        ]
        return invoked["result"], streamed

    assert await summary(servers.gateway_url, "gateway-key") == await summary(
        servers.direct_url, "direct-key"
    )
