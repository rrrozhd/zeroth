"""Raw request validation and derived load/recovery measurements."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

ROW_KEYS = frozenset(
    {
        "request_id",
        "profile",
        "tenant_id",
        "deployment_ref",
        "replica",
        "worker",
        "surface",
        "fault",
        "status_code",
        "retry_after_seconds",
        "started_at_ms",
        "finished_at_ms",
        "latency_ms",
        "queue_depth",
        "cpu_percent",
        "memory_bytes",
        "lifecycle",
    }
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "drained"})
FAULT_RECOVERY_STATES = {
    "database-contention": frozenset({"coordination-timeout", "query-restored"}),
    "redis-loss": frozenset({"artifact-unavailable", "artifact-restored"}),
    "worker-loss": frozenset({"worker-withdrawn", "worker-replaced"}),
    "service-restart": frozenset({"service-stopped", "service-started"}),
    "network-delay": frozenset({"transport-delayed", "transport-restored"}),
    "downstream-throttling": frozenset({"downstream-429", "delivery-retried", "delivered"}),
}


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank value used by generation and validation."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def _lifecycle_accounting(rows: list[dict]) -> tuple[int, int, float]:
    accepted: Counter[str] = Counter()
    terminal: Counter[str] = Counter()
    recoveries: list[float] = []
    for row in rows:
        events = row["lifecycle"]
        for event in events:
            run_id = event.get("run_id")
            if event.get("state") == "accepted" and run_id:
                accepted[run_id] += 1
            if event.get("state") in TERMINAL_STATES and run_id:
                terminal[run_id] += 1
        injected = next((event for event in events if event.get("state") == "fault-injected"), None)
        recovered = next((event for event in events if event.get("state") == "recovered"), None)
        if injected and recovered:
            recoveries.append(max(0.0, (recovered["at_ms"] - injected["at_ms"]) / 1000))
    lost = sum(count for run_id, count in accepted.items() if terminal[run_id] == 0)
    duplicates = sum(max(0, count - 1) for count in accepted.values())
    return lost, duplicates, max(recoveries, default=0.0)


def _fairness(rows: list[dict]) -> float:
    tenants = sorted({row["tenant_id"] for row in rows})
    counts = Counter()
    for row in rows:
        states = {event.get("state") for event in row["lifecycle"]}
        if "accepted" in states and states & {"completed", "failed"}:
            counts[row["tenant_id"]] += 1
    values = [counts[tenant] for tenant in tenants]
    squared = sum(value * value for value in values)
    return 0.0 if not squared else (sum(values) ** 2) / (len(values) * squared)


def _throughput(rows: list[dict]) -> float:
    workload = [row for row in rows if row["fault"] is None]
    started = min(float(row["started_at_ms"]) for row in workload)
    finished = max(float(row["finished_at_ms"]) for row in workload)
    elapsed = max((finished - started) / 1000, 1e-9)
    return len(workload) / elapsed


def recompute(rows: list[dict], profiles: dict) -> dict[str, dict[str, float | int]]:
    """Derive every release metric only from retained per-request rows."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["profile"]].append(row)
    measured = {}
    for name, group in grouped.items():
        latency = [float(row["latency_ms"]) for row in group]
        lost, duplicates, recovery = _lifecycle_accounting(group)
        measured[name] = {
            "throughput_per_second": round(_throughput(group), 6),
            "latency_p50_ms": round(percentile(latency, 0.50), 6),
            "latency_p95_ms": round(percentile(latency, 0.95), 6),
            "latency_p99_ms": round(percentile(latency, 0.99), 6),
            "rejection_rate": round(
                sum(row["status_code"] in {429, 503} for row in group) / len(group), 6
            ),
            "queue_depth_max": max(row["queue_depth"] for row in group),
            "tenant_fairness": round(_fairness(group), 6),
            "cpu_percent_max": max(row["cpu_percent"] for row in group),
            "memory_bytes_max": max(row["memory_bytes"] for row in group),
            "recovery_seconds_max": round(recovery, 6),
            "lost_accepted_runs": lost,
            "duplicate_accepted_runs": duplicates,
        }
    return measured


def _row_errors(row: Any, profiles: dict) -> list[str]:
    if not isinstance(row, dict) or set(row) != ROW_KEYS:
        return ["raw request has unexpected or missing keys"]
    request_id = row["request_id"]
    errors = _identity_errors(row, profiles, request_id)
    errors.extend(_measurement_errors(row, request_id))
    lifecycle_errors = _lifecycle_errors(request_id, row["lifecycle"])
    errors.extend(lifecycle_errors)
    if not lifecycle_errors:
        errors.extend(_rejection_errors(row, profiles, request_id))
    return errors


