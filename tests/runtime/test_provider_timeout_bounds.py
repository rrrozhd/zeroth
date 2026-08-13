"""An agent's LLM call always has a deadline (ZER-48 follow-up).

Measured before this change: ``AgentConfig.timeout_seconds`` defaults to ``None``
and so does the policy override, ``AgentRunner._effective_timeout`` returned
``None`` when neither named a value, and ``run_provider_with_timeout`` answered a
``None`` timeout by calling ``adapter.ainvoke(request)`` with no ``wait_for`` at
all.  An agent declared without an explicit timeout therefore called its provider
with no bound anywhere on the path.

The fix is in two places on purpose, so each has its own oracle here:

* the resolver stops returning ``None`` — covered by ``TestResolverAlwaysBounds``;
* the call site stops honouring ``None`` as "no limit" — covered by
  ``TestProviderCallAlwaysBounded``, which drives a provider that never answers.

Reverting either one alone must fail its own class. A test that only drove the
whole chain would stay green with the resolver reverted, because the call site
would silently cover for it.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, cast

import pytest

from zeroth.runtime.agents.provider import (
    DEFAULT_AGENT_PROVIDER_TIMEOUT_SECONDS,
    ProviderRequest,
    ProviderResponse,
    resolve_provider_timeout,
    run_provider_with_timeout,
)
from zeroth.runtime.agents.runner import AgentRunner

#: Narrowed so a test can prove the deadline fires without waiting ten minutes.
_NARROW_DEADLINE_SECONDS = 0.2

#: Comfortably longer than the narrowed deadline, so "the deadline fired" and
#: "the provider answered" are distinguishable rather than a race.
_PROVIDER_LATENCY_SECONDS = 5.0


class _NeverAnswers:
    """A provider adapter that accepts the call and then never returns."""

    def __init__(self, latency: float = _PROVIDER_LATENCY_SECONDS) -> None:
        self._latency = latency
        self.calls = 0

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Sleep past any sane deadline, then answer."""
        del request
        self.calls += 1
        await asyncio.sleep(self._latency)
        return ProviderResponse(content="too late")


def _request() -> ProviderRequest:
    return ProviderRequest(model_name="openai/gpt-4o")


# --- oracle for the resolver -------------------------------------------------


class TestResolverAlwaysBounds:
    """``_effective_timeout`` returns a number, never ``None``.

    ``_effective_timeout`` reads nothing off ``self``, so it is exercised
    unbound rather than by standing up a whole runner.
    """

    @staticmethod
    def _resolve(configured: float | None, policy: float | None) -> float:
        return AgentRunner._effective_timeout(cast(Any, None), configured, policy)

    def test_nothing_configured_still_yields_a_deadline(self) -> None:
        """The exact reachable default: neither the agent nor the policy names one."""
        resolved = self._resolve(None, None)

        assert resolved is not None, "an unconfigured agent got no provider deadline"
        assert math.isfinite(resolved) and resolved > 0

    def test_a_zero_configured_timeout_is_discarded(self) -> None:
        """``AgentConfig`` declares ``ge=0.0``, so ``0`` is authorable.

        Honoured literally it would cancel every provider call the instant it
        started, which is a different failure from the one being fixed but
        reachable through the same field.
        """
        assert self._resolve(0.0, None) == DEFAULT_AGENT_PROVIDER_TIMEOUT_SECONDS

    def test_an_infinite_configured_timeout_is_discarded(self) -> None:
        """``wait_for`` reads ``inf`` as no deadline, so it is not a bound."""
        assert self._resolve(math.inf, None) == DEFAULT_AGENT_PROVIDER_TIMEOUT_SECONDS

    def test_an_infinite_policy_override_does_not_beat_a_real_timeout(self) -> None:
        """A non-bound must not win ``min()`` against a genuine one."""
        assert self._resolve(30.0, math.inf) == 30.0

    @pytest.mark.parametrize(
        ("configured", "policy", "expected"),
        [
            (60.0, 30.0, 30.0),
            (30.0, 60.0, 30.0),
            (45.0, None, 45.0),
            (None, 45.0, 45.0),
        ],
    )
    def test_the_tighter_of_two_real_timeouts_still_wins(
        self, configured: float | None, policy: float | None, expected: float
    ) -> None:
        """The behaviour that already worked must survive the fallback."""
        assert self._resolve(configured, policy) == expected


class TestResolveProviderTimeout:
    """The shared resolver, exercised over the values that reach it."""

    @pytest.mark.parametrize("value", [None, math.inf, -math.inf, 0.0, -1.0, math.nan])
    def test_a_non_bound_resolves_to_the_default(self, value: float | None) -> None:
        assert resolve_provider_timeout(value) == DEFAULT_AGENT_PROVIDER_TIMEOUT_SECONDS

    @pytest.mark.parametrize("value", [0.1, 30.0, 600.0, 3600.0])
    def test_a_real_bound_is_preserved(self, value: float) -> None:
        assert resolve_provider_timeout(value) == value


# --- oracle for the call site ------------------------------------------------


class TestProviderCallAlwaysBounded:
    """``run_provider_with_timeout`` applies a deadline even given ``None``."""

    @pytest.mark.asyncio
    async def test_a_none_timeout_is_still_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The measured defect: ``None`` skipped ``wait_for`` entirely."""
        monkeypatch.setattr(
            "zeroth.runtime.agents.provider.DEFAULT_AGENT_PROVIDER_TIMEOUT_SECONDS",
            _NARROW_DEADLINE_SECONDS,
        )
        adapter = _NeverAnswers()

        with pytest.raises(TimeoutError):
            await run_provider_with_timeout(
                cast(Any, adapter), _request(), timeout_seconds=None
            )

        assert adapter.calls == 1, "the adapter was never actually invoked"

    @pytest.mark.asyncio
    async def test_an_infinite_timeout_is_still_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "zeroth.runtime.agents.provider.DEFAULT_AGENT_PROVIDER_TIMEOUT_SECONDS",
            _NARROW_DEADLINE_SECONDS,
        )

        with pytest.raises(TimeoutError):
            await run_provider_with_timeout(
                cast(Any, _NeverAnswers()), _request(), timeout_seconds=math.inf
            )

    @pytest.mark.asyncio
    async def test_an_explicit_timeout_is_honoured(self) -> None:
        """A caller-supplied bound must still be the one that fires."""
        with pytest.raises(TimeoutError):
            await run_provider_with_timeout(
                cast(Any, _NeverAnswers()),
                _request(),
                timeout_seconds=_NARROW_DEADLINE_SECONDS,
            )

    @pytest.mark.asyncio
    async def test_a_fast_provider_is_not_disturbed(self) -> None:
        """Introducing a bound must not break a call that answers in time."""

        class _Answers:
            async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
                del request
                return ProviderResponse(content="ok")

        response = await run_provider_with_timeout(
            cast(Any, _Answers()), _request(), timeout_seconds=None
        )

        assert response.content == "ok"
