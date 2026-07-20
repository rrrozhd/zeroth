from __future__ import annotations

from time import time
from typing import Any

from zeroth.econ.instrumentation.runtime import get_runtime


def start_time_ms() -> int:
    return int(time() * 1000)


def finalize_capture_metadata(
    *,
    metadata: dict[str, Any],
    source_layer: str,
    provider: str,
    model: str,
    join_key: str,
    run_id: str | None,
    start_ms: int,
) -> dict[str, Any]:
    runtime = get_runtime()
    md = dict(metadata)
    md.pop("_start_ms", None)
    md["source_layer"] = source_layer
    md["run_id"] = run_id
    md["parent_run_id"] = runtime.parent_run_id()
    fingerprint = runtime.dedupe_fingerprint(
        join_key=join_key,
        provider=provider,
        model=model,
        source_layer=source_layer,
        run_id=run_id,
        start_time_ms=start_ms,
    )
    md["dedupe_fingerprint"] = fingerprint
    if runtime.auto_enabled and runtime.auto_config.capture_mode == "all_layers":
        md["primary_for_rollup"] = runtime.mark_primary_for_rollup(fingerprint, source_layer)
    else:
        md["primary_for_rollup"] = True
    return md


def should_capture_provider(provider_layer: str) -> bool:
    return get_runtime().should_capture_provider(provider_layer)


def should_capture_layer(source_layer: str) -> bool:
    return get_runtime().should_capture_layer(source_layer)


def should_emit_by_rate(source_layer: str) -> bool:
    return get_runtime().should_emit_by_rate(source_layer)
