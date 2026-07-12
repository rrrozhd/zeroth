"""Regression guard: cache re-key must not break cost-instrumentation wrapping.

After WS-F re-keyed ``LiteLLMProviderAdapter._clients`` from ``model`` to
``(model, tenant_id, key_fingerprint)``, the ``ainvoke`` surface is unchanged,
so the ``InstrumentedProviderAdapter`` wrapping done at
``orchestrator/runtime.py`` must still compose and cost must still fire.
"""

from __future__ import annotations

import inspect

import pytest
from langchain_core.messages import AIMessage

from zeroth.core.agent_runtime.provider import (
    LiteLLMProviderAdapter,
    ProviderRequest,
)
from zeroth.core.econ.adapter import InstrumentedProviderAdapter
from zeroth.core.econ.cost import CostEstimator


class _FakeSecretProvider:
    def resolve(self, secret_ref, *, tenant_id=None):
        return "sk-injected"

    def resolve_many(self, refs, *, tenant_id=None):
        return {r: "sk-injected" for r in refs}

    def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):
        return "sk-injected"


class _FakeChatClient:
    """Stands in for the leaf ChatLiteLLM so no network call is made."""

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(
            content="hello",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )


def test_litellm_adapter_still_satisfies_provider_adapter_protocol() -> None:
    adapter = LiteLLMProviderAdapter(secret_provider=_FakeSecretProvider(), tenant_id="acme")
    # Structural check: the ProviderAdapter contract is an async ainvoke
    # (the Protocol is not runtime_checkable, so verify the shape directly).
    assert hasattr(adapter, "ainvoke")
    assert inspect.iscoroutinefunction(adapter.ainvoke)

    # The re-key is intact: _get_client caches under (model, tenant, fingerprint).
    client = adapter._get_client("openai/gpt-4o")
    assert client is not None
    (only_key,) = adapter._clients
    assert only_key[0] == "openai/gpt-4o"
    assert only_key[1] == "acme"
    assert isinstance(only_key[2], str) and only_key[2] != ""


async def test_instrumented_wrapping_composes_and_cost_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LiteLLMProviderAdapter(secret_provider=_FakeSecretProvider(), tenant_id="acme")
    # Swap the leaf network client for a fake; the full LiteLLMProviderAdapter
    # ainvoke path (message conversion, token extraction, response build) runs.
    monkeypatch.setattr(adapter, "_get_client", lambda model: _FakeChatClient())

    wrapped = InstrumentedProviderAdapter(
        inner=adapter,
        regulus_client=None,  # cost still attributed locally, no event stream
        cost_estimator=CostEstimator(),
        node_id="agent-1",
        run_id="run-1",
        tenant_id="acme",
        deployment_ref="dep-1",
    )

    request = ProviderRequest(
        model_name="openai/gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
    )
    response = await wrapped.ainvoke(request)

    # Composition preserved: inner content flows through the wrapper.
    assert response.content == "hello"
    # Cost instrumentation fired: cost_usd was stamped from the local estimate.
    assert response.cost_usd is not None
    assert response.cost_usd > 0
