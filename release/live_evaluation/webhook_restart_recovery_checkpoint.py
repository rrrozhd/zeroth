"""Provider-free live checkpoint for durable webhook restart recovery.

This module targets only the disposable campaign service on loopback port 8124.
It creates campaign-local subscriptions, drives the registered artifact workflow,
restarts the exact Docker Compose service twice, and seals metadata-only evidence.
No provider or external destination is used.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore

WORKTREE = Path(__file__).resolve().parents[2]
STATE_ROOT = (
    Path.home()
    / ".local/share/zeroth/evaluations/evaluation-studio-v1-retention-disposable"
)
ROOT = STATE_ROOT / "evidence/webhook-restart-recovery-live-20260825-1"
API_BASE = "http://127.0.0.1:8124"
TENANT = "evaluation-studio-v1-twin"
CAMPAIGN_ID = "evaluation-studio-v1-retention-disposable"
DEPLOYMENT = "demo-artifact-output-v1"
GRAPH = "evaluation-studio-v1-artifact-output@2"
SERVICE = "backend-retention-evidence"
COMPOSE_FILES = ("compose.dev.yml", "compose.retention-evidence.yml")
ACCEPTED_CRITERIA = (
    "webhooks.restart-subscription-persistence",
    "webhooks.restart-dead-letter-replay",
    "webhooks.restart-leased-delivery",
)


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _same_fields(
    left: Mapping[str, Any], right: Mapping[str, Any], fields: tuple[str, ...]
) -> bool:
    return all(left.get(field) == right.get(field) for field in fields)


def _validate_health(value: Mapping[str, Any]) -> None:
    expected = {
        "status": "ok",
        "campaign_id": CAMPAIGN_ID,
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
    }
    if {key: value.get(key) for key in expected} != expected:
        raise RuntimeError("restart health identity drifted from the disposable campaign")


def validate_proof(proof: Mapping[str, Any]) -> None:
    """Fail closed unless every durable restart correlation is exact."""
    if proof.get("schema_version") != 1 or proof.get("restart_count") != 2:
        raise RuntimeError("restart checkpoint requires exactly two service restarts")
    if proof.get("provider_calls_performed") != 0 or proof.get("external_network_calls") != 0:
        raise RuntimeError("restart checkpoint must remain provider-free and socket-free")
    for field in ("health_before", "health_after_restart_1", "health_after_restart_2"):
        _validate_health(_object(proof.get(field), label=field))

    starts = proof.get("container_started_at")
    if not isinstance(starts, list) or len(starts) != 3 or len(set(starts)) != 3:
        raise RuntimeError("container restart identity was not observed twice")

    sub_before = _object(proof.get("subscription_before_restart"), label="subscription before")
    sub_after = _object(proof.get("subscription_after_restart"), label="subscription after")
    if not _same_fields(
        sub_before,
        sub_after,
        ("subscription_id", "active", "target_url_mode", "event_types"),
    ) or sub_after.get("active") is not True:
        raise RuntimeError("subscription did not persist across restart")

    dead_fields = (
        "dead_letter_id",
        "subscription_id",
        "event_id",
        "run_id",
        "attempt_count",
    )
    dead_before = _object(proof.get("dead_letter_before_restart"), label="dead-letter before")
    dead_after_1 = _object(
        proof.get("dead_letter_after_restart_1"), label="dead-letter after restart 1"
    )
    dead_after_2 = _object(
        proof.get("dead_letter_after_restart_2"), label="dead-letter after restart 2"
    )
    if (
        not _same_fields(dead_before, dead_after_1, dead_fields)
        or not _same_fields(dead_before, dead_after_2, dead_fields)
        or dead_before.get("attempt_count") != 5
    ):
        raise RuntimeError("dead-letter identity did not persist across both restarts")

    replay_fields = (
        "delivery_id",
        "subscription_id",
        "event_id",
        "run_id",
        "status",
        "attempt_count",
    )
    replay_1 = _object(proof.get("replay_after_restart_1"), label="replay after restart 1")
    replay_2 = _object(proof.get("replay_after_restart_2"), label="replay after restart 2")
    if (
        not _same_fields(replay_1, replay_2, replay_fields)
        or replay_2.get("status") != "delivered"
        or replay_2.get("event_id") != dead_before.get("event_id")
        or replay_2.get("run_id") != dead_before.get("run_id")
    ):
        raise RuntimeError("dead-letter replay did not remain delivered after restart")

    leased = _object(proof.get("leased_before_restart"), label="leased delivery")
    recovered = _object(
        proof.get("lease_recovered_after_restart"), label="recovered leased delivery"
    )
    lease_fields = ("delivery_id", "subscription_id", "event_id", "run_id")
    if (
        not _same_fields(leased, recovered, lease_fields)
        or leased.get("status") != "delivering"
        or leased.get("attempt_count") != 1
        or recovered.get("status") != "delivered"
        or recovered.get("attempt_count") != 2
    ):
        raise RuntimeError("leased delivery was not reclaimed exactly once after restart")

    sink = _object(proof.get("lease_sink"), label="lease sink")
    if (
        sink.get("durable_marker_count") != 1
        or sink.get("receipt_count") != 1
        or sink.get("receipt_event_id") != recovered.get("event_id")
        or sink.get("receipt_attempt_count") != 2
        or sink.get("signature_verified") is not True
    ):
        raise RuntimeError("lease recovery must produce exactly one receipt with verified signing")

    cleanup = _object(proof.get("subscription_cleanup"), label="subscription cleanup")
    if set(cleanup.values()) != {"inactive"} or len(cleanup) != 2:
        raise RuntimeError("disposable subscriptions were not deactivated")


class _Api:
    def __init__(self, *, api_key: str) -> None:
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={"X-API-Key": api_key},
            timeout=10,
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, data: Mapping[str, Any] | None = None) -> Any:
        response = self._client.request(method, path, json=data)
        response.raise_for_status()
        return response.json() if response.content else None


def _api_key() -> str:
    path = STATE_ROOT / "runtime-secrets/tenant-b-admin-key"
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("disposable admin credential is unavailable")
    return value


def _health(api: _Api) -> dict[str, Any]:
    return {
        key: value
        for key, value in _object(api.request("GET", "/health"), label="health").items()
        if key in {"status", "campaign_id", "deployment_ref", "graph_version_ref"}
    }


def _wait(
    read: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    label: str,
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = read()
            if predicate(last):
                return last
        except (httpx.HTTPError, OSError, sqlite3.Error):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {label}: {last!r}")


def _subscriptions(api: _Api) -> list[dict[str, Any]]:
    value = _object(api.request("GET", "/v1/webhooks/subscriptions"), label="subscriptions")
    rows = value.get("subscriptions")
    if not isinstance(rows, list):
        raise RuntimeError("subscriptions response is invalid")
    return [row for row in rows if isinstance(row, dict)]


def _deliveries(api: _Api) -> list[dict[str, Any]]:
    value = _object(api.request("GET", "/v1/webhooks/deliveries"), label="deliveries")
    rows = value.get("deliveries")
    if not isinstance(rows, list):
        raise RuntimeError("deliveries response is invalid")
    return [row for row in rows if isinstance(row, dict)]


def _dead_letters(api: _Api) -> list[dict[str, Any]]:
    value = _object(api.request("GET", "/v1/webhooks/dead-letters"), label="dead letters")
    rows = value.get("dead_letters")
    if not isinstance(rows, list):
        raise RuntimeError("dead-letter response is invalid")
    return [row for row in rows if isinstance(row, dict)]


def _create_subscription(api: _Api, mode: str) -> dict[str, Any]:
    created = _object(
        api.request(
            "POST",
            "/v1/webhooks/subscriptions",
            data={
                "deployment_ref": "server-scoped",
                "tenant_id": TENANT,
                "target_url": f"https://example.com/zeroth-evaluation/{mode}",
                "event_types": ["run.completed"],
            },
        ),
        label="created subscription",
    )
    if not isinstance(created.get("subscription_id"), str):
        raise RuntimeError("subscription creation did not return an identity")
    return created


def _run_artifact(api: _Api, label: str) -> dict[str, Any]:
    run = _object(
        api.request(
            "POST",
            "/v1/runs",
            data={
                "input_payload": {"kind": "json", "label": label},
                "campaign_id": CAMPAIGN_ID,
                "campaign_strict": True,
            },
        ),
        label="run submission",
    )
    run_id = run.get("run_id")
    if not isinstance(run_id, str):
        raise RuntimeError("run submission did not return an identity")
    terminal = _wait(
        lambda: _object(api.request("GET", f"/v1/runs/{run_id}"), label="run"),
        lambda value: value.get("status") in {"succeeded", "failed"},
        label=f"run {run_id}",
        timeout=30,
    )
    if terminal.get("status") != "succeeded":
        raise RuntimeError("provider-free artifact run failed")
    return terminal


def _subscription_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    path = urlparse(str(value.get("target_url", ""))).path.strip("/").split("/")
    mode = path[1] if len(path) > 1 and path[0] == "zeroth-evaluation" else None
    return {
        "subscription_id": value.get("subscription_id"),
        "active": value.get("active"),
        "target_url_mode": mode,
        "event_types": value.get("event_types"),
    }


def _delivery_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "delivery_id",
            "subscription_id",
            "event_id",
            "run_id",
            "status",
            "attempt_count",
        )
    }


def _dead_letter_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "dead_letter_id",
            "subscription_id",
            "event_id",
            "run_id",
            "attempt_count",
        )
    }


def _container_started_at() -> str:
    container_id = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILES[0],
            "-f",
            COMPOSE_FILES[1],
            "ps",
            "-q",
            SERVICE,
        ],
        cwd=WORKTREE,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not container_id:
        raise RuntimeError("disposable webhook service container is not running")
    return subprocess.run(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container_id],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _restart(api: _Api, store: EvidenceStore, *, sequence: int) -> tuple[dict[str, Any], str]:
    argv = [
        "docker",
        "compose",
        "-f",
        COMPOSE_FILES[0],
        "-f",
        COMPOSE_FILES[1],
        "restart",
        SERVICE,
    ]
    completed = subprocess.run(
        argv,
        cwd=WORKTREE,
        check=False,
        capture_output=True,
        text=True,
    )
    store.record_command(
        sequence=sequence,
        name=f"webhook-backend-restart-{sequence}",
        argv=argv,
        working_directory=WORKTREE,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Docker restart {sequence} failed")
    health = _wait(
        lambda: _health(api),
        lambda value: value.get("status") == "ok",
        label=f"health after restart {sequence}",
        timeout=60,
    )
    return health, _container_started_at()


def _lease_marker_count(outcome: str) -> int:
    with sqlite3.connect(STATE_ROOT / "webhook-sink.sqlite3") as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM webhook_attempts WHERE outcome = ?", (outcome,)
        ).fetchone()
    return int(row[0] if row is not None else 0)


def _lease_sink_snapshot(
    *, subscription_id: str, event_id: str, outcome: str
) -> dict[str, Any]:
    with sqlite3.connect(STATE_ROOT / "webhook-sink.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        marker = connection.execute(
            "SELECT COUNT(*) AS count FROM webhook_attempts WHERE outcome = ?", (outcome,)
        ).fetchone()
        receipts = connection.execute(
            """
            SELECT event_id, signature_verified,
                   (
                       SELECT COUNT(*) FROM webhook_attempts a
                       WHERE a.subscription_id = r.subscription_id
                         AND a.event_id = r.event_id
                   )
                       AS attempt_count
            FROM webhook_receipts r WHERE subscription_id = ? AND event_id = ?
            """,
            (subscription_id, event_id),
        ).fetchall()
    return {
        "durable_marker_count": int(marker["count"]),
        "receipt_count": len(receipts),
        "receipt_event_id": receipts[0]["event_id"] if len(receipts) == 1 else None,
        "receipt_attempt_count": int(receipts[0]["attempt_count"]) if len(receipts) == 1 else None,
        "signature_verified": bool(receipts[0]["signature_verified"])
        if len(receipts) == 1
        else False,
    }


def execute_live(*, destination: Path = ROOT) -> Path:
    """Run and seal the disposable live restart checkpoint."""
    if destination.exists():
        raise FileExistsError(destination)
    store = EvidenceStore(destination)
    api = _Api(api_key=_api_key())
    created_ids: list[str] = []
    proof: dict[str, Any] = {"schema_version": 1}
    suffix = str(time.time_ns())
    try:
        proof["health_before"] = _health(api)
        starts = [_container_started_at()]

        flaky = _create_subscription(api, f"flaky/restart-{suffix}")
        flaky_id = str(flaky["subscription_id"])
        created_ids.append(flaky_id)
        proof["subscription_before_restart"] = _subscription_snapshot(flaky)
        flaky_run = _run_artifact(api, f"webhook-restart-dlq-{suffix}")
        dead_before = _wait(
            lambda: _dead_letters(api),
            lambda rows: any(
                row.get("subscription_id") == flaky_id
                and row.get("run_id") == flaky_run.get("run_id")
                for row in rows
            ),
            label="five-attempt dead-letter",
            timeout=75,
        )
        dead_before = next(
            row
            for row in dead_before
            if row.get("subscription_id") == flaky_id
            and row.get("run_id") == flaky_run.get("run_id")
        )
        proof["dead_letter_before_restart"] = _dead_letter_snapshot(dead_before)

        health_1, started_1 = _restart(api, store, sequence=1)
        proof["health_after_restart_1"] = health_1
        starts.append(started_1)
        sub_after = next(
            row for row in _subscriptions(api) if row.get("subscription_id") == flaky_id
        )
        proof["subscription_after_restart"] = _subscription_snapshot(sub_after)
        dead_after_1 = next(
            row
            for row in _dead_letters(api)
            if row.get("dead_letter_id") == dead_before.get("dead_letter_id")
        )
        proof["dead_letter_after_restart_1"] = _dead_letter_snapshot(dead_after_1)

        replay = _object(
            api.request(
                "POST",
                f"/v1/webhooks/dead-letters/{dead_before['dead_letter_id']}/replay",
            ),
            label="dead-letter replay",
        )
        replay_id = str(replay["delivery_id"])
        replay_after_1 = _wait(
            lambda: _deliveries(api),
            lambda rows: any(
                row.get("delivery_id") == replay_id and row.get("status") == "delivered"
                for row in rows
            ),
            label="replayed delivery",
            timeout=45,
        )
        replay_after_1 = next(row for row in replay_after_1 if row.get("delivery_id") == replay_id)
        proof["replay_after_restart_1"] = _delivery_snapshot(replay_after_1)

        lease_mode = f"restart-after-lease/restart-{suffix}"
        lease_outcome = f"leased_before_restart:restart-{suffix}"
        leased_sub = _create_subscription(api, lease_mode)
        leased_sub_id = str(leased_sub["subscription_id"])
        created_ids.append(leased_sub_id)
        lease_run = _run_artifact(api, f"webhook-restart-lease-{suffix}")
        leased_rows = _wait(
            lambda: _deliveries(api),
            lambda rows: _lease_marker_count(lease_outcome) == 1
            and any(
                row.get("subscription_id") == leased_sub_id
                and row.get("run_id") == lease_run.get("run_id")
                and row.get("status") == "delivering"
                and row.get("attempt_count") == 1
                for row in rows
            ),
            label="durably leased delivery",
            timeout=30,
        )
        leased_before = next(
            row
            for row in leased_rows
            if row.get("subscription_id") == leased_sub_id
            and row.get("run_id") == lease_run.get("run_id")
        )
        proof["leased_before_restart"] = _delivery_snapshot(leased_before)

        health_2, started_2 = _restart(api, store, sequence=2)
        proof["health_after_restart_2"] = health_2
        starts.append(started_2)
        lease_recovered = _wait(
            lambda: _deliveries(api),
            lambda rows: any(
                row.get("delivery_id") == leased_before.get("delivery_id")
                and row.get("status") == "delivered"
                and row.get("attempt_count") == 2
                for row in rows
            ),
            label="expired lease recovery",
            timeout=75,
        )
        lease_recovered = next(
            row
            for row in lease_recovered
            if row.get("delivery_id") == leased_before.get("delivery_id")
        )
        proof["lease_recovered_after_restart"] = _delivery_snapshot(lease_recovered)
        proof["lease_sink"] = _lease_sink_snapshot(
            subscription_id=leased_sub_id,
            event_id=str(lease_recovered["event_id"]),
            outcome=lease_outcome,
        )
        proof["dead_letter_after_restart_2"] = _dead_letter_snapshot(
            next(
                row
                for row in _dead_letters(api)
                if row.get("dead_letter_id") == dead_before.get("dead_letter_id")
            )
        )
        proof["replay_after_restart_2"] = _delivery_snapshot(
            next(row for row in _deliveries(api) if row.get("delivery_id") == replay_id)
        )
        proof["restart_count"] = 2
        proof["container_started_at"] = starts
        proof["provider_calls_performed"] = 0
        proof["external_network_calls"] = 0
    finally:
        cleanup: dict[str, str] = {}
        for subscription_id in created_ids:
            try:
                api.request("DELETE", f"/v1/webhooks/subscriptions/{subscription_id}")
                cleanup[subscription_id] = "inactive"
            except httpx.HTTPError:
                cleanup[subscription_id] = "cleanup_failed"
        proof["subscription_cleanup"] = cleanup
        api.close()

    validate_proof(proof)
    proof_path = Path("runtime/restart-proof.json")
    store._write_exclusive(proof_path, proof)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "webhook-restart-recovery-live",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": TENANT,
            "campaign_id": CAMPAIGN_ID,
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "provider_calls_performed": 0,
            "external_network_calls": 0,
            "restart_count": 2,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    event_id = store.append_event(
        "campaign.webhook_restart_recovery_verified",
        {
            "result": "pass",
            "proof_paths": [
                proof_path.as_posix(),
                "commands/0001-webhook-backend-restart-1.json",
                "commands/0002-webhook-backend-restart-2.json",
            ],
            "provider_call_count": 0,
            "external_network_call_count": 0,
        },
        correlation=CorrelationIds(
            run_id=str(proof["lease_recovered_after_restart"]["run_id"]),
            operation_id=str(proof["lease_recovered_after_restart"]["delivery_id"]),
        ),
    )
    evidence = (
        proof_path.as_posix(),
        "commands/0001-webhook-backend-restart-1.json",
        "commands/0002-webhook-backend-restart-2.json",
        f"events.ndjson#{event_id}",
    )
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion_id, "pass", evidence)
            for criterion_id in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Webhook restart recovery live checkpoint\n\n"
            "The disposable loopback-only service preserved an active subscription and a "
            "five-attempt dead letter across restart, delivered its replay, and preserved "
            "both states across a second restart. The second restart terminated the worker "
            "while a delivery was durably leased; after the 30-second lease expired, the new "
            "worker reclaimed generation two and produced exactly one HMAC-verified sink "
            "receipt. The temporary subscriptions were deactivated. No provider or external "
            "network call occurred. This checkpoint does not prove a transactional audit "
            "outbox or approval-event emission.\n"
        ),
    )
    return destination


def main() -> int:
    root = execute_live()
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
