"""RAG ingestion: chunk documents and write them to a memory connector (RAG-02).

Splits source text into overlapping character chunks and writes each chunk to a
memory connector via its ``write(key, value, scope)`` method. Embedding is
*delegated to the connector* (chroma / pgvector embed on write) — this module does
not embed. Each chunk is keyed ``{source_id}#{index}`` so a RetrievalNode can later
attribute the retrieved chunk back to its source document.

An ingest lands whole or leaves nothing behind: a failed write is followed by a
sweep that deletes the keys this run wrote (RAG-02 / A07-23). See
:func:`ingest_documents` for what that guarantee does and does not cover.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zeroth.contracts.governed.models.memory import MemoryScope


@dataclass(frozen=True)
class SourceDocument:
    """A document to ingest: a stable source id and its text."""

    source_id: str
    text: str


@dataclass(frozen=True)
class IngestionReport:
    """Summary of one ingestion run."""

    documents: int
    chunks_written: int


@dataclass(frozen=True)
class IngestionCursor:
    """Where an interrupted ingest broke, for logs and reconciliation."""

    document_index: int
    """Index into the ``documents`` sequence that was being ingested."""

    source_id: str
    """``source_id`` of that document, for logs and reconciliation."""

    chunk_index: int
    """Index of the chunk whose write failed.

    A diagnostic, not a resume point: the chunks before it were swept back out
    (see :func:`ingest_documents`), so a retry re-offers the whole sequence.
    """


class IngestionRolledBackError(RuntimeError):
    """An ingest failed and every chunk it had written was removed again.

    This is the ordinary failure of :func:`ingest_documents`: the write loop
    broke partway and compensation succeeded, so the call left nothing behind.
    ``report`` is what this call left indexed -- ``(0, 0)`` -- and ``cursor``
    names where it broke. Retry by re-offering the whole ``documents``
    sequence; there is no partial ingest to resume from. The connector's own
    error is the ``__cause__``.
    """

    def __init__(self, cursor: IngestionCursor) -> None:
        super().__init__(
            f"ingest failed on document {cursor.document_index}"
            f" ({cursor.source_id!r}) at chunk {cursor.chunk_index};"
            " every chunk it had written was deleted again, so nothing is indexed"
        )
        self.report = IngestionReport(documents=0, chunks_written=0)
        self.cursor = cursor


class PartialIngestionError(RuntimeError):
    """An ingest failed *and* the cleanup of its own writes failed (A07-23).

    Compensation is the only unwind this protocol offers, so when a delete
    itself fails there is residue in the index that this process cannot remove.
    That is surfaced here rather than swallowed: ``orphaned_keys`` is the
    authoritative list of what is still indexed and needs durable
    reconciliation, and ``cleanup_errors`` holds the delete failures, one per
    orphaned key in the same order.

    ``report`` counts that same residue -- ``chunks_written`` is
    ``len(orphaned_keys)`` and ``documents`` is how many distinct source
    documents have chunks left over. Note that this is *not* the success-path
    meaning of :class:`IngestionReport`: no document here was ingested whole,
    these are leftovers. ``cursor`` names the write that broke the loop, and
    the connector's write error is the ``__cause__``.
    """

    def __init__(
        self,
        report: IngestionReport,
        cursor: IngestionCursor,
        orphaned_keys: tuple[str, ...] = (),
        cleanup_errors: tuple[BaseException, ...] = (),
    ) -> None:
        super().__init__(
            f"ingest failed on document {cursor.document_index}"
            f" ({cursor.source_id!r}) at chunk {cursor.chunk_index} and could not"
            f" clean up after itself; {report.chunks_written} chunk(s) across"
            f" {report.documents} document(s) are still indexed and need"
            f" reconciliation: {list(orphaned_keys)}"
        )
        self.report = report
        self.cursor = cursor
        self.orphaned_keys = orphaned_keys
        self.cleanup_errors = cleanup_errors


def chunk_text(text: str, *, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """Split ``text`` into overlapping fixed-size character chunks.

    Character-based (not token-based) chunking — simple and dependency-free.
    Returns an empty list for blank text. Raises ``ValueError`` for invalid sizes.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must be in the range [0, chunk_size)")
    stripped = text.strip()
    if not stripped:
        return []
    step = chunk_size - overlap
    return [stripped[start : start + chunk_size] for start in range(0, len(stripped), step)]


def _require_compensable(connector: Any) -> None:
    """Refuse an ingest that could not be undone.

    ``connector`` is duck-typed (``Any``), but "leaves no partial ingestion"
    rests entirely on ``delete``: without it, a write that fails halfway cannot
    be swept back out. Checking before the first write keeps the property
    trivially true for a connector that cannot support it -- nothing is written,
    so nothing is left. The :class:`~zeroth.integrations.memory.governed
    .connector.MemoryConnector` protocol declares both methods.
    """
    for method in ("write", "delete"):
        if not callable(getattr(connector, method, None)):
            raise TypeError(
                f"connector must expose an awaitable {method}(...): an ingest that"
                " cannot delete its own writes cannot promise to leave none behind"
            )


