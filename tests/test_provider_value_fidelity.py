from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, text

from zeroth.contracts.governed import RunStatus
from zeroth.econ.analytics.waste import analyze_run
from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.capabilities.service import pick_ab_arm
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate
from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage
from zeroth.runtime.agents.provider import (
    DeterministicProviderAdapter,
    LiteLLMProviderAdapter,
    ProviderResponse,
)
from zeroth.runtime.agents.models import AgentConfig
from zeroth.runtime.agents.runner import AgentRunner
from zeroth.runtime.context.models import CompactionResult, ContextWindowSettings
from zeroth.runtime.context.strategies import LLMSummarizationStrategy, ObservationMaskingStrategy
from zeroth.runtime.orchestration.parallel_executor import sum_run_cost
from zeroth.runtime.runs import Run, RunHistoryEntry


def test_execution_event_defaults_to_explicit_unmeasured_values() -> None:
    event = ExecutionEvent(capability_id="cap", implementation_id="impl")
    assert event.cost_measurement is MeasurementState.UNMEASURED
    assert event.usage_measurement is MeasurementState.UNMEASURED
    assert event.token_cost_usd is None
    assert event.tool_cost_usd is None
    assert event.compute_cost_usd is None

    with pytest.raises(ValidationError, match="unmeasured cost"):
        ExecutionEvent(
            capability_id="cap",
            implementation_id="impl",
            cost_measurement=MeasurementState.UNMEASURED,
            token_cost_usd=Decimal("0"),
        )


def test_plane_event_preserves_unmeasured_instead_of_defaulting_to_zero() -> None:
    event = ExecutionEventCreate(
        execution_id="e",
        timestamp="2026-08-12T00:00:00Z",
        capability_id="cap",
        implementation_id="impl",
        model_version="m",
    )
    assert event.cost_measurement is MeasurementState.UNMEASURED
    assert event.token_cost_usd is None


def test_missing_total_token_usage_is_unmeasured() -> None:
    adapter = LiteLLMProviderAdapter()
    message = AIMessage(
        content="ok",
        response_metadata={"token_usage": {"prompt_tokens": 2, "completion_tokens": 3}},
    )
    assert adapter._extract_token_usage(message, "m") is None


async def test_summarization_carries_provider_measurement_without_emitting() -> None:
    class Provider:
        async def ainvoke(self, request):  # noqa: ANN001
            return ProviderResponse(
                content="summary",
                token_usage=TokenUsage(
                    input_tokens=2, output_tokens=3, total_tokens=5, model_name="m"
                ),
                cost_usd=0.0,
                cost_measurement=MeasurementState.MEASURED,
            )

    result = await LLMSummarizationStrategy(Provider()).compact(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "old"}],
        settings=ContextWindowSettings(
            max_context_tokens=10,
            preserve_recent_messages_count=0,
        ),
        model_name="m",
    )
    assert result.token_usage.total_tokens == 5
    assert result.cost_usd == 0.0
    assert result.cost_measurement is MeasurementState.MEASURED


async def test_compaction_measurement_is_promoted_into_the_run_audit() -> None:
    class Input(BaseModel):
        query: str

    class Output(BaseModel):
        answer: str

    compaction = CompactionResult(
        messages=[{"role": "user", "content": "compacted"}],
        original_count=2,
        compacted_count=1,
        tokens_before=20,
        tokens_after=10,
        strategy_name="llm_summarization",
        token_usage=TokenUsage(input_tokens=2, output_tokens=3, total_tokens=5, model_name="m"),
        estimated_cost_usd=0.25,
        cost_measurement=MeasurementState.ESTIMATED,
    )
    tracker = AsyncMock()
    tracker.maybe_compact = AsyncMock(return_value=(compaction.messages, compaction))
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content='{"answer":"ok"}',
                token_usage=TokenUsage(
                    input_tokens=7, output_tokens=11, total_tokens=18, model_name="m"
                ),
                cost_usd=0.0,
                cost_measurement=MeasurementState.MEASURED,
            )
        ]
    )
    runner = AgentRunner(
        AgentConfig(
            name="fidelity",
            instruction="answer",
            model_name="m",
            input_model=Input,
            output_model=Output,
        ),
        provider,
        context_tracker=tracker,
    )

    result = await runner.run({"query": "hi"})

    assert result.audit_record["token_usage"]["total_tokens"] == 23
    assert result.audit_record["cost_usd"] == 0.0
    assert result.audit_record["estimated_cost_usd"] == 0.25
    assert result.audit_record["cost_measurement"] is MeasurementState.ESTIMATED


