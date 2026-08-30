"""Portable filesystem boundaries for live-evaluation helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved checkout and external state locations for an evaluation run."""

    worktree: Path
    state_root: Path


def resolve_runtime_paths(environ: Mapping[str, str] | None = None) -> RuntimePaths:
    """Resolve portable defaults with explicit operator overrides.

    Evaluation state remains outside the repository by default. CI and operators
    can relocate either boundary without modifying committed source files.
    """
    values = os.environ if environ is None else environ
    worktree = Path(
        values.get("ZEROTH_EVALUATION_WORKTREE", Path(__file__).resolve().parents[2])
    )
    state_root = Path(
        values.get(
            "ZEROTH_EVALUATION_STATE_ROOT",
            Path.home() / ".local/share/zeroth/evaluations/evaluation-studio-v1",
        )
    )
    return RuntimePaths(
        worktree=worktree.expanduser().resolve(strict=False),
        state_root=state_root.expanduser().resolve(strict=False),
    )