async def _sweep(
    connector: Any, written: Sequence[tuple[str, str]], scope: MemoryScope
) -> list[tuple[tuple[str, str], BaseException]]:
    """Delete every key this ingest wrote; answer the ones that survived it.

    Absence is the postcondition, not deletion, so ``KeyError`` -- what the
    backends answer for "no such key" (``chroma_connector.delete``,
    ``pgvector_connector.delete``) -- counts as success. Two properties this
    loop must keep: a failed delete does not abort the sweep (that would orphan
    every key after it), and no key is attempted twice (the write loop is
    bounded by its input and its compensation has to stay bounded too).
    """
    failures: list[tuple[tuple[str, str], BaseException]] = []
    for entry in reversed(written):
        try:
            await connector.delete(entry[1], scope)
        except KeyError:
            continue  # already absent -- which is all this sweep wants
        except Exception as exc:  # noqa: PERF203 - one attempt per key, never retried
            failures.append((entry, exc))
    return failures


async def _interrupted(
    connector: Any,
    written: Sequence[tuple[str, str]],
    scope: MemoryScope,
    cursor: IngestionCursor,
) -> RuntimeError:
    """Undo what this ingest wrote and build the error that says how it went."""
    failures = await _sweep(connector, written, scope)
    if not failures:
        return IngestionRolledBackError(cursor)
    orphans = [entry for entry, _ in failures]
    return PartialIngestionError(
        IngestionReport(
            documents=len({source_id for source_id, _ in orphans}),
            chunks_written=len(orphans),
        ),
        cursor,
        tuple(key for _, key in orphans),
        tuple(exc for _, exc in failures),
    )


async def ingest_documents(
    connector: Any,
    documents: Sequence[SourceDocument],
    scope: MemoryScope,
    *,
    chunk_size: int = 1000,
    overlap: int = 200,
) -> IngestionReport:
    """Chunk each document and write its chunks to ``connector`` (embedding delegated).

    Each chunk is written under key ``{source_id}#{index}`` with the chunk text as
    the value, so a vector connector embeds the text and a RetrievalNode can later
    attribute the chunk back to ``source_id``. (The connector ``write`` interface
    carries no metadata field, so source attribution travels via the key.)

    A07-23 -- **an ingest either lands whole or leaves nothing behind.** Writes
    go one chunk at a time into a backend with no transaction to join, so a
    failure partway does leave earlier chunks durably indexed for a moment; this
    function then deletes every key it wrote, including the key of the write
    that failed (which may have applied on the far side of the error), and
    raises :class:`IngestionRolledBackError`. Retry by re-offering the whole
    ``documents`` sequence -- nothing survived to resume from.

    Compensation is the only unwind this protocol offers and it can itself fail.
    When a delete fails, the leftovers are named rather than swallowed:
    :class:`PartialIngestionError` carries the orphaned keys and the delete
    errors for durable reconciliation. Both errors carry the ``cursor`` of the
    write that broke, and the connector's error as ``__cause__``.

    Two boundaries of the guarantee, stated rather than implied. It covers
    failures the connector *reports*: if this process dies mid-loop, or the
    ingest is cancelled, nothing gets to run the sweep and chunks stay indexed
    -- that needs durable reconciliation outside this call. And it is about the
    keys this call writes: re-ingesting a ``source_id`` that is already indexed
    overwrites its chunks in place, so a rollback deletes those keys rather than
    restoring their previous contents (``write`` takes no metadata and
    ``MemoryEntry`` timestamps are backend-assigned, so a pre-image cannot be
    put back faithfully, and a retry rewrites the document whole anyway). Keys
    the run never reached are left alone.
    """
    _require_compensable(connector)
    written: list[tuple[str, str]] = []
    for document_index, document in enumerate(documents):
        chunks = chunk_text(document.text, chunk_size=chunk_size, overlap=overlap)
        for index, chunk in enumerate(chunks):
            key = f"{document.source_id}#{index}"
            try:
                await connector.write(key, chunk, scope)
            except Exception as exc:
                error = await _interrupted(
                    connector,
                    # The failed write may have applied before it raised, so its
                    # own key joins the sweep.
                    [*written, (document.source_id, key)],
                    scope,
                    IngestionCursor(
                        document_index=document_index,
                        source_id=document.source_id,
                        chunk_index=index,
                    ),
                )
                raise error from exc
            written.append((document.source_id, key))
    return IngestionReport(documents=len(documents), chunks_written=len(written))
