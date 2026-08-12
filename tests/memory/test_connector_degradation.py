"""How memory connectors behave when the backend misbehaves (A07-32, AC4).

Every other suite in ``tests/memory`` drives its connector with a cooperative
double: the mock answers exactly the shape the happy path expects. Across all
five, the only injected fault was a single Elasticsearch ``NotFoundError``. So
the suite proved the connectors work against a *well-behaved* peer and said
nothing about the far likelier case -- a peer that is slow, truncated, or
answering a shape its own API does not document.

The three degradations covered here are the ones that reach production first:

* **a malformed embedding** -- an embedding provider returning no vector, or a
  reshaped payload,
* **a wrong or missing key in the backend's response** -- the measured default:
  a stub collection returning ``{}`` made ``read`` raise ``KeyError('ids')``,
  and a flat ``{"ids": []}`` made ``search`` raise
  ``IndexError('list index out of range')``,
* **a slow peer** -- a socket that accepts and then answers nothing, which is
  what an overloaded or partitioned backend looks like from this side.

The first two must surface as a typed connector error. A raw ``KeyError`` out
of a memory connector is a *lie*: it is this layer's own not-found signal
(``delete`` raises it deliberately) and ``error_vocabulary`` maps it to
``NOT_FOUND``, so a garbled response would be reported as "no such entry". A
raw ``IndexError`` reads as a bug in the connector rather than a backend fault.
The third must be cut off by the timeout the factory configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import redis.exceptions

from zeroth.integrations.memory import factory
from zeroth.integrations.memory.chroma_connector import (
    ChromaDBMemoryConnector,
    MemoryBackendResponseError,
)
from zeroth.integrations.memory.governed.models import MemoryScope

FAKE_EMBEDDING = [0.1] * 8

#: Distinct from ``None``, which is itself one of the malformed payloads tested.
_DEFAULT_EMBEDDING = object()

#: Long enough that a bounded client times out first, short enough that an
#: *unbounded* one still fails instead of hanging the run. That matters for the
#: negative controls: with the timeout removed these tests must fail, not wedge.
_PEER_HOLD_SECONDS = 3.0

#: The ceiling the tests install in place of the module default, so a slow-peer
#: assertion costs a quarter-second rather than ten.
_TEST_TIMEOUT_SECONDS = 0.25

#: Upper guard on the whole call. Only reached when the connector is unbounded.
_GUARD_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _embedding_response(data: object) -> MagicMock:
    """Build a litellm-shaped embedding response carrying *data*."""
    response = MagicMock()
    response.data = data
    return response


def _chroma_connector(collection: MagicMock, embedding: object) -> ChromaDBMemoryConnector:
    """Build a Chroma connector over *collection* with a patched embedder."""
    client = MagicMock()
    client.get_or_create_collection = MagicMock(return_value=collection)
    connector = ChromaDBMemoryConnector(
        client=client,
        collection_prefix="zeroth_test",
        embedding_model="text-embedding-3-small",
    )
    patcher = patch("zeroth.integrations.memory.chroma_connector.litellm")
    mock_litellm = patcher.start()
    mock_litellm.aembedding = AsyncMock(return_value=_embedding_response(embedding))
    connector._stop_embedding_patch = patcher.stop
    return connector


@pytest.fixture
def chroma_factory():
    """Yield a builder for Chroma connectors, stopping any litellm patch after."""
    built: list[ChromaDBMemoryConnector] = []

    def _build(
        collection: MagicMock | None = None, embedding: object = _DEFAULT_EMBEDDING
    ) -> ChromaDBMemoryConnector:
        if embedding is _DEFAULT_EMBEDDING:
            embedding = [{"embedding": FAKE_EMBEDDING}]
        connector = _chroma_connector(collection or MagicMock(), embedding)
        built.append(connector)
        return connector

    yield _build
    for connector in built:
        connector._stop_embedding_patch()


@contextlib.asynccontextmanager
async def _black_hole_peer() -> AsyncIterator[int]:
    """Serve a port that accepts connections, answers nothing, then hangs up.

    The hang-up matters as much as the silence. A peer that never closes would
    make an *unbounded* client block forever, and the negative control for the
    timeout fix would wedge the test run instead of failing it. Closing after
    ``_PEER_HOLD_SECONDS`` means an unbounded client fails late and with the
    wrong exception -- a clean, terminating failure.

    The pending handlers are cancelled on the way out. ``Server.wait_closed``
    joins its connection tasks, so leaving them to expire on their own would
    charge every passing test the full hold in teardown -- time that proves
    nothing, since the assertion already fired at the timeout.
    """
    handlers: set[asyncio.Task] = set()

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            handlers.add(task)
        try:
            await asyncio.sleep(_PEER_HOLD_SECONDS)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        yield port
    finally:
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# A malformed embedding
# ---------------------------------------------------------------------------


class TestMalformedEmbedding:
    """An embedding provider that answers without a usable vector."""

    @pytest.mark.parametrize(
        ("data", "reason"),
        [
            ([], "no entries at all"),
            (None, "data is not a sequence"),
            ([{}], "entry carries no 'embedding' field"),
            ([{"embedding": []}], "vector is empty"),
            ([{"embedding": "not-a-vector"}], "vector is a string"),
            ([{"embedding": [0.1, None, 0.3]}], "vector holds a non-number"),
        ],
    )
    async def test_write_refuses_a_malformed_embedding(self, chroma_factory, data, reason):
        connector = chroma_factory(embedding=data)

        with pytest.raises(MemoryBackendResponseError) as caught:
            await connector.write("doc1", {"text": "hi"}, MemoryScope.SHARED, target="__shared__")

        assert not isinstance(caught.value, KeyError | IndexError), reason

    async def test_search_refuses_a_malformed_embedding(self, chroma_factory):
        connector = chroma_factory(embedding=[{"no_embedding_here": True}])

        with pytest.raises(MemoryBackendResponseError, match="no 'embedding' field"):
            await connector.search({"text": "hi"}, MemoryScope.SHARED, target="__shared__")

    async def test_a_malformed_embedding_never_reaches_the_backend(self, chroma_factory):
        """The bad vector is caught at the boundary, not stored."""
        collection = MagicMock()
        connector = chroma_factory(collection=collection, embedding=[])

        with pytest.raises(MemoryBackendResponseError):
            await connector.write("doc1", {"text": "hi"}, MemoryScope.SHARED, target="__shared__")

        collection.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# A wrong or missing key in the backend's response
# ---------------------------------------------------------------------------


class TestMalformedBackendResponse:
    """A backend answering a shape other than the one its API documents."""

    async def test_read_of_an_empty_response_is_a_backend_error_not_a_key_error(
        self, chroma_factory
    ):
        """Measured default: ``{}`` from ``get`` raised ``KeyError('ids')``."""
        collection = MagicMock()
        collection.get = MagicMock(return_value={})
        connector = chroma_factory(collection=collection)

        with pytest.raises(MemoryBackendResponseError) as caught:
            await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

        assert "'ids'" in str(caught.value)
        assert not isinstance(caught.value, KeyError)

    async def test_read_of_a_response_missing_documents_is_a_backend_error(self, chroma_factory):
        collection = MagicMock()
        collection.get = MagicMock(return_value={"ids": ["doc1"]})
        connector = chroma_factory(collection=collection)

        with pytest.raises(MemoryBackendResponseError, match="documents"):
            await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

    async def test_read_of_a_truncated_response_is_a_backend_error_not_an_index_error(
        self, chroma_factory
    ):
        collection = MagicMock()
        collection.get = MagicMock(return_value={"ids": ["doc1"], "documents": [], "metadatas": []})
        connector = chroma_factory(collection=collection)

        with pytest.raises(MemoryBackendResponseError) as caught:
            await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

        assert not isinstance(caught.value, IndexError)

    async def test_read_of_an_undecodable_document_is_a_backend_error(self, chroma_factory):
        collection = MagicMock()
        collection.get = MagicMock(
            return_value={"ids": ["doc1"], "documents": ["{not json"], "metadatas": [{}]}
        )
        connector = chroma_factory(collection=collection)

        with pytest.raises(MemoryBackendResponseError, match="not valid JSON"):
            await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

    async def test_search_of_a_flat_response_is_a_backend_error_not_an_index_error(
        self, chroma_factory
    ):
        """Measured default: ``{"ids": []}`` from ``query`` raised ``IndexError``.

        ``query`` answers per query embedding, so every field is a list of one
        list. A flat ``ids`` is a malformed response, not an empty result set.
        """
        collection = MagicMock()
        collection.query = MagicMock(return_value={"ids": []})
        connector = chroma_factory(collection=collection)

        with pytest.raises(MemoryBackendResponseError) as caught:
            await connector.search({"text": "hi"}, MemoryScope.SHARED, target="__shared__")

        assert not isinstance(caught.value, IndexError)

    async def test_search_of_a_response_missing_documents_is_a_backend_error(self, chroma_factory):
        collection = MagicMock()
        collection.query = MagicMock(return_value={"ids": [["doc1"]], "metadatas": [[{}]]})
        connector = chroma_factory(collection=collection)

        with pytest.raises(MemoryBackendResponseError, match="documents"):
            await connector.search({"text": "hi"}, MemoryScope.SHARED, target="__shared__")

    async def test_delete_of_an_empty_response_is_a_backend_error_not_a_missing_entry(
        self, chroma_factory
    ):
        """``delete`` raises ``KeyError`` for a genuinely absent entry.

        A garbled response must not borrow that signal, or a broken backend
        reads to the caller as an empty store.
        """
        collection = MagicMock()
        collection.get = MagicMock(return_value={})
        connector = chroma_factory(collection=collection)

        with pytest.raises(MemoryBackendResponseError):
            await connector.delete("doc1", MemoryScope.SHARED, target="__shared__")

        collection.delete.assert_not_called()

    async def test_a_genuinely_absent_entry_still_raises_key_error(self, chroma_factory):
        """The not-found protocol survives the hardening."""
        collection = MagicMock()
        collection.get = MagicMock(return_value={"ids": [], "documents": [], "metadatas": []})
        connector = chroma_factory(collection=collection)

        with pytest.raises(KeyError):
            await connector.delete("missing", MemoryScope.SHARED, target="__shared__")

    async def test_a_well_formed_response_still_reads_back(self, chroma_factory):
        """The hardening must not turn a valid answer into an error."""
        collection = MagicMock()
        collection.get = MagicMock(
            return_value={
                "ids": ["doc1"],
                "documents": [json.dumps({"text": "hello"})],
                "metadatas": [{"key": "doc1"}],
            }
        )
        connector = chroma_factory(collection=collection)

        entry = await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

        assert entry is not None
        assert entry.value == {"text": "hello"}


# ---------------------------------------------------------------------------
# A slow peer
# ---------------------------------------------------------------------------


class TestSlowPeerTimeout:
    """A07-14 end to end: the configured ceiling actually bounds a real socket.

    Asserting the kwarg was passed only proves the call site. These drive a
    genuine TCP peer that accepts and stays silent, so they fail if the value
    does not reach the driver's read path.
    """

    async def test_a_silent_redis_peer_is_cut_off_at_the_configured_timeout(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(factory, "REDIS_TIMEOUT_SECONDS", _TEST_TIMEOUT_SECONDS)

        async with _black_hole_peer() as port:
            _, connector = factory.build_connector(
                "redis_thread", {"url": f"redis://127.0.0.1:{port}"}
            )
            started = time.monotonic()
            try:
                # redis.exceptions.TimeoutError, deliberately: it subclasses the
                # builtin, so catching the builtin here would also catch the
                # outer guard and the assertion would prove nothing.
                with pytest.raises(redis.exceptions.TimeoutError):
                    await asyncio.wait_for(
                        connector.read("messages", MemoryScope.THREAD, target="t-1"),
                        timeout=_GUARD_SECONDS,
                    )
                elapsed = time.monotonic() - started
            finally:
                await connector._redis.aclose()

        assert elapsed < _PEER_HOLD_SECONDS / 2, (
            f"redis waited {elapsed:.2f}s on a silent peer; the "
            f"{_TEST_TIMEOUT_SECONDS}s socket timeout did not reach the driver"
        )

    async def test_a_silent_chroma_peer_is_cut_off_at_the_configured_timeout(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(factory, "CHROMA_TIMEOUT_SECONDS", _TEST_TIMEOUT_SECONDS)
        session = httpx.Client(timeout=None)
        stub = SimpleNamespace(_server=SimpleNamespace(_session=session))

        async with _black_hole_peer() as port:
            with patch("zeroth.integrations.memory.factory.chromadb") as mock_chromadb:
                mock_chromadb.HttpClient.return_value = stub
                factory.build_connector("chroma", {"host": "127.0.0.1", "port": port})

            started = time.monotonic()
            try:
                with pytest.raises(httpx.ReadTimeout):
                    await asyncio.wait_for(
                        asyncio.to_thread(session.get, f"http://127.0.0.1:{port}/api/v2/heartbeat"),
                        timeout=_GUARD_SECONDS,
                    )
                elapsed = time.monotonic() - started
            finally:
                session.close()

        assert elapsed < _PEER_HOLD_SECONDS / 2, (
            f"chroma waited {elapsed:.2f}s on a silent peer; the "
            f"{_TEST_TIMEOUT_SECONDS}s ceiling did not reach its httpx client"
        )
