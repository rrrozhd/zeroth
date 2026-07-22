"""Integration tests for content-safety guardrails wired into AgentRunner (SAFE).

Covers flag (audit only), redact (rewrites the typed payload), and block (raises a
terminal AgentContentBlockedError that carries an audit_record) on both the input
and output boundaries.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.runtime.agents import (
    AgentConfig,
    AgentContentBlockedError,
    AgentRunner,
    ContentSafetyConfig,
    DeterministicProviderAdapter,
    ProviderResponse,
)


class DemoInput(BaseModel):
    query: str


class DemoOutput(BaseModel):
    answer: str


def _config(safety: ContentSafetyConfig) -> AgentConfig:
    return AgentConfig(
        name="demo",
        instruction="answer",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        content_safety=safety,
    )


def _provider(answer: str = "all good") -> DeterministicProviderAdapter:
    return DeterministicProviderAdapter([ProviderResponse(content=f'{{"answer":"{answer}"}}')])


@pytest.mark.asyncio
async def test_output_flag_mode_records_findings_without_changing_output() -> None:
    config = _config(ContentSafetyConfig(enabled=True, mode="flag"))
    provider = _provider("contact me at a@b.com")
    result = await AgentRunner(config, provider).run({"query": "hi"})

    # flag mode does not modify the output
    assert result.output_data == {"answer": "contact me at a@b.com"}
    findings = result.audit_record["content_safety"]["output"]["findings"]
    assert {"category": "pii:email", "count": 1} in findings


@pytest.mark.asyncio
async def test_output_redact_mode_rewrites_output_payload() -> None:
    config = _config(ContentSafetyConfig(enabled=True, mode="redact"))
    provider = _provider("contact me at a@b.com")
    result = await AgentRunner(config, provider).run({"query": "hi"})

    assert result.output_data == {"answer": "contact me at [REDACTED:email]"}
    assert result.audit_record["content_safety"]["output"]["findings"]
    # redaction must also scrub the persisted audit response — not just output_data
    assert "a@b.com" not in str(result.audit_record["response"])


@pytest.mark.asyncio
async def test_input_flag_mode_records_findings() -> None:
    config = _config(ContentSafetyConfig(enabled=True, mode="flag"))
    provider = _provider()
    result = await AgentRunner(config, provider).run({"query": "my email is a@b.com"})

    assert result.output_data == {"answer": "all good"}
    findings = result.audit_record["content_safety"]["input"]["findings"]
    assert {"category": "pii:email", "count": 1} in findings


@pytest.mark.asyncio
async def test_output_block_raises_terminal_error_with_audit_record() -> None:
    config = _config(ContentSafetyConfig(enabled=True, mode="block"))
    provider = _provider("ssn 123-45-6789")

    with pytest.raises(AgentContentBlockedError) as excinfo:
        await AgentRunner(config, provider).run({"query": "hi"})

    err = excinfo.value
    assert err.direction == "output"
    assert "pii:ssn" in err.findings
    # carries the audit_record the orchestrator persists as a rejected NodeAuditRecord
    assert err.audit_record["content_safety"]["output"]["blocked"] is True
    # terminal: the provider was invoked exactly once (no retry/wrapping)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_input_block_raises_before_provider_is_called() -> None:
    config = _config(ContentSafetyConfig(enabled=True, mode="block"))
    provider = _provider()

    with pytest.raises(AgentContentBlockedError) as excinfo:
        await AgentRunner(config, provider).run({"query": "my email is a@b.com"})

    assert excinfo.value.direction == "input"
    assert provider.requests == []  # blocked before any model call


@pytest.mark.asyncio
async def test_disabled_by_default_is_a_no_op() -> None:
    # No content_safety configured -> guardrail is off, output untouched, no audit key.
    config = AgentConfig(
        name="demo",
        instruction="answer",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
    )
    provider = _provider("contact me at a@b.com")
    result = await AgentRunner(config, provider).run({"query": "hi"})

    assert result.output_data == {"answer": "contact me at a@b.com"}
    assert "content_safety" not in result.audit_record
