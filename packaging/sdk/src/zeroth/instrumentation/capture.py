"""Shared capture contract: one physical call, one charge; one run, one summary.

Every recipe maps its framework's lifecycle onto this module and nothing else,
so the economics stay centralised in the sold ``zeroth.protocol`` events:

* ``Run.charge`` records one physical model call as ``cost_role="charge"`` with
  a stable ``event_id``/``charge_id`` (``workflow:version:run:step:attempt``),
  an estimated cost from the pinned rate card, and the usage split under
  ``metadata["usage"]``. Missing usage or an unknown model stays unmeasured.
* ``Run.tool_charge`` records a priced tool call the same way with a
  caller-supplied amount.
* ``Run.summary`` records the run's aggregate as ``cost_role="summary"``, which
  the protocol forbids from carrying money, so wrapping a run twice cannot
  double count.
* ``Run.outcome`` records the customer's own outcome truth and maturity.

Delivery is synchronous; a failed delivery is retained in ``Recorder.lost`` and
re-raised unless the recorder was built with ``raise_on_error=False``, so a
passive adapter never changes the application's behaviour.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from zeroth.instrumentation.rate_card import RATE_CARD_VERSION, bare_model, price, rates
from zeroth.protocol import ExecutionEvent, OutcomeEvent

Pricing = Literal["rate_card", "caller", "missing_usage", "unknown_model"]


class EventSink(Protocol):
    def record_execution(self, event: ExecutionEvent) -> dict[str, Any]: ...

    def record_outcome(self, event: OutcomeEvent) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Usage:
    """Provider usage normalised so ``input_tokens`` excludes cached tokens."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def as_metadata(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass
class Recorder:
    """Deliver events to a Zeroth client and retain every delivery that failed."""

    client: EventSink
    raise_on_error: bool = True
    lost: list[tuple[ExecutionEvent | OutcomeEvent, Exception]] = field(default_factory=list)

    def deliver(self, event: ExecutionEvent | OutcomeEvent) -> dict[str, Any] | None:
        try:
            if isinstance(event, ExecutionEvent):
                return self.client.record_execution(event)
            return self.client.record_outcome(event)
        except Exception as error:  # noqa: BLE001 - retained, not hidden
            self.lost.append((event, error))
            if self.raise_on_error:
                raise
            return None

    def run(self, workflow: str, workflow_version: str, run_id: str) -> Run:
        return Run(self, workflow, workflow_version, run_id)


_current: ContextVar[Run | None] = ContextVar("zeroth_capture_run", default=None)


def current_run() -> Run | None:
    """The run an adapter should attach physical calls to, if one is active."""
    return _current.get()


@dataclass
class Run:
    recorder: Recorder
    workflow: str
    workflow_version: str
    run_id: str
    _occurrences: dict[str, int] = field(default_factory=dict)

    def event_id(self, step: str, attempt: int) -> str:
        return f"{self.workflow}:{self.workflow_version}:{self.run_id}:{step}:{attempt}"

    def step(self, name: str) -> str:
        """A step name unique within the run for repeated invocations of ``name``."""
        count = self._occurrences.get(name, 0) + 1
        self._occurrences[name] = count
        return name if count == 1 else f"{name}#{count}"

    @contextmanager
    def active(self) -> Iterator[Run]:
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def charge(
        self,
        step: str,
        *,
        model: str,
        usage: Usage | None,
        attempt: int = 1,
        provider: str | None = None,
        request_id: str | None = None,
        latency_ms: int = 0,
        error: str | None = None,
        recorded_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record one physical model call; usage or model gaps stay unmeasured."""
        card = rates(model)
        cost: Decimal | None = None
        pricing: Pricing
        if usage is None:
            pricing = "missing_usage"
        elif card is None:
            pricing = "unknown_model"
        else:
            cost = price(
                model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            )
            pricing = "rate_card" if cost is not None else "unknown_model"
        return self._charge(
            step,
            attempt=attempt,
            model=bare_model(model),
            cost=cost,
            measurement="estimated" if cost is not None else "unmeasured",
            latency_ms=latency_ms,
            recorded_at=recorded_at,
            metadata={
                "charge_kind": "model",
                "provider": provider or (card.provider if card else "unknown"),
                "provider_request_id": request_id,
                "usage": None if usage is None else usage.as_metadata(),
                "pricing": pricing,
                "rate_card_version": RATE_CARD_VERSION if pricing == "rate_card" else None,
                "error": error,
                **(metadata or {}),
            },
        )

    def tool_charge(
        self,
        step: str,
        *,
        tool: str,
        cost_usd: Decimal | None,
        attempt: int = 1,
        measurement: Literal["measured", "estimated"] = "measured",
        latency_ms: int = 0,
        error: str | None = None,
        recorded_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record one priced tool call with the caller's own cost assertion."""
        return self._charge(
            step,
            attempt=attempt,
            model=f"tool:{tool}",
            cost=cost_usd,
            measurement=measurement if cost_usd is not None else "unmeasured",
            latency_ms=latency_ms,
            recorded_at=recorded_at,
            metadata={
                "charge_kind": "tool",
                "tool": tool,
                "pricing": "caller" if cost_usd is not None else "missing_usage",
                "error": error,
                **(metadata or {}),
            },
        )

    def _charge(
        self,
        step: str,
        *,
        attempt: int,
        model: str,
        cost: Decimal | None,
        measurement: str,
        latency_ms: int,
        recorded_at: datetime | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        event_id = self.event_id(step, attempt)
        event = ExecutionEvent(
            workflow=self.workflow,
            workflow_version=self.workflow_version,
            run_id=self.run_id,
            step=step,
            attempt=attempt,
            event_id=event_id,
            cost_role="charge",
            charge_id=f"charge:{event_id}",
            recorded_at=recorded_at or datetime.now(UTC),
            model_version=model,
            cost_usd=cost,
            cost_measurement=measurement,
            latency_ms=latency_ms,
            metadata=metadata,
        )
        return self.recorder.deliver(event)

    def summary(
        self,
        terminal_state: str,
        *,
        recorded_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record the run aggregate; summaries never carry money."""
        event = ExecutionEvent(
            workflow=self.workflow,
            workflow_version=self.workflow_version,
            run_id=self.run_id,
            step="summary",
            event_id=self.event_id("summary", 1),
            cost_role="summary",
            recorded_at=recorded_at or datetime.now(UTC),
            metadata={"terminal_state": terminal_state, **(metadata or {})},
        )
        return self.recorder.deliver(event)

    def outcome(
        self,
        accepted: bool | None,
        *,
        maturity: Literal["unknown", "provisional", "final", "withdrawn"] = "final",
        occurred_at: datetime | None = None,
        outcome_type: str = "accepted",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record the customer's outcome truth for this run."""
        event = OutcomeEvent(
            workflow=self.workflow,
            workflow_version=self.workflow_version,
            run_id=self.run_id,
            accepted=accepted,
            maturity=maturity,
            outcome_type=outcome_type,
            occurred_at=occurred_at or datetime.now(UTC),
            metadata=metadata or {},
        )
        return self.recorder.deliver(event)
