"""Caller-owned execution inventory; agreement is not independent source truth.

Mirrored in the standalone SDK and checked for contract/digest parity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MAX_INVENTORY_RUNS = 5_000
MAX_WINDOW_EXECUTIONS = 50_000


def execution_ids_digest(ids: list[str]) -> str:
    """Hash exact unique IDs in UTF-8 byte order, shared with explicit HTTP callers."""
    if any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in ids):
        raise ValueError("execution IDs must be nonempty strings of at most 128 characters")
    if len(set(ids)) != len(ids):
        raise ValueError("execution IDs must be unique")
    encoded = json.dumps(
        sorted(ids, key=lambda value: value.encode("utf-8")),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RunInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    terminal_state: Literal["completed", "failed", "cancelled"]
    execution_count: int = Field(ge=0, le=MAX_WINDOW_EXECUTIONS, strict=True)
    execution_ids_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _empty_inventory(self) -> RunInventory:
        self.run_id.encode("utf-8")
        if self.execution_count == 0 and self.execution_ids_digest != execution_ids_digest([]):
            raise ValueError("zero executions requires the empty ID digest")
        return self


class SourceWindowInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["source-inventory/1"] = "source-inventory/1"
    source_window_id: str = Field(min_length=1, max_length=128)
    opened_at: AwareDatetime
    closed_at: AwareDatetime
    runs: list[RunInventory] = Field(max_length=MAX_INVENTORY_RUNS)

    @model_validator(mode="after")
    def _closed_inventory(self) -> SourceWindowInventory:
        self.source_window_id.encode("utf-8")
        self.opened_at = self.opened_at.astimezone(UTC)
        self.closed_at = self.closed_at.astimezone(UTC)
        if self.closed_at < self.opened_at:
            raise ValueError("closed_at must be at or after opened_at")
        if len({run.run_id for run in self.runs}) != len(self.runs):
            raise ValueError("inventory run IDs must be unique")
        if sum(run.execution_count for run in self.runs) > MAX_WINDOW_EXECUTIONS:
            raise ValueError("inventory exceeds 50000 expected executions")
        self.runs = sorted(self.runs, key=lambda run: run.run_id.encode("utf-8"))
        return self

    def digest(self) -> str:
        """Canonical request assertion, without retaining another raw inventory copy."""
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


def validate_source_windows(
    value: dict[Literal["baseline", "candidate"], SourceWindowInventory],
) -> dict[Literal["baseline", "candidate"], SourceWindowInventory]:
    if value and set(value) != {"baseline", "candidate"}:
        raise ValueError("provide both baseline and candidate source windows, or neither")
    return value