def _identity_errors(row: dict, profiles: dict, request_id: Any) -> list[str]:
    errors = []
    text_fields = ("request_id", "profile", "tenant_id", "deployment_ref", "replica", "worker")
    if any(not isinstance(row[name], str) or not row[name].strip() for name in text_fields):
        errors.append("raw request identity fields must be non-empty strings")
    if row["profile"] not in profiles["profiles"]:
        errors.append(f"unknown profile {row['profile']!r}")
    if row["surface"] not in profiles["surfaces"]:
        errors.append(f"request {request_id!r} has unknown product surface")
    if row["fault"] is not None and row["fault"] not in profiles["faults"]:
        errors.append(f"request {request_id!r} has unknown fault")
    if (
        not isinstance(row["status_code"], int)
        or isinstance(row["status_code"], bool)
        or not 100 <= row["status_code"] <= 599
    ):
        errors.append(f"request {request_id!r} has invalid HTTP status")
    return errors


def _measurement_errors(row: dict, request_id: Any) -> list[str]:
    errors = []
    numeric = (
        "started_at_ms",
        "finished_at_ms",
        "latency_ms",
        "queue_depth",
        "cpu_percent",
        "memory_bytes",
    )
    if any(not is_number(row[name]) or row[name] < 0 for name in numeric):
        errors.append(f"request {request_id!r} has invalid measurements")
    elif row["finished_at_ms"] < row["started_at_ms"]:
        errors.append(f"request {request_id!r} finishes before it starts")
    elif abs(row["finished_at_ms"] - row["started_at_ms"] - row["latency_ms"]) > 0.01:
        errors.append(f"request {request_id!r} latency does not match its raw timestamps")
    return errors


def _rejection_errors(row: dict, profiles: dict, request_id: Any) -> list[str]:
    errors = []
    rejected = any(event.get("state") == "rejected" for event in row["lifecycle"])
    if rejected and row["status_code"] not in set(profiles["overload_contract"]["statuses"]):
        errors.append(f"request {request_id!r} rejection must be 429 or 503")
    if rejected and (not is_number(row["retry_after_seconds"]) or row["retry_after_seconds"] <= 0):
        errors.append(f"request {request_id!r} rejection needs useful Retry-After")
    return errors


def _event_identity(
    request_id: Any, event: Any, index: int
) -> tuple[list[str], float | None, tuple[int, str] | None, tuple[int, str] | None]:
    error = f"request {request_id!r} has malformed lifecycle event"
    if not isinstance(event, dict):
        return [error], None, None, None
    state = event.get("state")
    at_ms = event.get("at_ms")
    if not isinstance(state, str) or not state.strip() or not is_number(at_ms) or at_ms < 0:
        return [error], None, None, None
    if state not in {"accepted", *TERMINAL_STATES}:
        return [], at_ms, None, None
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        kind = "acceptance" if state == "accepted" else "terminal"
        return [f"request {request_id!r} {kind} has no run identifier"], at_ms, None, None
    identity = (index, run_id)
    return (
        [],
        at_ms,
        identity if state == "accepted" else None,
        identity if state in TERMINAL_STATES else None,
    )


def _lifecycle_errors(request_id: Any, events: Any) -> list[str]:
    if not isinstance(events, list) or not events:
        return [f"request {request_id!r} has no lifecycle"]
    errors = []
    previous = -1.0
    accepted: list[tuple[int, str]] = []
    terminal: list[tuple[int, str]] = []
    for index, event in enumerate(events):
        event_errors, at_ms, accepted_event, terminal_event = _event_identity(
            request_id, event, index
        )
        errors.extend(event_errors)
        if at_ms is None:
            continue
        if at_ms < previous:
            errors.append(f"request {request_id!r} lifecycle is not chronological")
        previous = at_ms
        accepted.extend(() if accepted_event is None else (accepted_event,))
        terminal.extend(() if terminal_event is None else (terminal_event,))
    if accepted or terminal:
        if len(accepted) != 1:
            errors.append(f"request {request_id!r} must have exactly one acceptance")
        if len(terminal) != 1:
            errors.append(
                f"request {request_id!r} lost accepted run or has duplicate terminal states"
            )
        if len(accepted) == len(terminal) == 1 and (
            accepted[0][1] != terminal[0][1] or accepted[0][0] >= terminal[0][0]
        ):
            errors.append(
                f"request {request_id!r} duplicate accepted run identifier or terminal mismatch"
            )
    return errors


