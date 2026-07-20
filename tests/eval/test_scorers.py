"""Tests for eval scorers (EVAL-02, EVAL-03), with depth on judge error handling."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.runtime.agents.provider import ProviderResponse
from zeroth.eval import (
    ContainsScorer,
    EvalCase,
    ExactMatchScorer,
    JudgeVerdict,
    LLMJudgeScorer,
    PredicateScorer,
    SchemaScorer,
)


def _case(expected=None, **kwargs) -> EvalCase:
    return EvalCase(id="c", input={"q": "hi"}, expected=expected, **kwargs)


# --- Deterministic scorers (EVAL-03) -- light coverage; logic is total ----------


@pytest.mark.asyncio
async def test_exact_match_on_field() -> None:
    scorer = ExactMatchScorer(field="answer")
    assert (await scorer.score({"answer": "42"}, _case(expected="42"))).passed is True
    assert (await scorer.score({"answer": "no"}, _case(expected="42"))).passed is False


@pytest.mark.asyncio
async def test_contains_is_case_insensitive_by_default() -> None:
    result = await ContainsScorer().score("The ANSWER is here", _case(expected="answer"))
    assert result.passed is True


@pytest.mark.asyncio
async def test_schema_scorer_fail_is_quality_fail_not_error() -> None:
    class Out(BaseModel):
        n: int

    result = await SchemaScorer(Out).score({"n": "not-an-int"}, _case())
    assert result.passed is False
    assert result.errored is False  # validation failure is a quality fail, not a harness error


@pytest.mark.asyncio
async def test_predicate_crash_is_errored_not_failed() -> None:
    def boom(output, case):  # noqa: ANN001
        raise RuntimeError("predicate exploded")

    result = await PredicateScorer(boom).score({}, _case())
    assert result.errored is True
    assert result.passed is False


# --- LLM judge (EVAL-02) -- the parts where "passes tests but wrong" lives -------


class _FakeProvider:
    def __init__(self, response=None, *, raises=None):
        self._response = response
        self._raises = raises

    async def ainvoke(self, request):  # noqa: ANN001
        if self._raises is not None:
            raise self._raises
        return self._response


def _judge(provider, *, pass_threshold=0.7) -> LLMJudgeScorer:
    return LLMJudgeScorer(
        provider, "openai/gpt-4o", rubric="grade it", pass_threshold=pass_threshold
    )


@pytest.mark.asyncio
async def test_judge_parses_json_verdict_and_applies_threshold() -> None:
    provider = _FakeProvider(ProviderResponse(content='{"score": 0.9, "rationale": "great"}'))
    result = await _judge(provider).score("the output", _case())
    assert result.value == 0.9
    assert result.passed is True
    assert result.rationale == "great"
    assert result.errored is False


@pytest.mark.asyncio
async def test_judge_below_threshold_fails_without_error() -> None:
    provider = _FakeProvider(
        ProviderResponse(content=JudgeVerdict(score=0.5, rationale="weak"))  # typed content
    )
    result = await _judge(provider).score("weak output", _case())
    assert result.value == 0.5
    assert result.passed is False
    assert result.errored is False


@pytest.mark.asyncio
async def test_judge_provider_failure_is_errored_not_zero() -> None:
    provider = _FakeProvider(raises=RuntimeError("503 service unavailable"))
    result = await _judge(provider).score("out", _case())
    assert result.errored is True  # a flaky judge must not read as a quality regression
    assert result.passed is False
    assert "judge failed" in result.error


@pytest.mark.asyncio
async def test_judge_unparseable_content_is_errored() -> None:
    provider = _FakeProvider(ProviderResponse(content="totally not json"))
    result = await _judge(provider).score("out", _case())
    assert result.errored is True
    assert result.passed is False


@pytest.mark.asyncio
async def test_judge_extracts_json_embedded_in_prose() -> None:
    provider = _FakeProvider(
        ProviderResponse(
            content='Here is my grade:\n```json\n{"score": 0.8, "rationale": "ok"}\n```'
        )
    )
    result = await _judge(provider).score("out", _case())
    assert result.value == 0.8
    assert result.passed is True
