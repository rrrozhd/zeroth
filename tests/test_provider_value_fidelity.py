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
from sqlalchemy.orm import sessionmaker

from zeroth.contracts.governed import RunStatus
from zeroth.econ.analytics.budget import BudgetCheckResult, BudgetEnforcer
from zeroth.econ.analytics.waste import analyze_run
from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.capabilities.service import pick_ab_arm
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate
from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage
from zeroth.runtime.agents.provider import (
    CallableProviderAdapter,
    DeterministicProviderAdapter,
    LiteLLMProviderAdapter,
    ProviderResponse,
)
from zeroth.runtime.agents.errors import (
    AgentOutputValidationError,
    AgentProviderError,
    AgentTimeoutError,
    BudgetExceededError,
)
from zeroth.runtime.agents.models import AgentConfig, RetryPolicy
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


def test_usage_metadata_without_explicit_total_is_unmeasured() -> None:
    adapter = LiteLLMProviderAdapter()
    message = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    assert message.usage_metadata is not None
    message.usage_metadata.pop("total_tokens")
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


def _compaction_with_estimated_cost(cost: float = 0.25) -> CompactionResult:
    return CompactionResult(
        messages=[{"role": "user", "content": "compacted"}],
        original_count=2,
        compacted_count=1,
        tokens_before=20,
        tokens_after=10,
        strategy_name="llm_summarization",
        token_usage=TokenUsage(input_tokens=2, output_tokens=3, total_tokens=5, model_name="m"),
        estimated_cost_usd=cost,
        cost_measurement=MeasurementState.ESTIMATED,
    )


def _runner_with_compaction(provider, *, budget_enforcer=None) -> AgentRunner:  # noqa: ANN001
    class Input(BaseModel):
        query: str

    class Output(BaseModel):
        answer: str

    tracker = AsyncMock()
    compaction = _compaction_with_estimated_cost()
    tracker.maybe_compact = AsyncMock(return_value=(compaction.messages, compaction))
    return AgentRunner(
        AgentConfig(
            name="fidelity-failure",
            instruction="answer",
            model_name="m",
            input_model=Input,
            output_model=Output,
        ),
        provider,
        context_tracker=tracker,
        budget_enforcer=budget_enforcer,
    )


async def test_compaction_measurement_survives_provider_failure() -> None:
    async def fail(_request):  # noqa: ANN001
        raise RuntimeError("provider failed")

    with pytest.raises(AgentProviderError) as raised:
        await _runner_with_compaction(CallableProviderAdapter(fail)).run({"query": "hi"})

    assert raised.value.audit_record["estimated_cost_usd"] == 0.25
    assert raised.value.audit_record["token_usage"]["total_tokens"] == 5


async def test_compaction_measurement_survives_output_failure() -> None:
    runner = _runner_with_compaction(
        DeterministicProviderAdapter(
            [
                ProviderResponse(
                    content='{"wrong":"shape"}',
                    cost_usd=1.0,
                    cost_measurement=MeasurementState.MEASURED,
                )
            ]
        )
    )

    with pytest.raises(AgentOutputValidationError) as raised:
        await runner.run({"query": "hi"})

    assert raised.value.audit_record["cost_usd"] == 1.0
    assert raised.value.audit_record["estimated_cost_usd"] == 0.25


