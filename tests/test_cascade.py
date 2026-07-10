"""Tests for the cost-cascade provider adapter (ECON-CASCADE-01)."""

from __future__ import annotations

import pytest
from governai.integrations.tool_calls import NormalizedToolCall

from zeroth.core.agent_runtime.cascade import CascadingProviderAdapter, _is_blank_response
from zeroth.core.agent_runtime.provider import ProviderRequest, ProviderResponse


class _ByModel:
    """Inner adapter returning a scripted response (or raising) keyed by model_name."""

    def __init__(self, by_model: dict):
        self._by_model = by_model
        self.calls: list[str] = []

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request.model_name)
        item = self._by_model[request.model_name]
        if isinstance(item, Exception):
            raise item
        return item


def _req(model: str = "openai/gpt-4o") -> ProviderRequest:
    return ProviderRequest(model_name=model, messages=[{"role": "user", "content": "hi"}])


def _resp(content, *, cost=0.0, event="e", tool_calls=None) -> ProviderResponse:
    return ProviderResponse(
        content=content, cost_usd=cost, cost_event_id=event, tool_calls=tool_calls or []
    )


async def test_cheap_success_serves_cheap_without_escalation():
    inner = _ByModel({"cheap": _resp("answer", cost=0.001, event="c1")})
    out = await CascadingProviderAdapter(inner, cheap_model="cheap").ainvoke(_req("incumbent"))
    assert out.content == "answer"
    assert inner.calls == ["cheap"]  # incumbent never called
    assert out.metadata["cascade"]["served_by"] == "cheap"
    assert out.metadata["cascade"]["escalated"] is False


async def test_blank_cheap_escalates_and_sums_both_costs():
    inner = _ByModel(
        {
            "cheap": _resp("   ", cost=0.001, event="c1"),  # whitespace = blank
            "incumbent": _resp("real answer", cost=0.01, event="i1"),
        }
    )
    out = await CascadingProviderAdapter(inner, cheap_model="cheap").ainvoke(_req("incumbent"))
    assert out.content == "real answer"
    assert inner.calls == ["cheap", "incumbent"]
    assert out.cost_usd == 0.011  # every dollar attributed: cheap attempt + incumbent
    cascade = out.metadata["cascade"]
    assert cascade["served_by"] == "incumbent"
    assert cascade["escalated"] is True
    assert cascade["primary_failure"] == "blank"
    assert cascade["primary_cost_usd"] == 0.001


async def test_cheap_error_escalates_with_zero_primary_cost():
    inner = _ByModel(
        {
            "cheap": RuntimeError("boom"),
            "incumbent": _resp("real answer", cost=0.01, event="i1"),
        }
    )
    out = await CascadingProviderAdapter(inner, cheap_model="cheap").ainvoke(_req("incumbent"))
    assert out.content == "real answer"
    assert out.cost_usd == 0.01  # a raised call has no measurable cost
    assert out.metadata["cascade"]["primary_failure"] == "error"
    assert out.metadata["cascade"]["primary_cost_usd"] == 0.0


async def test_tool_call_turn_is_not_treated_as_blank():
    # A tool-call turn has content=None but is a valid intermediate step — serve the cheap model.
    tool_turn = _resp(None, cost=0.001, tool_calls=[NormalizedToolCall(id="1", name="t", args={})])
    inner = _ByModel({"cheap": tool_turn})
    out = await CascadingProviderAdapter(inner, cheap_model="cheap").ainvoke(_req("incumbent"))
    assert inner.calls == ["cheap"]  # not escalated
    assert out.metadata["cascade"]["escalated"] is False


async def test_both_models_fail_propagates_the_exception():
    inner = _ByModel(
        {
            "cheap": _resp(None, cost=0.0),  # blank
            "incumbent": RuntimeError("incumbent down"),
        }
    )
    with pytest.raises(RuntimeError, match="incumbent down"):
        await CascadingProviderAdapter(inner, cheap_model="cheap").ainvoke(_req("incumbent"))


def test_is_blank_response_definition():
    assert _is_blank_response(_resp(None)) is True
    assert _is_blank_response(_resp("   ")) is True
    assert _is_blank_response(_resp("x")) is False
    assert _is_blank_response(_resp({"field": 1})) is False  # structured content is not blank
    assert _is_blank_response(_resp(None, tool_calls=[NormalizedToolCall(id="1", name="t", args={})])) is False
