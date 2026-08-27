"""Seal the exact native-Safari resilient-HTTP journey from the live service."""

from __future__ import annotations

import argparse
import json
import sqlite3
import stat
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .control_plane import dirty_tree_hash
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore, _assert_safe

TENANT = "evaluation-studio-v1"
DEPLOYMENT = "provider-free-resilient-http-dual-browser-20260826-2"
GRAPH = "2ad25244-e051-4dff-9c72-a1ee813e92bd@1"
HEALTH = {
    "status": "ok",
    "campaign_id": TENANT,
    "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
    "deployment_version": 1,
    "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
}
RUNS = (
    "f9a333bc7d0746479815e4a6d8a2f8fe",
    "375a3f9b65e045068977da5f421152ce",
    "3d1d7ad79e7549c9a862c8ceb9424758",
    "7c30227f5b48446b8f91041e0f880336",
    "43bfd98093584b429024c6d2cc18a5ea",
)
EXPECTED_STATUSES = ("succeeded", "failed", "failed", "failed", "succeeded")
SCREENSHOTS = (
    "01-configured-workflow.png",
    "02-retry-succeeded.png",
    "03-timeout-failed.png",
    "04-circuit-open.png",
    "05-recovery-succeeded.png",
)
CRITERIA = (
    "resilient-http.native-safari-configured",
    "resilient-http.retry-success",
    "resilient-http.timeout-exhaustion",
    "resilient-http.first-circuit-failure",
    "resilient-http.circuit-open",
    "resilient-http.recovery",
    "resilient-http.sanitized-signed-audit",
    "resilient-http.zero-provider-economics",
    "resilient-http.d012-current-health-preserved",
)

RequestJson = Callable[[str, str, str], tuple[int, dict[str, object]]]