async def test_validation_retry_keeps_every_provider_measurement() -> None:
    class Input(BaseModel):
        query: str

    class Output(BaseModel):
        answer: str

    runner = AgentRunner(
        AgentConfig(
            name="fidelity-retry",
            instruction="answer",
            model_name="m",
            input_model=Input,
            output_model=Output,
            retry_policy=RetryPolicy(max_retries=1, use_exponential_backoff=False),
        ),
        DeterministicProviderAdapter(
            [
                ProviderResponse(
                    content='{"wrong":"shape"}',
                    token_usage=TokenUsage(
                        input_tokens=2, output_tokens=3, total_tokens=5, model_name="m"
                    ),
                    cost_usd=0.2,
                    cost_measurement=MeasurementState.MEASURED,
                ),
                ProviderResponse(
                    content='{"answer":"ok"}',
                    token_usage=TokenUsage(
                        input_tokens=7, output_tokens=11, total_tokens=18, model_name="m"
                    ),
                    estimated_cost_usd=0.3,
                    cost_measurement=MeasurementState.ESTIMATED,
                ),
            ]
        ),
    )

    result = await runner.run({"query": "hi"})

    assert result.audit_record["cost_usd"] == 0.2
    assert result.audit_record["estimated_cost_usd"] == 0.3
    assert result.audit_record["cost_measurement"] is MeasurementState.ESTIMATED
    assert result.audit_record["token_usage"]["total_tokens"] == 23
    assert result.audit_record["token_usage"]["model_name"] == "m"


async def test_failed_provider_retry_keeps_measurement_incomplete() -> None:
    class Input(BaseModel):
        query: str

    class Output(BaseModel):
        answer: str

    transient = RuntimeError("provider unavailable")
    transient.status_code = 503  # type: ignore[attr-defined]
    runner = AgentRunner(
        AgentConfig(
            name="fidelity-failed-retry",
            instruction="answer",
            model_name="m",
            input_model=Input,
            output_model=Output,
            retry_policy=RetryPolicy(max_retries=1, use_exponential_backoff=False),
        ),
        DeterministicProviderAdapter(
            [
                transient,
                ProviderResponse(
                    content='{"answer":"ok"}',
                    token_usage=TokenUsage(
                        input_tokens=2, output_tokens=3, total_tokens=5, model_name="m"
                    ),
                    cost_usd=0.2,
                    cost_measurement=MeasurementState.MEASURED,
                ),
            ]
        ),
    )

    result = await runner.run({"query": "hi"})

    assert result.audit_record["cost_usd"] == 0.2
    assert result.audit_record["cost_measurement"] is MeasurementState.UNMEASURED
    assert result.audit_record["usage_measurement"] is MeasurementState.UNMEASURED


@pytest.mark.parametrize(
    ("terminal_error", "expected_error"),
    [
        (TimeoutError(), AgentTimeoutError),
        (RuntimeError("provider unavailable"), AgentProviderError),
    ],
)
async def test_terminal_provider_failure_after_paid_attempt_is_unmeasured(
    terminal_error: Exception,
    expected_error: type[Exception],
) -> None:
    class Input(BaseModel):
        query: str

    class Output(BaseModel):
        answer: str

    runner = AgentRunner(
        AgentConfig(
            name="fidelity-terminal-failure",
            instruction="answer",
            model_name="m",
            input_model=Input,
            output_model=Output,
            retry_policy=RetryPolicy(max_retries=1, use_exponential_backoff=False),
        ),
        DeterministicProviderAdapter(
            [
                ProviderResponse(
                    content='{"wrong":"shape"}',
                    token_usage=TokenUsage(
                        input_tokens=2, output_tokens=3, total_tokens=5, model_name="m"
                    ),
                    cost_usd=0.2,
                    cost_measurement=MeasurementState.MEASURED,
                ),
                terminal_error,
            ]
        ),
    )

    with pytest.raises(expected_error) as raised:
        await runner.run({"query": "hi"})

    assert raised.value.audit_record["cost_usd"] == pytest.approx(0.2)
    assert raised.value.audit_record["cost_measurement"] is MeasurementState.UNMEASURED
    assert raised.value.audit_record["token_usage"]["total_tokens"] == 5
    assert raised.value.audit_record["usage_measurement"] is MeasurementState.UNMEASURED


