"""InstrumentedProviderAdapter -- cost-tracking decorator for any ProviderAdapter.

Wraps any ProviderAdapter to:
1. Measure wall-clock latency of the LLM call
2. Estimate USD cost via CostEstimator (litellm pricing)
3. Emit a Regulus ExecutionEvent via RegulusClient (fire-and-forget)
4. Return enriched ProviderResponse with cost_usd and cost_event_id

Exception -- cache hits: when an inner ``CachingProviderAdapter`` short-circuits
the call (``response.metadata["cache_hit"] is True``) no model was reached, so
steps 3-4 would fabricate cost and double-bill Regulus. On a hit the adapter emits
no event and attributes zero marginal cost, recording the avoided spend as
``metadata["cache_saved_usd"]`` for savings visibility.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from time import perf_counter

from zeroth.econ.analytics.client import RegulusClient
from zeroth.econ.analytics.cost import CostEstimator
from zeroth.econ.analytics.identity import capability_identity, implementation_identity
from zeroth.econ.instrumentation import ExecutionEvent
from zeroth.econ.instrumentation.schemas import DimensionValue
from zeroth.econ.measurement import MeasurementState
from zeroth.runtime.agents.provider import (
    ModelParams,
    ProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)

_DEFAULT_RESERVED_OUTPUT_TOKENS = 4096


@dataclass(frozen=True, slots=True)
class ProviderCallEvidence:
    """Sanitized identity and economics emitted by one reserved provider call."""

    operation_id: str
    model_name: str
    provider_request_id: str | None
    cost_event_id: str | None
    cost_measurement: MeasurementState
    measured_cost_usd: Decimal | None
    estimated_cost_usd: Decimal | None
    input_tokens: int | None
    output_tokens: int | None
    cleanup_status: str
    provider_call_attempted: bool
    cache_hit: bool


class InstrumentedProviderAdapter:
    """Wraps any ProviderAdapter to emit Regulus cost events per D-04.

    After each ainvoke() call, estimates the cost, builds an ExecutionEvent,
    fires it to Regulus via the client, and enriches the response with
    cost_usd and cost_event_id so downstream code (audit records, etc.)
    can carry cost attribution.

    A cache hit is the one exception: when an inner ``CachingProviderAdapter``
    short-circuits the call (``response.metadata["cache_hit"] is True``), there is
    no new spend, so no event is emitted and zero marginal cost is attributed
    (see ``ainvoke``).
    """

    def __init__(
        self,
        inner: ProviderAdapter,
        regulus_client: RegulusClient | None,
        cost_estimator: CostEstimator,
        *,
        node_id: str,
        run_id: str,
        tenant_id: str,
        deployment_ref: str,
        workflow_version: str | None = None,
        subject_id: str | None = None,
        dimensions: dict[str, DimensionValue] | None = None,
        cost_instrumentation: object | None = None,
        campaign_id: str | None = None,
        per_run_cap_usd: Decimal | float | None = None,
        branch_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._regulus_client = regulus_client
        self._cost_estimator = cost_estimator
        self._node_id = node_id
        self._run_id = run_id
        self._tenant_id = tenant_id
        self._deployment_ref = deployment_ref
        self._workflow_version = workflow_version
        self._subject_id = subject_id
        self._dimensions = dict(dimensions or {})
        self._cost_instrumentation = cost_instrumentation
        self._campaign_id = campaign_id
        self._per_run_cap_usd = (
            Decimal(str(per_run_cap_usd)) if per_run_cap_usd is not None else None
        )
        self._branch_id = branch_id or "main"
        self._call_ordinals = count(1)
        self._call_evidence: list[ProviderCallEvidence] = []

    @property
    def call_evidence(self) -> tuple[ProviderCallEvidence, ...]:
        """Return immutable, credential-free observations for completed call attempts."""
        return tuple(self._call_evidence)

    def _record_call_evidence(
        self,
        *,
        operation_id: str,
        request: ProviderRequest,
        provider_request_id: str | None,
        cost_event_id: str | None,
        cost_measurement: MeasurementState,
        actual_cost_usd: Decimal | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cleanup_status: str,
        provider_call_attempted: bool,
        cache_hit: bool = False,
    ) -> None:
        self._call_evidence.append(
            ProviderCallEvidence(
                operation_id=operation_id,
                model_name=request.model_name,
                provider_request_id=provider_request_id,
                cost_event_id=cost_event_id,
                cost_measurement=cost_measurement,
                measured_cost_usd=(
                    actual_cost_usd if cost_measurement is MeasurementState.MEASURED else None
                ),
                estimated_cost_usd=(
                    actual_cost_usd if cost_measurement is MeasurementState.ESTIMATED else None
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cleanup_status=cleanup_status,
                provider_call_attempted=provider_call_attempted,
                cache_hit=cache_hit,
            )
        )

    def _bounded_request(self, request: ProviderRequest) -> tuple[ProviderRequest, Decimal]:
        """Apply a provider-visible output bound and price a conservative request bound."""
        params = request.model_params or ModelParams()
        max_output_tokens = params.max_tokens or _DEFAULT_RESERVED_OUTPUT_TOKENS
        if params.max_tokens is None:
            params = params.model_copy(update={"max_tokens": max_output_tokens})
            request = request.model_copy(update={"model_params": params})

        # Every UTF-8 byte can represent at most one token for the tokenizers used by
        # supported chat providers.  Pricing the serialized request byte count is
        # intentionally conservative and avoids making admission depend on a remote
        # tokenizer/control plane.
        serialized = json.dumps(
            {"messages": request.messages, "tools": request.tools},
            default=str,
            ensure_ascii=False,
        ).encode("utf-8")
        max_input_tokens = max(1, len(serialized))
        maximum = self._cost_estimator.estimate(
            request.model_name,
            input_tokens=max_input_tokens,
            output_tokens=max_output_tokens,
        )
        maximum = Decimal(str(maximum))
        if maximum <= 0:
            raise RuntimeError(
                f"cannot derive a positive server-side cost bound for {request.model_name}"
            )
        return request, maximum

    @staticmethod
    def _provider_request_id(response: ProviderResponse) -> str | None:
        for key in ("provider_request_id", "request_id", "id"):
            value = response.metadata.get(key)
            if value:
                return str(value)
        return None

    def _operation_id(self, request: ProviderRequest, ordinal: int) -> str:
        raw = f"{self._run_id}\0{self._node_id}\0{self._branch_id}\0{ordinal}\0{request.model_name}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        # Preserve enough human-readable identity for operators while remaining
        # safely below the persistent operation_id column's 192-character limit.
        return (
            f"workflow:{self._run_id[:48]}:{self._node_id[:32]}:"
            f"{self._branch_id[:32]}:call:{ordinal}:{digest}"
        )

    async def _ainvoke_reserved(self, request: ProviderRequest) -> ProviderResponse:
        """Reserve atomically before one logical provider call and settle afterward."""
        instrumentation = self._cost_instrumentation
        assert instrumentation is not None
        if self._per_run_cap_usd is None:
            raise RuntimeError("persistent provider cost admission requires a per-run cap")

        request, maximum = self._bounded_request(request)
        ordinal = next(self._call_ordinals)
        operation_id = self._operation_id(request, ordinal)
        capability_id = capability_identity(self._tenant_id, self._deployment_ref, self._node_id)
        implementation_id = implementation_identity(capability_id, request.model_name)
        common = {
            "tenant_id": self._tenant_id,
            "campaign_id": self._campaign_id,
            "operation_id": operation_id,
            "run_id": self._run_id,
            "capability_id": capability_id,
            "implementation_id": implementation_id,
        }
        await instrumentation.reserve_probe(
            **common,
            max_cost_usd=str(maximum),
            run_cap_usd=str(self._per_run_cap_usd),
        )

        start = perf_counter()
        try:
            response = await self._inner.ainvoke(request)
        except BaseException as exc:
            if getattr(exc, "provider_call_attempted", None) is False:
                evidence = await instrumentation.release_probe(
                    tenant_id=self._tenant_id,
                    operation_id=operation_id,
                    cleanup_status="provider_not_called",
                )
                self._record_call_evidence(
                    operation_id=operation_id,
                    request=request,
                    provider_request_id=None,
                    cost_event_id=getattr(evidence, "cost_event_id", None),
                    cost_measurement=MeasurementState.MEASURED,
                    actual_cost_usd=Decimal(0),
                    input_tokens=None,
                    output_tokens=None,
                    cleanup_status=getattr(evidence, "cleanup_status", "provider_not_called"),
                    provider_call_attempted=False,
                )
            else:
                evidence = await instrumentation.mark_probe_ambiguous(
                    **common,
                    latency_ms=int((perf_counter() - start) * 1000),
                )
                self._record_call_evidence(
                    operation_id=operation_id,
                    request=request,
                    provider_request_id=getattr(evidence, "provider_request_id", None),
                    cost_event_id=getattr(evidence, "cost_event_id", None),
                    cost_measurement=MeasurementState.UNMEASURED,
                    actual_cost_usd=None,
                    input_tokens=None,
                    output_tokens=None,
                    cleanup_status=getattr(evidence, "cleanup_status", "pending_reconciliation"),
                    provider_call_attempted=True,
                )
            raise
        elapsed_ms = int((perf_counter() - start) * 1000)

        if response.metadata.get("cache_hit") is True:
            evidence = await instrumentation.release_probe(
                tenant_id=self._tenant_id,
                operation_id=operation_id,
                cleanup_status="cache_hit_no_provider_work",
            )
            self._record_call_evidence(
                operation_id=operation_id,
                request=request,
                provider_request_id=None,
                cost_event_id=None,
                cost_measurement=MeasurementState.MEASURED,
                actual_cost_usd=Decimal(0),
                input_tokens=None,
                output_tokens=None,
                cleanup_status=getattr(evidence, "cleanup_status", "cache_hit_no_provider_work"),
                provider_call_attempted=False,
                cache_hit=True,
            )
            return response.model_copy(
                update={
                    "cost_usd": 0.0,
                    "cost_measurement": MeasurementState.MEASURED,
                    "cost_event_id": None,
                }
            )

        actual: Decimal | None = None
        measurement = MeasurementState.UNMEASURED
        if response.cost_usd is not None:
            actual = Decimal(str(response.cost_usd))
            measurement = MeasurementState.MEASURED
        elif response.token_usage is not None:
            actual = Decimal(
                str(
                    self._cost_estimator.estimate(
                        request.model_name,
                        input_tokens=response.token_usage.input_tokens,
                        output_tokens=response.token_usage.output_tokens,
                    )
                )
            )
            if actual >= 0:
                measurement = MeasurementState.ESTIMATED

        if actual is None or measurement is MeasurementState.UNMEASURED:
            evidence = await instrumentation.mark_probe_ambiguous(
                **common,
                latency_ms=elapsed_ms,
                provider_request_id=self._provider_request_id(response),
            )
            self._record_call_evidence(
                operation_id=operation_id,
                request=request,
                provider_request_id=evidence.provider_request_id,
                cost_event_id=evidence.cost_event_id,
                cost_measurement=MeasurementState.UNMEASURED,
                actual_cost_usd=None,
                input_tokens=(response.token_usage.input_tokens if response.token_usage else None),
                output_tokens=(
                    response.token_usage.output_tokens if response.token_usage else None
                ),
                cleanup_status=evidence.cleanup_status,
                provider_call_attempted=True,
            )
            return response.model_copy(
                update={
                    "cost_event_id": evidence.cost_event_id,
                    "cost_measurement": MeasurementState.UNMEASURED,
                }
            )

        evidence = await instrumentation.commit_probe(
            **common,
            actual_cost_usd=str(actual),
            cost_measurement=measurement.value,
            provider_request_id=self._provider_request_id(response),
            cleanup_status="complete",
            latency_ms=elapsed_ms,
            input_tokens=(response.token_usage.input_tokens if response.token_usage else None),
            output_tokens=(response.token_usage.output_tokens if response.token_usage else None),
        )
        updates: dict[str, object] = {
            "cost_measurement": MeasurementState(evidence.cost_measurement),
            "cost_event_id": evidence.cost_event_id,
        }
        if measurement is MeasurementState.MEASURED:
            updates["cost_usd"] = float(actual)
        else:
            updates["estimated_cost_usd"] = float(actual)
        self._record_call_evidence(
            operation_id=operation_id,
            request=request,
            provider_request_id=evidence.provider_request_id,
            cost_event_id=evidence.cost_event_id,
            cost_measurement=measurement,
            actual_cost_usd=actual,
            input_tokens=(response.token_usage.input_tokens if response.token_usage else None),
            output_tokens=(response.token_usage.output_tokens if response.token_usage else None),
            cleanup_status=evidence.cleanup_status,
            provider_call_attempted=True,
        )
        return response.model_copy(update=updates)

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Call the inner adapter, estimate cost, emit event, return enriched response.

        A cache hit (set by an inner ``CachingProviderAdapter``) is the exception: no
        model was reached, so no event is emitted and zero marginal cost is attributed
        -- see the cache-hit branch below.
        """
        if self._cost_instrumentation is not None:
            return await self._ainvoke_reserved(request)

        start = perf_counter()
        response = await self._inner.ainvoke(request)
        elapsed_ms = int((perf_counter() - start) * 1000)

        # Extract token counts from response (may be None)
        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None
        if response.token_usage is not None:
            input_tokens = response.token_usage.input_tokens
            output_tokens = response.token_usage.output_tokens
            total_tokens = response.token_usage.total_tokens

        estimated_cost: Decimal | None = None
        if response.token_usage is not None:
            with contextlib.suppress(Exception):
                estimated_cost = self._cost_estimator.estimate(
                    request.model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

        # Cache hit: an inner CachingProviderAdapter short-circuited the model call,
        # so there is no new spend. Emitting an event or stamping cost here would
        # fabricate cost and double-bill Regulus for a call that never reached a
        # model. Attribute zero marginal cost and emit no event; record what the call
        # would have cost as cache_saved_usd for savings visibility. Preserve
        # cache_hit=True so econ.waste's cache_inefficiency detector still sees it.
        if response.metadata.get("cache_hit") is True:
            return response.model_copy(
                update={
                    "cost_usd": 0.0,
                    "cost_measurement": MeasurementState.MEASURED,
                    "cost_event_id": None,
                    "metadata": {
                        **response.metadata,
                        "cache_saved_usd": (
                            float(estimated_cost) if estimated_cost is not None else None
                        ),
                    },
                }
            )

        # An inner instrumentation layer already owns this measurement/event.
        # Repricing it here would count the same provider call twice.
        if response.cost_measurement is not MeasurementState.UNMEASURED:
            return response

        # No Regulus client (cost tracking on, event stream off): still attribute the
        # local litellm cost estimate so the audit trail and local econ lenses work,
        # but emit no event and leave cost_event_id None — there is no Regulus event to
        # reference. Mirrors the already-shipped cache-hit shape (cost_usd + no event).
        if self._regulus_client is None:
            return response.model_copy(
                update={
                    "estimated_cost_usd": (
                        float(estimated_cost) if estimated_cost is not None else None
                    ),
                    "cost_measurement": (
                        MeasurementState.ESTIMATED
                        if estimated_cost is not None
                        else MeasurementState.UNMEASURED
                    ),
                    "cost_event_id": None,
                }
            )

        # Build and emit the Regulus ExecutionEvent
        capability_id = capability_identity(self._tenant_id, self._deployment_ref, self._node_id)
        event = ExecutionEvent(
            capability_id=capability_id,
            implementation_id=implementation_identity(capability_id, request.model_name),
            workflow_id=self._deployment_ref,
            workflow_version=self._workflow_version,
            run_id=self._run_id,
            step_id=self._node_id,
            attempt=1,
            subject_id=self._subject_id,
            dimensions=self._dimensions,
            model_version=request.model_name,
            tenant_id=self._tenant_id,
            deployment_ref=self._deployment_ref,
            evidence_kind="production",
            token_cost_usd=estimated_cost,
            cost_measurement=(
                MeasurementState.ESTIMATED
                if estimated_cost is not None
                else MeasurementState.UNMEASURED
            ),
            usage_measurement=(
                MeasurementState.MEASURED
                if response.token_usage is not None
                else MeasurementState.UNMEASURED
            ),
            latency_ms=elapsed_ms,
            compute_time_ms=elapsed_ms,
            metadata={
                "run_id": self._run_id,
                "tenant_id": self._tenant_id,
                "deployment_ref": self._deployment_ref,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
        )
        self._regulus_client.track_execution(event)

        # Return enriched response with cost attribution
        return response.model_copy(
            update={
                "estimated_cost_usd": (
                    float(estimated_cost) if estimated_cost is not None else None
                ),
                "cost_measurement": (
                    MeasurementState.ESTIMATED
                    if estimated_cost is not None
                    else MeasurementState.UNMEASURED
                ),
                "cost_event_id": event.execution_id,
            }
        )


_instrumented_adapter_parameters = inspect.signature(InstrumentedProviderAdapter).parameters
InstrumentedProviderAdapter.__signature__ = inspect.signature(InstrumentedProviderAdapter).replace(
    parameters=[
        parameter
        for name, parameter in _instrumented_adapter_parameters.items()
        if name
        not in {
            "cost_instrumentation",
            "campaign_id",
            "per_run_cap_usd",
            "branch_id",
            "workflow_version",
            "subject_id",
            "dimensions",
        }
    ]
)