async def test_masking_preserves_tool_message_identity_and_artifact() -> None:
    original = ToolMessage(
        content="secret",
        tool_call_id="call-1",
        name="lookup",
        id="message-1",
        status="success",
        artifact={"raw": True},
    )
    result = await ObservationMaskingStrategy().compact(
        [original],
        settings=ContextWindowSettings(
            max_context_tokens=10,
            preserve_recent_messages_count=0,
        ),
        model_name="m",
    )
    [masked] = result.messages
    assert isinstance(masked, ToolMessage)
    assert (masked.name, masked.tool_call_id, masked.id, masked.status, masked.artifact) == (
        "lookup",
        "call-1",
        "message-1",
        "success",
        {"raw": True},
    )
    assert masked.content != "secret"


def test_run_cost_refuses_unmeasured_but_accepts_measured_zero() -> None:
    run = Run(graph_version_ref="g", deployment_ref="d")
    run.execution_history = [
        RunHistoryEntry(
            node_id="n",
            status="completed",
            cost_measurement=MeasurementState.UNMEASURED,
        )
    ]
    assert sum_run_cost(run) is None

    run.execution_history[0] = RunHistoryEntry(
        node_id="n",
        status="completed",
        cost_usd=0.0,
        cost_measurement=MeasurementState.MEASURED,
    )
    assert sum_run_cost(run) == 0.0


def test_waste_excludes_estimates_from_recorded_denominator_and_flags_unknown() -> None:
    common = {
        "run_id": "r",
        "graph_version_ref": "g",
        "deployment_ref": "d",
        "status": "completed",
    }
    audits = [
        NodeAuditRecord(
            audit_id="measured",
            node_id="a",
            cost_usd=0.0,
            cost_measurement=MeasurementState.MEASURED,
            **common,
        ),
        NodeAuditRecord(
            audit_id="estimated",
            node_id="b",
            estimated_cost_usd=2.0,
            cost_measurement=MeasurementState.ESTIMATED,
            **common,
        ),
        NodeAuditRecord(
            audit_id="unknown",
            node_id="c",
            cost_measurement=MeasurementState.UNMEASURED,
            **common,
        ),
    ]
    report = analyze_run("r", RunStatus.COMPLETED, audits)
    assert report.total_cost_usd == 0.0
    assert report.estimated_cost_usd == 2.0
    assert report.cost_measurement_complete is False


def test_fractional_ab_target_is_not_truncated() -> None:
    import hashlib

    join_key = next(
        str(i)
        for i in range(100_000)
        if int(hashlib.sha256(str(i).encode()).hexdigest()[:8], 16) % 10_000 == 5_050
    )
    assert pick_ab_arm(join_key, 50.51) == "A"
    assert pick_ab_arm(join_key, 50.50) == "B"


def test_execution_measurement_migration_backfills_legacy_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'econ.db'}"
    monkeypatch.setenv("ECP_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE execution_events (id INTEGER PRIMARY KEY, "
                "execution_id VARCHAR(64) NOT NULL, tenant_id VARCHAR(128), "
                "join_key VARCHAR(128), timestamp DATETIME NOT NULL, "
                "capability_id VARCHAR(128) NOT NULL, implementation_id VARCHAR(128) NOT NULL, "
                "model_version VARCHAR(128) NOT NULL, token_cost_usd NUMERIC(12, 4) NOT NULL, "
                "tool_cost_usd NUMERIC(12, 4) NOT NULL, "
                "compute_cost_usd NUMERIC(12, 4) NOT NULL, latency_ms INTEGER NOT NULL, "
                "compute_time_ms INTEGER NOT NULL, metadata JSON NOT NULL)"
            )
        )
    command.stamp(config, "20260712_03")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(execution_id, tenant_id, join_key, timestamp, capability_id, "
                "implementation_id, model_version, token_cost_usd, tool_cost_usd, "
                "compute_cost_usd, latency_ms, compute_time_ms, metadata) VALUES "
                "('legacy', 't', 'j', CURRENT_TIMESTAMP, 'c', 'i', 'm', 0, 0, 0, 0, 0, '{}')"
            )
        )
    command.upgrade(config, "20260812_04")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT cost_measurement, usage_measurement FROM execution_events "
                "WHERE execution_id = 'legacy'"
            )
        ).one()
    assert row == ("measured", "unmeasured")