@pytest.mark.parametrize(
    ("post_response", "expected_state", "expected_cost"),
    [
        (True, MeasurementState.MEASURED, 0.2),
        (False, MeasurementState.UNMEASURED, None),
    ],
)
async def test_operational_failure_measurement_matches_provider_response(
    monkeypatch: pytest.MonkeyPatch,
    post_response: bool,
    expected_state: MeasurementState,
    expected_cost: float | None,
) -> None:
    class Input(BaseModel):
        query: str

    class Output(BaseModel):
        answer: str

    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content='{"answer":"ok"}',
                token_usage=TokenUsage(
                    input_tokens=2, output_tokens=3, total_tokens=5, model_name="m"
                ),
                cost_usd=0.2,
                cost_measurement=MeasurementState.MEASURED,
            )
            if post_response
            else RuntimeError("provider failed")
        ]
    )
    runner = AgentRunner(
        AgentConfig(
            name="fidelity-operational-failure",
            instruction="answer",
            model_name="m",
            input_model=Input,
            output_model=Output,
        ),
        provider,
    )
    if post_response:
        monkeypatch.setattr(
            runner,
            "_store_memory",
            AsyncMock(side_effect=RuntimeError("memory store failed")),
        )

    with pytest.raises(AgentProviderError) as raised:
        await runner.run({"query": "hi"})

    audit = raised.value.audit_record
    assert audit.get("cost_usd") == expected_cost
    assert audit["cost_measurement"] is expected_state
    assert audit["usage_measurement"] is expected_state
    assert audit.get("token_usage") == (
        {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5, "model_name": "m"}
        if post_response
        else None
    )


async def test_runner_preserves_degraded_budget_status() -> None:
    budget = AsyncMock(spec=BudgetEnforcer)
    status = BudgetCheckResult(
        allowed=True,
        spend_usd=0.0,
        cap_usd=None,
        degraded=True,
        failure_mode="fail_open",
        measurement_complete=False,
    )
    budget.check_budget_status = AsyncMock(return_value=status)
    runner = _runner_with_compaction(
        DeterministicProviderAdapter(
            [
                ProviderResponse(
                    content='{"answer":"ok"}',
                    cost_usd=0.0,
                    cost_measurement=MeasurementState.MEASURED,
                )
            ]
        ),
        budget_enforcer=budget,
    )

    result = await runner.run({"query": "hi"})

    budget.check_budget_status.assert_awaited_once_with("default")
    budget.check_budget.assert_not_awaited()
    assert result.audit_record["extra"]["budget_check"] == status.model_dump(mode="json")


async def test_compaction_measurement_survives_budget_rejection() -> None:
    budget = AsyncMock(spec=BudgetEnforcer)
    budget.check_budget_status = AsyncMock(
        return_value=BudgetCheckResult(allowed=False, spend_usd=2.0, cap_usd=1.0)
    )
    runner = _runner_with_compaction(DeterministicProviderAdapter([]), budget_enforcer=budget)

    with pytest.raises(BudgetExceededError) as raised:
        await runner.run({"query": "hi"})

    assert raised.value.audit_record["estimated_cost_usd"] == 0.25
    assert raised.value.audit_record["token_usage"]["total_tokens"] == 5


@pytest.mark.parametrize(
    ("payload", "expected_state", "expected_cost"),
    [
        (
            {"token_cost_usd": "1.25", "cost_measurement": "estimated"},
            MeasurementState.ESTIMATED,
            Decimal("1.25"),
        ),
        ({"token_cost_usd": None}, MeasurementState.UNMEASURED, None),
        (
            {"token_cost_usd": 0, "cost_measurement": "measured"},
            MeasurementState.MEASURED,
            Decimal("0"),
        ),
    ],
)
def test_langgraph_adapter_preserves_explicit_cost_provenance(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected_state: MeasurementState,
    expected_cost: Decimal | None,
) -> None:
    from zeroth.econ.instrumentation.langgraph import adapter as adapter_module

    captured = []
    monkeypatch.setattr(adapter_module, "track_execution", captured.append)

    adapter_module.LangGraphTelemetryAdapter().on_run_end("r", "cap", "impl", payload)

    [event] = captured
    assert event.token_cost_usd == expected_cost
    assert event.cost_measurement is expected_state


