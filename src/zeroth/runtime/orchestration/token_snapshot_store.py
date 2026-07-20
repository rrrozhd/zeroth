"""Runtime-owned persistence seam for atomic token-engine snapshots."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot


class TokenSnapshotConcurrencyError(RuntimeError):
    """A snapshot CAS lost to a different persisted revision."""

    def __init__(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        actual_revision: int | None,
    ) -> None:
        self.run_id = run_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"token snapshot CAS for {run_id!r} expected revision "
            f"{expected_revision!r}, found {actual_revision!r}"
        )


class TokenSnapshotTransitionError(ValueError):
    """A proposed snapshot violates monotonic transition invariants."""


class TokenSnapshotCorruptionError(RuntimeError):
    """Persisted row metadata contradicts its serialized snapshot."""


@runtime_checkable
class TokenSnapshotStore(Protocol):
    """Persistence operations the token scheduler drives."""

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None: ...

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot: ...


__all__ = [
    "TokenSnapshotConcurrencyError",
    "TokenSnapshotCorruptionError",
    "TokenSnapshotStore",
    "TokenSnapshotTransitionError",
]
