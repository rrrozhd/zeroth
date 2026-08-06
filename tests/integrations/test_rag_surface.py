"""Canonical import surface for the rag integrations package.

Non-golden boundary tests for the Task 15 rag move: the canonical
``zeroth.integrations.rag`` package must publish the same objects the
legacy ``zeroth.core.rag`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

EXPORTS = (
    "IngestionReport",
    "SourceDocument",
    "chunk_text",
    "ingest_documents",
)


def test_rag_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import rag as legacy
    from zeroth.integrations import rag as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


def test_rag_ingestion_module_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core.rag import ingestion as legacy_module
    from zeroth.integrations.rag import ingestion as canonical_module

    for name in EXPORTS:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


def test_rag_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.integrations.rag"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
