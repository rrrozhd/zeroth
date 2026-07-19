"""OpenTelemetry distributed tracing (optional).

Emits spans across the orchestrator drive loop, node dispatch, agent runs, tool
calls, and subgraph / fan-out hops, so a single multi-hop run can be followed end
to end. Each span carries the active correlation ID and the run/node identifiers
that also key the Prometheus metrics and audit records, so traces line up with
both (OBS-03).

OpenTelemetry is an *optional* dependency (the ``otel`` extra). Until
``configure_tracing`` enables it — and always, when the extra is not installed —
every span is a zero-overhead no-op, so the core carries no hard dependency on
OpenTelemetry and default behaviour is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from zeroth.core.observability.correlation import get_correlation_id

try:
    from opentelemetry import trace as _otel_trace

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the otel extra
    _OTEL_AVAILABLE = False

if TYPE_CHECKING:
    from zeroth.platform.config.settings import TracingSettings

_TRACING_ENABLED = False
_TRACER_NAME = "zeroth.core"


def configure_tracing(settings: TracingSettings) -> bool:
    """Configure the OTLP tracer from settings; return True if tracing is active.

    No-op returning ``False`` when ``settings.enabled`` is False or the ``otel``
    extra is not installed. Idempotent — intended to be called once at bootstrap.
    """
    global _TRACING_ENABLED
    if not settings.enabled or not _OTEL_AVAILABLE:
        _TRACING_ENABLED = False
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": settings.service_name})
    provider = TracerProvider(resource=resource)
    exporter = (
        OTLPSpanExporter(endpoint=settings.otlp_endpoint)
        if settings.otlp_endpoint
        else OTLPSpanExporter()
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _otel_trace.set_tracer_provider(provider)
    _TRACING_ENABLED = True
    return True


@contextmanager
def start_span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Start ``name`` as the current span; a zero-overhead no-op when tracing is off.

    Attaches the active correlation ID plus any non-None ``attributes``. Work
    started inside the block — including asyncio tasks (e.g. fan-out branches) —
    inherits this span as its parent via OpenTelemetry's contextvar context, which
    asyncio copies at task creation. Exceptions propagate and are recorded on the
    span (OTel's default ``record_exception`` / ``set_status_on_exception``).
    """
    if not _TRACING_ENABLED or not _OTEL_AVAILABLE:
        yield
        return
    attrs: dict[str, Any] = {
        key: value for key, value in (attributes or {}).items() if value is not None
    }
    correlation_id = get_correlation_id()
    if correlation_id:
        attrs["zeroth.correlation_id"] = correlation_id
    tracer = _otel_trace.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(name, attributes=attrs):
        yield
