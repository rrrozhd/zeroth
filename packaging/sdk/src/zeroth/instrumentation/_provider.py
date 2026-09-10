"""Shared mechanics for provider-SDK adapters: run lookup, timing, attempt charging."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any

from zeroth.instrumentation._scope import current_scope, mark_charged
from zeroth.instrumentation.capture import Run, Usage, current_run


class Capture:
    """One captured provider call: charges every physical attempt exactly once."""

    def __init__(self, run: Run, operation: str, *, provider: str, priced: bool) -> None:
        self.run = run
        scope = current_scope()
        framework_step = scope.step if scope is not None and not scope.charged else None
        self.step = framework_step or run.next_step(operation)
        self.provider = provider
        self.priced = priced
        self.model_hint = "unknown"
        self.started = perf_counter()
        self.charged = False

    def _elapsed(self) -> int:
        return int((perf_counter() - self.started) * 1000)

    def retried(self, retries_taken: int, model: str) -> None:
        """SDK-internal retries are physical requests without usable usage."""
        for attempt in range(1, retries_taken + 1):
            self.run.charge(
                self.step, attempt=attempt, model=model, usage=None, provider=self.provider,
                error="retried", priced=self.priced, latency_ms=0,
            )

    def complete(
        self, *, model: str, usage: Usage | None, request_id: str | None, attempt: int,
        stream: str | None = None,
    ) -> None:
        if self.charged:
            return
        self.charged = True
        mark_charged()
        self.run.charge(
            self.step, attempt=attempt, model=model, usage=usage, provider=self.provider,
            request_id=request_id, priced=self.priced, latency_ms=self._elapsed(),
            metadata={"stream": stream},
        )

    def abort(self, *, model: str, attempt: int) -> None:
        """A stream closed or collected before its final usage arrived."""
        if self.charged:
            return
        self.charged = True
        mark_charged()
        self.run.charge(
            self.step, attempt=attempt, model=model, usage=None, provider=self.provider,
            error="aborted", priced=self.priced, latency_ms=self._elapsed(),
            metadata={"stream": "aborted"},
        )

    def failed(self, error: BaseException, *, model: str, attempt: int = 1,
               stream: str | None = None) -> None:
        """A physical attempt that ended in an exception or cancellation: charged, unmeasured."""
        if self.charged:
            return
        self.charged = True
        mark_charged()
        status = getattr(error, "status_code", None)
        self.run.charge(
            self.step, attempt=attempt, model=model, usage=None, provider=self.provider,
            error=type(error).__name__, priced=self.priced, latency_ms=self._elapsed(),
            metadata={"stream": stream, "status_code": status, "retries_unobserved": True},
        )


def official(client: Any, host: str, declared_provider: str | None) -> tuple[str, bool]:
    """Provider identity and whether the rate card applies.

    A base URL on the provider's own host is that provider. Anything else is an
    OpenAI-compatible or proxied endpoint: identity and price are unknown unless
    the recipe declares which provider is actually billed behind it.
    """
    base = str(getattr(client, "base_url", "") or "")
    if base.startswith(f"https://{host}"):
        return declared_provider or host.split(".")[1], True
    if declared_provider is not None:
        return declared_provider, True
    return "unknown", False


def active(operation: str, provider: str, priced: bool) -> Capture | None:
    run = current_run()
    return None if run is None else Capture(run, operation, provider=provider, priced=priced)


Factory = Callable[[Callable[..., Any]], Callable[..., Any]]


def bind(target: Any, name: str, factory: Factory) -> None:
    """Replace a bound method on a resource instance with its captured wrapper."""
    original = getattr(target, name)
    if getattr(original, "_zeroth_captured", False):
        return
    wrapped = factory(original)
    wrapped._zeroth_captured = True  # type: ignore[attr-defined]
    setattr(target, name, wrapped)
