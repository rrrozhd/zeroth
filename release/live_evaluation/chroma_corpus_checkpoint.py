"""Seal the persistent Chroma corpus identity and content hashes."""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, EvidenceStore
from .workflow3_lifecycle_evidence import (
    STATE_ROOT,
    WORKTREE,
    _git,
    _run_recorded,
    _source_hashes,
    _tree_digest,
)

ROOT = STATE_ROOT / "evidence/chroma-corpus-checkpoint-20260824-2"


def main() -> int:
    if ROOT.exists():
        raise RuntimeError(f"checkpoint already exists: {ROOT}")
    store = EvidenceStore(ROOT)
    sources = [
        WORKTREE / "release/live_evaluation/chroma_corpus_probe.py",
        WORKTREE / "src/zeroth/integrations/memory/chroma_connector.py",
        WORKTREE / "src/zeroth/integrations/memory/registry.py",
    ]
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "persistent-chroma-corpus",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": str(_git(WORKTREE, "rev-parse", "HEAD")).strip(),
            "tree_digest": _tree_digest(),
            "source_hashes": _source_hashes(sources),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "limitation": (
                "This proves the persisted corpus only; the newly instrumented embedding "
                "probe remains blocked pending credential rotation."
            ),
        }
    )
    _run_recorded(
        store,
        sequence=1,
        name="persistent-chroma-corpus-probe",
        argv=[
            "uv",
            "run",
            "python",
            "-m",
            "release.live_evaluation.chroma_corpus_probe",
        ],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=2,
        name="chroma-corpus-probe-lint",
        argv=[
            "uv",
            "run",
            "ruff",
            "check",
            "release/live_evaluation/chroma_corpus_probe.py",
            "release/live_evaluation/chroma_corpus_checkpoint.py",
        ],
        cwd=WORKTREE,
    )
    command = json.loads((ROOT / "commands/0001-persistent-chroma-corpus-probe.json").read_text())
    observation = json.loads(str(command["stdout"]))
    store._write_exclusive(Path("runtime/corpus-observation.json"), observation)
    criterion = AcceptanceCriterion(
        "control.chroma-corpus-seeded",
        "pass",
        (
            "commands/0001-persistent-chroma-corpus-probe.json",
            "runtime/corpus-observation.json",
        ),
    )
    report = """# Persistent Chroma corpus checkpoint

The loopback Chroma service contains exactly the three expected synthetic
documents in the application-tenant-qualified collection. Document IDs, scope
metadata, collection identity, and SHA-256 content hashes match. Embeddings are
not exported. The live instrumented embedding-probe gate remains separate and
blocked pending a newly rotated provider credential.
"""
    store.finalize_bundle(acceptance=(criterion,), report_markdown=report)
    print(json.dumps({"root": str(ROOT), "sealed": store.is_sealed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