def _object(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not a JSON object")
    _assert_safe(value)
    return value


def _sequence(value: object, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"{label} is not an array of objects")
    for item in value:
        _assert_safe(item)
    return value


def _live_requester(base_url: str) -> RequestJson:
    def request_json(method: str, path: str, api_key: str) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{base_url}{path}",
            method=method,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=b"{}" if method == "POST" else None,
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 -- loopback only
                payload = json.load(response)
                return int(response.status), _object(payload, label=path)
        except HTTPError as exc:
            try:
                payload = json.load(exc)
            except (UnicodeDecodeError, json.JSONDecodeError) as decode_error:
                raise RuntimeError(f"non-JSON service response at {path}") from decode_error
            return int(exc.code), _object(payload, label=path)
        except (OSError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"service request failed at {path}") from exc

    return request_json


def _secure_api_key(path: Path, *, repository_root: Path, state_root: Path) -> str:
    path = path.expanduser().resolve(strict=True)
    if path != state_root / "runtime-secrets" / "service-api-key":
        raise ValueError("service API key must use the external runtime-secrets reference")
    if path == repository_root or repository_root in path.parents:
        raise ValueError("service API key must remain outside the repository")
    metadata = path.stat()
    if (
        not path.is_file()
        or metadata.st_size == 0
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise RuntimeError("service API key reference is unavailable or not private")
    value = path.read_text(encoding="utf-8").strip()
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("service API key reference is invalid")
    return value


def _service_call(
    request_json: RequestJson,
    method: str,
    path: str,
    api_key: str,
) -> dict[str, Any]:
    status, payload = request_json(method, path, api_key)
    if status != 200:
        raise RuntimeError(f"service endpoint did not return 200: {method} {path}")
    return _object(payload, label=f"{method} {path}")


def _validate_run(run: Mapping[str, Any], *, run_id: str, status: str) -> None:
    expected = {
        "run_id": run_id,
        "thread_id": run_id,
        "tenant_id": TENANT,
        "campaign_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
        "status": status,
    }
    if any(run.get(field) != value for field, value in expected.items()):
        raise RuntimeError(f"run identity or status drifted: {run_id}")
    if status == "succeeded" and run.get("failure_state") is not None:
        raise RuntimeError(f"successful run has failure state: {run_id}")
    if status == "failed":
        failure = run.get("failure_state")
        if not isinstance(failure, Mapping) or failure.get("reason") != "node_execution_failed":
            raise RuntimeError(f"failed run has no exact failure state: {run_id}")


def _project_run(run: Mapping[str, Any]) -> dict[str, object]:
    return {
        field: run.get(field)
        for field in (
            "run_id",
            "thread_id",
            "tenant_id",
            "campaign_id",
            "deployment_ref",
            "graph_version_ref",
            "status",
            "failure_state",
            "terminal_output",
            "audit_refs",
        )
    }


def _validate_audits(records: list[dict[str, Any]], *, run_id: str) -> list[dict[str, object]]:
    if not records:
        raise RuntimeError(f"run has no audit records: {run_id}")
    projected: list[dict[str, object]] = []
    for sequence, record in enumerate(records, start=1):
        signature = record.get("record_signature")
        digest = record.get("record_digest")
        metadata = record.get("execution_metadata")
        if (
            record.get("audit_id") != f"{run_id}:audit:{sequence}"
            or record.get("run_id") != run_id
            or record.get("thread_id") != run_id
            or record.get("deployment_ref") != DEPLOYMENT
            or record.get("graph_version_ref") != GRAPH
            or record.get("chain_sequence") != sequence
            or record.get("status") not in {"completed", "failed"}
            or not isinstance(signature, str)
            or len(signature) != 64
            or not isinstance(digest, str)
            or len(digest) != 64
            or record.get("signing_key_id") != "dev-local"
            or record.get("signing_algorithm") != "HS256"
            or record.get("cost_usd") != 0.0
            or record.get("estimated_cost_usd") != 0.0
            or record.get("cost_event_id") is not None
            or record.get("provider_request_id") is not None
            or not isinstance(metadata, Mapping)
        ):
            raise RuntimeError(f"audit is unsigned, uncorrelated, or priced: {run_id}")
        serialized_metadata = json.dumps(metadata, sort_keys=True).lower()
        if any(token in serialized_metadata for token in ("http://", "https://", "127.0.0.1")):
            raise RuntimeError(f"audit retains a raw HTTP target: {run_id}")
        http_metadata = {
            field: metadata.get(field)
            for field in (
                "node_kind",
                "reason_code",
                "retry_count",
                "upstream_status_code",
                "duration_ms",
                "target_url_sha256",
            )
            if field in metadata
        }
        if http_metadata:
            target_digest = http_metadata.get("target_url_sha256")
            if (
                http_metadata.get("node_kind") != "http_request"
                or not isinstance(target_digest, str)
                or len(target_digest) != 64
            ):
                raise RuntimeError(f"HTTP audit metadata is incomplete: {run_id}")
        projected.append(
            {
                "audit_id": record["audit_id"],
                "run_id": run_id,
                "thread_id": run_id,
                "deployment_ref": DEPLOYMENT,
                "graph_version_ref": GRAPH,
                "node_id": record.get("node_id"),
                "status": record.get("status"),
                "chain_sequence": sequence,
                "record_digest": digest,
                "record_signature": signature,
                "signing_key_id": "dev-local",
                "signing_algorithm": "HS256",
                "cost_usd": 0.0,
                "estimated_cost_usd": 0.0,
                "cost_event_id": None,
                "provider_request_id": None,
                "http_metadata": http_metadata,
            }
        )
    return projected


def _http_audit(records: list[dict[str, object]], *, run_id: str) -> dict[str, object]:
    matches = [
        record
        for record in records
        if isinstance(record.get("http_metadata"), Mapping) and bool(record["http_metadata"])
    ]
    if len(matches) != 1:
        raise RuntimeError(f"run does not have exactly one HTTP audit: {run_id}")
    return matches[0]


def _validate_scenario(
    run: Mapping[str, Any], audits: list[dict[str, object]], *, index: int
) -> None:
    run_id = RUNS[index]
    http = _http_audit(audits, run_id=run_id)
    metadata = http["http_metadata"]
    assert isinstance(metadata, Mapping)
    failure = run.get("failure_state")
    terminal = run.get("terminal_output")
    if index == 0:
        response = terminal.get("http_response") if isinstance(terminal, Mapping) else None
        body = response.get("body") if isinstance(response, Mapping) else None
        valid = (
            http.get("node_id") == "http-retry"
            and http.get("status") == "completed"
            and metadata.get("retry_count") == 2
            and metadata.get("upstream_status_code") == 200
            and isinstance(body, Mapping)
            and body.get("scenario") == "retry-then-success"
            and body.get("attempt") == 3
        )
    elif index == 1:
        valid = (
            http.get("node_id") == "http-timeout"
            and http.get("status") == "failed"
            and metadata.get("reason_code") == "http_retry_exhausted_error"
            and metadata.get("retry_count") == 2
            and isinstance(failure, Mapping)
            and str(failure.get("message", "")).startswith("All 2 retry attempts exhausted")
        )
    elif index == 2:
        valid = (
            http.get("node_id") == "http-circuit"
            and http.get("status") == "failed"
            and metadata.get("reason_code") == "http_retry_exhausted_error"
            and metadata.get("retry_count") == 0
            and isinstance(failure, Mapping)
            and str(failure.get("message", "")).endswith("HTTP 503")
        )
    elif index == 3:
        valid = (
            http.get("node_id") == "http-circuit"
            and http.get("status") == "failed"
            and metadata.get("reason_code") == "circuit_open_error"
            and metadata.get("retry_count") == 0
            and metadata.get("duration_ms") == 0.0
            and isinstance(failure, Mapping)
            and str(failure.get("message", "")).startswith("Circuit breaker open")
        )
    else:
        response = terminal.get("http_response") if isinstance(terminal, Mapping) else None
        body = response.get("body") if isinstance(response, Mapping) else None
        valid = (
            http.get("node_id") == "http-circuit"
            and http.get("status") == "completed"
            and metadata.get("retry_count") == 0
            and metadata.get("upstream_status_code") == 200
            and isinstance(body, Mapping)
            and body.get("scenario") == "circuit"
            and body.get("recovered") is True
        )
    if not valid:
        raise RuntimeError(f"resilient-HTTP scenario evidence is not exact: {run_id}")


def _validate_summary(summary: object, *, audit_count: int, run_id: str) -> dict[str, object]:
    value = _object(summary, label=f"evidence summary {run_id}")
    expected = {
        "audit_count": audit_count,
        "priced_call_count": 0,
        "cost_event_count": 0,
        "total_cost_usd": 0.0,
        "cost_identity_state": "not_applicable_no_priced_call",
        "reconciliation_state": "reconciled_zero_activity",
    }
    if any(value.get(field) != expected_value for field, expected_value in expected.items()):
        raise RuntimeError(f"provider-free economics did not reconcile: {run_id}")
    return expected


def _validate_verification(
    verification: Mapping[str, Any], *, run_id: str, audit_count: int
) -> dict[str, object]:
    expected = {
        "scope": f"run:{run_id}",
        "verified": True,
        "signature_verified": True,
        "record_count": audit_count,
        "unsigned_record_count": 0,
        "signing_key_id": "dev-local",
        "failed_audit_id": None,
        "error": None,
    }
    if any(verification.get(field) != value for field, value in expected.items()):
        raise RuntimeError(f"signed audit chain did not verify: {run_id}")
    return expected


def _validate_economics(database: Path, *, state_root: Path) -> dict[str, object]:
    database = database.expanduser().resolve(strict=True)
    if database != state_root / "econ.db":
        raise ValueError("authoritative economics database path drifted")
    if (state_root / "econ_plane.db").exists():
        raise RuntimeError("legacy econ_plane.db must be absent")
    placeholders = ",".join("?" for _ in RUNS)
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            event_count = connection.execute(
                f"""SELECT COUNT(*) FROM execution_events
                WHERE execution_id IN ({placeholders})
                   OR join_key IN ({placeholders})
                   OR json_extract(metadata, '$.run_id') IN ({placeholders})""",
                (*RUNS, *RUNS, *RUNS),
            ).fetchone()[0]
            reservation_count = connection.execute(
                f"SELECT COUNT(*) FROM cost_reservations WHERE run_id IN ({placeholders})",
                RUNS,
            ).fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("authoritative economics query failed") from exc
    if integrity != ("ok",) or event_count != 0 or reservation_count != 0:
        raise RuntimeError("provider-free run IDs have economics activity")
    return {
        "database_integrity": "ok",
        "execution_event_count": 0,
        "reservation_count": 0,
        "provider_call_count": 0,
        "cost_event_count": 0,
        "total_cost_usd": 0.0,
        "legacy_econ_plane_database_present": False,
    }


def _screenshots(source_root: Path) -> tuple[tuple[Path, str], ...]:
    if {path.name for path in (source_root / "screenshots").iterdir() if path.is_file()} != set(
        SCREENSHOTS
    ):
        raise RuntimeError("native Safari screenshot inventory is not exact")
    values: list[tuple[Path, str]] = []
    for name in SCREENSHOTS:
        source = source_root / "screenshots" / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"native Safari screenshot is missing: {name}")
        payload = source.read_bytes()
        if len(payload) < 256 or not payload.startswith(b"\xff\xd8\xff"):
            raise RuntimeError(f"native Safari screenshot is not a JPEG capture: {name}")
        values.append((source, f"screenshots/{Path(name).stem}.jpg"))
    return tuple(values)


def build_checkpoint(
    *,
    source_root: Path,
    destination: Path,
    repository_root: Path,
    state_root: Path,
    service_api_key_path: Path,
    econ_database: Path,
    base_url: str = "http://127.0.0.1:8122",
    request_json: RequestJson | None = None,
) -> Path:
    """Reconcile live runtime evidence, then append and seal the accepted checkpoint."""
    repository_root = repository_root.expanduser().resolve(strict=True)
    state_root = state_root.expanduser().resolve(strict=True)
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if source_root != state_root / "staging/native-safari-resilient-http-20260826-1":
        raise ValueError("native Safari staging root is not exact")
    if destination != state_root / "evidence/native-safari-resilient-http-accepted-20260826-1":
        raise ValueError("native Safari evidence destination is not exact")
    if destination.exists():
        raise FileExistsError(destination)
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 8122
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("service origin must be exact loopback 127.0.0.1:8122")
    screenshot_sources = _screenshots(source_root)
    api_key = _secure_api_key(
        service_api_key_path, repository_root=repository_root, state_root=state_root
    )
    requester = request_json or _live_requester(base_url.rstrip("/"))
    health = _service_call(requester, "GET", "/health", api_key)
    if health != HEALTH:
        raise RuntimeError("current D-012 health is not exact")

    run_evidence: list[dict[str, object]] = []
    run_projections: list[dict[str, object]] = []
    verification_projections: list[dict[str, object]] = []
    for index, (run_id, expected_status) in enumerate(zip(RUNS, EXPECTED_STATUSES, strict=True)):
        run = _service_call(requester, "GET", f"/v1/runs/{run_id}", api_key)
        timeline = _service_call(requester, "GET", f"/v1/runs/{run_id}/timeline", api_key)
        evidence = _service_call(requester, "GET", f"/v1/runs/{run_id}/evidence", api_key)
        verification = _service_call(requester, "POST", f"/v1/runs/{run_id}/verify-chain", api_key)
        _validate_run(run, run_id=run_id, status=expected_status)
        evidence_run = _object(evidence.get("run"), label=f"evidence run {run_id}")
        _validate_run(evidence_run, run_id=run_id, status=expected_status)
        if _project_run(evidence_run) != _project_run(run):
            raise RuntimeError(f"run and evidence endpoints disagree: {run_id}")
        audits = _sequence(evidence.get("audits"), label=f"audits {run_id}")
        projected_audits = _validate_audits(audits, run_id=run_id)
        entries = _sequence(timeline.get("entries"), label=f"timeline {run_id}")
        if (
            timeline.get("deployment_ref") != DEPLOYMENT
            or timeline.get("run_id") != run_id
            or [entry.get("audit_id") for entry in entries]
            != [record["audit_id"] for record in projected_audits]
            or any(
                entry.get("run_id") != run_id
                or entry.get("thread_id") != run_id
                or entry.get("deployment_ref") != DEPLOYMENT
                or entry.get("graph_version_ref") != GRAPH
                or entry.get("cost_usd") != 0.0
                or entry.get("estimated_cost_usd") != 0.0
                or entry.get("cost_event_id") is not None
                or entry.get("provider_request_id") is not None
                for entry in entries
            )
        ):
            raise RuntimeError(f"timeline and signed evidence disagree: {run_id}")
        _validate_scenario(run, projected_audits, index=index)
        summary = _validate_summary(evidence.get("summary"), audit_count=len(audits), run_id=run_id)
        verification_projection = _validate_verification(
            verification, run_id=run_id, audit_count=len(audits)
        )
        run_projections.append(_project_run(run))
        run_evidence.append({"run_id": run_id, "summary": summary, "audits": projected_audits})
        verification_projections.append(verification_projection)

    economics = _validate_economics(econ_database, state_root=state_root)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(revision) != 40:
        raise RuntimeError("repository revision is invalid")
    manifest = {
        "schema_version": 1,
        "checkpoint": "native-safari-resilient-http-accepted-20260826-1",
        "created_at": datetime.now(UTC).isoformat(),
        "tenant_id": TENANT,
        "revision": revision,
        "diff_sha256": dirty_tree_hash(repository_root).removeprefix("sha256:"),
        "source_root": str(source_root),
        "served_identity": health,
        "fixture_identity": {
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
        },
        "run_ids": list(RUNS),
        "provider_calls_performed": 0,
        "total_cost_usd": 0.0,
        "native_safari_screenshot_count": len(SCREENSHOTS),
        "legacy_econ_plane_database_present": False,
    }
    for value in (manifest, run_projections, run_evidence, verification_projections, economics):
        _assert_safe(value)

    store = EvidenceStore(destination)
    store.write_manifest(manifest)
    store._write_exclusive(Path("runtime/health.json"), health)
    store._write_exclusive(Path("runtime/runs.json"), run_projections)
    store._write_exclusive(Path("runtime/evidence.json"), run_evidence)
    store._write_exclusive(Path("runtime/verify-chain.json"), verification_projections)
    store._write_exclusive(Path("runtime/economics.json"), economics)
    evidence_paths = [
        "runtime/health.json",
        "runtime/runs.json",
        "runtime/evidence.json",
        "runtime/verify-chain.json",
        "runtime/economics.json",
    ]
    for source, relative in screenshot_sources:
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative)
    event_refs: list[str] = []
    for run_id, status in zip(RUNS, EXPECTED_STATUSES, strict=True):
        event_id = store.append_event(
            "campaign.run.native_safari_resilient_http_verified",
            {
                "result": "pass",
                "status": status,
                "deployment_ref": DEPLOYMENT,
                "graph_version_ref": GRAPH,
                "provider_call_count": 0,
                "cost_event_count": 0,
                "total_cost_usd": 0.0,
            },
            correlation=CorrelationIds(run_id=run_id),
        )
        event_refs.append(f"events.ndjson#{event_id}")
    references = tuple([*evidence_paths, *event_refs])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", references) for criterion in CRITERIA
        ),
        report_markdown=(
            "# Native Safari resilient HTTP checkpoint\n\n"
            "Native Safari configured the exact provider-free resilient-HTTP graph and displayed "
            "retry success, timeout exhaustion, circuit-open refusal, and recovery. Five exact "
            "service runs reconcile with their timeline, per-run evidence, signed `dev-local` "
            "chain verification, and the authoritative economics database. The retry path used "
            "two retries before HTTP 200; the timeout exhausted two retries; the first circuit "
            "request failed on HTTP 503; the next request was refused with `circuit_open_error`; "
            "and the recovery request returned HTTP 200. Provider request IDs, cost-event IDs, "
            "reservations, execution events, and total cost are all exactly zero. Current health "
            "still reports the frozen D-012 deployment and graph. The obsolete root-level "
            "`econ_plane.db` is absent. Screenshots are native Safari captures; API observations "
            "were collected only from loopback using an ephemeral external secret reference.\n"
        ),
    )
    store.scan_recursive()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--service-api-key-path", type=Path, required=True)
    parser.add_argument("--econ-database", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        source_root=args.source_root,
        destination=args.destination,
        repository_root=args.repository_root,
        state_root=args.state_root,
        service_api_key_path=args.service_api_key_path,
        econ_database=args.econ_database,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
