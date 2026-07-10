from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def maybe_span(enabled: bool, name: str, attrs: dict[str, Any]) -> Iterator[None]:
    if not enabled:
        yield
        return
    try:
        from opentelemetry import trace  # type: ignore

        tracer = trace.get_tracer("econ_instrumentation")
        with tracer.start_as_current_span(name) as span:
            for key, value in attrs.items():
                span.set_attribute(key, value)
            yield
    except Exception:
        yield