def _matrix_errors(rows: list[dict], matrix: dict, prefix: str) -> list[str]:
    errors = []
    tenants = {row["tenant_id"] for row in rows}
    if len(tenants) < matrix["tenants"]:
        errors.append(f"{prefix} tenant matrix is incomplete")
    for tenant in tenants:
        deployments = {row["deployment_ref"] for row in rows if row["tenant_id"] == tenant}
        if len(deployments) < matrix["deployments_per_tenant"]:
            errors.append(f"{prefix} tenant {tenant} deployment matrix is incomplete")
    if len({row["replica"] for row in rows}) < matrix["replicas"]:
        errors.append(f"{prefix} replica matrix is incomplete")
    if len({row["worker"] for row in rows}) < matrix["workers"]:
        errors.append(f"{prefix} worker matrix is incomplete")
    return errors


def _peak_in_flight(rows: list[dict]) -> int:
    events = [(row["started_at_ms"], 1) for row in rows]
    events.extend((row["finished_at_ms"], -1) for row in rows)
    active = peak = 0
    for _, change in sorted(events, key=lambda event: (event[0], event[1])):
        active += change
        peak = max(peak, active)
    return peak


def _profile_errors(rows: list[dict], profiles: dict) -> list[str]:
    errors = []
    for name, settings in profiles["profiles"].items():
        workload = [row for row in rows if row["profile"] == name and row["fault"] is None]
        expected = round(settings["duration_seconds"] * settings["requests_per_second"])
        if len(workload) != expected:
            errors.append(
                f"profile {name} scheduled request count is {len(workload)}; expected {expected}"
            )
        if workload:
            window = max(row["started_at_ms"] for row in workload) - min(
                row["started_at_ms"] for row in workload
            )
            scheduled = (expected - 1) * 1000 / settings["requests_per_second"]
            if window < scheduled * 0.95:
                errors.append(f"profile {name} schedule window is incomplete")
            if _peak_in_flight(workload) > settings["max_in_flight"]:
                errors.append(f"profile {name} exceeded its maximum in-flight requests")
        errors.extend(_matrix_errors(workload, profiles["matrix"], f"profile {name}"))
        observed = {row["surface"] for row in workload}
        if not set(profiles["surfaces"]).issubset(observed):
            errors.append(f"profile {name} product surface matrix is incomplete")
    return errors


def _fault_errors(rows: list[dict], profiles: dict) -> list[str]:
    errors = []
    for fault in profiles["faults"]:
        matching = [row for row in rows if row["fault"] == fault]
        recovered = any(
            any(event.get("state") == "fault-injected" for event in row["lifecycle"])
            and any(
                event.get("state") == "recovered" and event.get("repair") == "automatic"
                for event in row["lifecycle"]
            )
            for row in matching
        )
        if not recovered:
            errors.append(f"{fault}: no observed automatic recovery")
        observed = {event.get("state") for row in matching for event in row["lifecycle"]}
        missing = FAULT_RECOVERY_STATES[fault] - observed
        if missing:
            errors.append(f"{fault}: missing observed {', '.join(sorted(missing))}")
    return errors


def _lost_ids(rows: list[dict]) -> list[str]:
    accepted = {
        event["run_id"]
        for row in rows
        for event in row["lifecycle"]
        if event.get("state") == "accepted" and event.get("run_id")
    }
    terminal = {
        event["run_id"]
        for row in rows
        for event in row["lifecycle"]
        if event.get("state") in TERMINAL_STATES and event.get("run_id")
    }
    return sorted(accepted - terminal)


def evidence_errors(rows: Any, profiles: dict) -> list[str]:
    """Return bounded reasons raw workload evidence is incomplete."""
    if not isinstance(rows, list) or not rows:
        return ["raw_requests must be a non-empty list"]
    checked = [(row, _row_errors(row, profiles)) for row in rows]
    errors = [error for _, row_errors in checked for error in row_errors]
    valid = [row for row, row_errors in checked if not row_errors]
    if len({row["request_id"] for row in valid}) != len(valid):
        errors.append("raw request identifiers are duplicated")
    if {row["profile"] for row in valid} != set(profiles["profiles"]):
        errors.append("raw requests do not cover every profile")
    errors.extend(_profile_errors(valid, profiles))
    errors.extend(_fault_errors(valid, profiles))
    lost, duplicates, _ = _lifecycle_accounting(valid)
    if lost:
        errors.append(f"lost accepted run(s) without terminal state: {', '.join(_lost_ids(valid))}")
    if duplicates:
        errors.append("duplicate accepted run identifiers were observed")
    states = {event.get("state") for row in valid for event in row["lifecycle"]}
    if profiles["overload_contract"]["drain"] and "draining" not in states:
        errors.append("drain behavior was not observed")
    if profiles["overload_contract"]["cancellation"] and "cancel-requested" not in states:
        errors.append("cancellation behavior was not observed")
    return errors
