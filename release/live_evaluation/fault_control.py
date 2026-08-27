"""Campaign-scoped deterministic provider fault state for local evaluation only."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.runtime.agents.provider import ProviderRequest, ProviderResponse

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_TARGETS = {
    "provider",
    "connector",
    "runtime",
    "ui",
    "action_sink",
    "action_outcome_lookup",
    "input",
}
_PROVIDER_MODES = {
    "invalid_secret_reference",
    "malformed_response",
    "rate_limit",
    "revision_required",
    "timeout",
}


@dataclass(frozen=True, slots=True)
class ArmedFault:
    fault_id: str
    campaign_id: str
    target: str
    mode: str
    parameters: dict[str, object]


class EvaluationProviderFaultError(RuntimeError):
    """A local deterministic fault that proves the provider was never called."""

    provider_call_attempted = False

    def __init__(self, mode: str) -> None:
        super().__init__(f"deterministic evaluation provider fault: {mode}")
        self.mode = mode


class EvaluationConnectorFaultError(RuntimeError):
    """A deterministic connector failure that never touches the real backend."""


class FaultArmRequest(BaseModel):
    """Exact out-of-band instruction emitted by the campaign HTTP backend."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
    operation_id: str = Field(min_length=1, max_length=192)
    run_id: str = Field(min_length=1, max_length=192)
    deterministic: bool
    target: str
    mode: str
    parameters: dict[str, object] = Field(default_factory=dict)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_parameters(parameters: dict[str, object] | None) -> str:
    try:
        encoded = json.dumps(parameters or {}, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("fault parameters must be JSON serializable") from exc
    if not isinstance(decoded, dict):
        raise ValueError("fault parameters must be an object")
    return encoded


class EvaluationFaultState:
    """SQLite-backed one-shot fault controller shared by local service processes."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve(strict=False)
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_faults (
                    fault_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    armed_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_evaluation_faults_active
                ON evaluation_faults (campaign_id, target, consumed_at, armed_at)
                """
            )

    def arm(
        self,
        *,
        campaign_id: str,
        target: str,
        mode: str,
        parameters: dict[str, object] | None = None,
    ) -> ArmedFault:
        if not _SAFE_ID.fullmatch(campaign_id):
            raise ValueError("unsafe campaign identity")
        if target not in _TARGETS:
            raise ValueError("unsupported fault target")
        if not _SAFE_ID.fullmatch(mode):
            raise ValueError("unsafe fault mode")
        uses = (parameters or {}).get("uses", 1)
        if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= 8:
            raise ValueError("fault uses must be an integer from one through eight")
        encoded = _json_parameters(parameters)
        fault = ArmedFault(uuid4().hex, campaign_id, target, mode, json.loads(encoded))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM evaluation_faults
                WHERE campaign_id = ? AND target = ? AND consumed_at IS NULL
                LIMIT 1
                """,
                (campaign_id, target),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise ValueError("fault target is already armed for this campaign")
            connection.execute(
                """
                INSERT INTO evaluation_faults
                    (fault_id, campaign_id, target, mode, parameters_json, armed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (fault.fault_id, campaign_id, target, mode, encoded, _utc_now()),
            )
            connection.commit()
        return fault

    def consume(self, *, campaign_id: str, target: str) -> ArmedFault | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT fault_id, campaign_id, target, mode, parameters_json
                FROM evaluation_faults
                WHERE campaign_id = ? AND target = ? AND consumed_at IS NULL
                ORDER BY armed_at, fault_id
                LIMIT 1
                """,
                (campaign_id, target),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            parameters = json.loads(row["parameters_json"])
            uses = parameters.get("uses", 1)
            if isinstance(uses, bool) or not isinstance(uses, int) or uses < 1:
                connection.rollback()
                raise RuntimeError("persisted evaluation fault has invalid uses")
            if uses == 1:
                updated = connection.execute(
                    """
                    UPDATE evaluation_faults SET consumed_at = ?
                    WHERE fault_id = ? AND consumed_at IS NULL
                    """,
                    (_utc_now(), row["fault_id"]),
                )
            else:
                parameters["uses"] = uses - 1
                updated = connection.execute(
                    """
                    UPDATE evaluation_faults SET parameters_json = ?
                    WHERE fault_id = ? AND consumed_at IS NULL
                    """,
                    (_json_parameters(parameters), row["fault_id"]),
                )
            if updated.rowcount != 1:
                connection.rollback()
                raise RuntimeError("fault consumption lost its atomic claim")
            connection.commit()
        return ArmedFault(
            fault_id=row["fault_id"],
            campaign_id=row["campaign_id"],
            target=row["target"],
            mode=row["mode"],
            parameters=json.loads(row["parameters_json"]),
        )


class FaultingProviderAdapter:
    """Intercept one armed campaign fault before delegating to a real provider."""

    def __init__(self, *, inner: Any, state: EvaluationFaultState) -> None:
        self.inner = inner
        self.state = state

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        runtime_context = request.metadata.get("runtime_context")
        campaign_id = (
            runtime_context.get("campaign_id")
            if isinstance(runtime_context, dict)
            else None
        )
        if not isinstance(campaign_id, str):
            return await self.inner.ainvoke(request)
        fault = self.state.consume(campaign_id=campaign_id, target="provider")
        if fault is None:
            return await self.inner.ainvoke(request)
        if fault.mode not in _PROVIDER_MODES:
            raise EvaluationProviderFaultError(f"unsupported:{fault.mode}")
        if fault.mode == "malformed_response":
            return ProviderResponse(
                content={"evaluation_malformed_response": True},
                metadata={
                    "cache_hit": True,
                    "evaluation_fault": fault.mode,
                    "evaluation_fault_id": fault.fault_id,
                },
            )
        if fault.mode == "revision_required":
            return ProviderResponse(
                content={
                    "query": "synthetic-excessive-revision",
                    "answer": "Deterministic evaluation revision requested.",
                    "source_ids": [],
                    "revision_required": True,
                    "revision_count": 1,
                },
                metadata={
                    "cache_hit": True,
                    "evaluation_fault": fault.mode,
                    "evaluation_fault_id": fault.fault_id,
                },
            )
        raise EvaluationProviderFaultError(fault.mode)


class _FaultingMemoryConnector:
    def __init__(
        self,
        *,
        inner: Any,
        state: EvaluationFaultState,
        campaign_id: str,
    ) -> None:
        self.inner = inner
        self.state = state
        self.campaign_id = campaign_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def search(self, query: Any, scope: Any, *, target: str | None = None) -> Any:
        fault = self.state.consume(campaign_id=self.campaign_id, target="connector")
        if fault is None:
            return await self.inner.search(query, scope, target=target)
        if fault.mode == "retrieval_miss":
            return []
        if fault.mode == "unavailable":
            raise EvaluationConnectorFaultError(
                "deterministic evaluation connector unavailable"
            )
        raise EvaluationConnectorFaultError(
            f"unsupported deterministic evaluation connector fault: {fault.mode}"
        )


class EvaluationFaultingMemoryResolver:
    """Wrap resolved campaign connectors without changing the global registry."""

    def __init__(self, *, inner: Any, state: EvaluationFaultState) -> None:
        self.inner = inner
        self.state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def resolve(self, memory_refs: list[str], **kwargs: Any) -> list[Any]:
        bindings = await self.inner.resolve(memory_refs, **kwargs)
        runtime_context = kwargs.get("runtime_context")
        campaign_id = (
            runtime_context.get("campaign_id")
            if isinstance(runtime_context, Mapping)
            else None
        )
        if not isinstance(campaign_id, str):
            return bindings
        wrapped: list[Any] = []
        for binding in bindings:
            connector = _FaultingMemoryConnector(
                inner=binding.connector,
                state=self.state,
                campaign_id=campaign_id,
            )
            model_copy = getattr(binding, "model_copy", None)
            if callable(model_copy):
                wrapped.append(model_copy(update={"connector": connector}))
            else:
                clone = copy(binding)
                clone.connector = connector
                wrapped.append(clone)
        return wrapped


def register_fault_control_routes(
    app: FastAPI | APIRouter,
    *,
    state: EvaluationFaultState,
    campaign_id: str,
) -> None:
    """Mount the local-only one-shot arming route on an evaluation service."""

    @app.post("/faults/arm", status_code=status.HTTP_204_NO_CONTENT)
    async def arm_fault(payload: FaultArmRequest) -> Response:
        if payload.campaign_id != campaign_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="fault campaign does not match the configured evaluation campaign",
            )
        if payload.deterministic is not True:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="evaluation fault must be deterministic",
            )
        try:
            fault = state.arm(
                campaign_id=payload.campaign_id,
                target=payload.target,
                mode=payload.mode,
                parameters=payload.parameters,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="evaluation fault could not be armed",
            ) from exc
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"X-Evaluation-Fault-ID": fault.fault_id},
        )