async def test_explicit_zero_estimate_is_retained() -> None:
    runner = _runner_with_compaction(
        DeterministicProviderAdapter(
            [
                ProviderResponse(
                    content='{"answer":"ok"}',
                    cost_usd=0.0,
                    cost_measurement=MeasurementState.MEASURED,
                )
            ]
        )
    )
    compaction = _compaction_with_estimated_cost(0.0)
    runner.context_tracker.maybe_compact = AsyncMock(return_value=(compaction.messages, compaction))

    result = await runner.run({"query": "hi"})

    assert result.audit_record["estimated_cost_usd"] == 0.0


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
        "tenant_id": "tenant_default",
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
    assert row == ("unmeasured", "unmeasured")


def test_migration_free_sqlite_startup_accepts_unmeasured_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zeroth.econ.plane import database as plane_database
    from zeroth.econ.plane.common import bootstrap as plane_bootstrap

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-econ.db'}", future=True)
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE execution_events (id INTEGER PRIMARY KEY, "
                "execution_id VARCHAR(128) NOT NULL, timestamp DATETIME NOT NULL, "
                "capability_id VARCHAR(128) NOT NULL, implementation_id VARCHAR(128) NOT NULL, "
                "model_version VARCHAR(128) NOT NULL, token_cost_usd NUMERIC(12, 4) NOT NULL, "
                "tool_cost_usd NUMERIC(12, 4) NOT NULL, "
                "compute_cost_usd NUMERIC(12, 4) NOT NULL, latency_ms INTEGER NOT NULL, "
                "compute_time_ms INTEGER NOT NULL, metadata JSON NOT NULL)"
            )
        )
        connection.execute(
            text("CREATE INDEX ix_legacy_execution_capability ON execution_events (capability_id)")
        )
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(execution_id, timestamp, capability_id, implementation_id, model_version, "
                "token_cost_usd, tool_cost_usd, compute_cost_usd, latency_ms, "
                "compute_time_ms, metadata) VALUES "
                "('legacy', CURRENT_TIMESTAMP, 'cap', 'impl', 'm', 1, 2, 3, 0, 0, '{}')"
            )
        )

    monkeypatch.setattr(plane_database, "engine", legacy_engine)
    monkeypatch.setattr(
        plane_database,
        "SessionLocal",
        sessionmaker(bind=legacy_engine, autocommit=False, autoflush=False),
    )
    monkeypatch.setattr(plane_bootstrap, "engine", legacy_engine)
    monkeypatch.setattr(plane_bootstrap, "SessionLocal", plane_database.SessionLocal)

    plane_bootstrap.bootstrap()

    with plane_database.SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO execution_events "
                "(tenant_id, execution_id, timestamp, capability_id, implementation_id, "
                "model_version, token_cost_usd, tool_cost_usd, compute_cost_usd, "
                "cost_measurement, usage_measurement, latency_ms, compute_time_ms, metadata) "
                "VALUES ('tenant_default', 'unmeasured', CURRENT_TIMESTAMP, 'cap', 'impl', "
                "'m', NULL, NULL, NULL, 'unmeasured', 'unmeasured', 0, 0, '{}')"
            )
        )
        db.commit()
    # A second startup is a no-op, including for the preserved custom index.
    plane_bootstrap.bootstrap()
    with legacy_engine.connect() as connection:
        columns = {
            row[1]: row[3]
            for row in connection.execute(text("PRAGMA table_info(execution_events)"))
        }
        execution_ids = connection.execute(
            text("SELECT execution_id FROM execution_events ORDER BY id")
        ).scalars().all()
        indexes = {
            row[1] for row in connection.execute(text("PRAGMA index_list(execution_events)"))
        }
    assert columns["token_cost_usd"] == 0
    assert columns["tool_cost_usd"] == 0
    assert columns["compute_cost_usd"] == 0
    assert execution_ids == ["legacy", "unmeasured"]
    assert "ix_legacy_execution_capability" in indexes
