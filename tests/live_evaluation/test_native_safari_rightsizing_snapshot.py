from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from release.live_evaluation.native_safari_rightsizing_snapshot import (
    collect_snapshot,
    read_docker_access_log,
    fetch_loopback_health,
    write_json_atomic,
)


TENANT = "evaluation-studio-v1"


def test_reads_active_backend_logs_with_exact_noninteractive_docker_command() -> None:
    calls: list[tuple[object, ...]] = []

    class Result:
        returncode = 0
        stdout = (
            'INFO: 127.0.0.1:50001 - "POST /v1/econ/rightsizing/experiment '
            'HTTP/1.1" 422 Unprocessable Entity\n'
        )
        stderr = ""

    def runner(*args: object, **kwargs: object) -> Result:
        calls.append((*args, kwargs))
        return Result()

    assert read_docker_access_log("zeroth-dev-backend-1", runner=runner) == Result.stdout
    assert calls == [
        (
            ["docker", "logs", "zeroth-dev-backend-1"],
            {
                "capture_output": True,
                "check": False,
                "text": True,
                "timeout": 10.0,
            },
        )
    ]


@pytest.mark.parametrize(
    ("container", "message"),
    (
        ("backend", "exact campaign backend"),
        ("zeroth-dev-backend-1; env", "exact campaign backend"),
    ),
)
def test_docker_log_reader_rejects_unpinned_container(container: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        read_docker_access_log(container)


def test_docker_log_reader_fails_closed_on_command_error() -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "daemon unavailable"

    with pytest.raises(RuntimeError, match="docker logs failed"):
        read_docker_access_log(
            "zeroth-dev-backend-1", runner=lambda *args, **kwargs: Result()
        )


def _databases(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    service = tmp_path / "zeroth.db"
    economics = tmp_path / "econ.db"
    with sqlite3.connect(service) as database:
        database.executescript(
            """
            CREATE TABLE runs (run_id TEXT NOT NULL, tenant_id TEXT NOT NULL);
            CREATE TABLE node_audits (
                audit_id TEXT NOT NULL, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
                graph_version_ref TEXT NOT NULL, deployment_ref TEXT NOT NULL,
                created_at TEXT NOT NULL, record_json TEXT NOT NULL, cost_usd REAL,
                cost_event_id TEXT, chain_sequence INTEGER, tenant_id TEXT NOT NULL
            );
            """
        )
        database.executemany(
            "INSERT INTO runs VALUES (?, ?)",
            (("run-z", TENANT), ("run-a", TENANT), ("other-run", "tenant-b")),
        )
        database.executemany(
            "INSERT INTO node_audits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "audit-a",
                    "run-a",
                    "research",
                    "graph@1",
                    "deployment",
                    "2026-08-26T12:00:00Z",
                    '{"result":"ok"}',
                    0.01,
                    "cost-a",
                    1,
                    TENANT,
                ),
                (
                    "other-audit",
                    "other-run",
                    "research",
                    "graph@1",
                    "deployment",
                    "2026-08-26T12:00:00Z",
                    "{}",
                    99.0,
                    "other-cost",
                    1,
                    "tenant-b",
                ),
            ),
        )
    with sqlite3.connect(economics) as database:
        database.executescript(
            """
            CREATE TABLE execution_events (
                id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL,
                execution_id TEXT NOT NULL, provider_request_id TEXT,
                token_cost_usd NUMERIC, tool_cost_usd NUMERIC,
                compute_cost_usd NUMERIC
            );
            CREATE TABLE cost_reservations (
                id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL,
                operation_id TEXT NOT NULL, provider_request_id TEXT,
                held_cost_usd NUMERIC NOT NULL, actual_cost_usd NUMERIC
            );
            """
        )
        database.executemany(
            "INSERT INTO execution_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (1, TENANT, "execution-a", "provider-a", "0.001", "0.002", "0.003"),
                (2, TENANT, "execution-b", None, "0.004", None, None),
                (3, "tenant-b", "other-execution", "other-provider", "99", "0", "0"),
            ),
        )
        database.executemany(
            "INSERT INTO cost_reservations VALUES (?, ?, ?, ?, ?, ?)",
            (
                (1, TENANT, "operation-a", "provider-a", "0.010", "0.006"),
                (2, TENANT, "operation-b", "provider-reservation-only", "0.020", None),
                (3, "tenant-b", "other-operation", "other-provider", "99", "99"),
            ),
        )
    return service, economics


def _access_log(tmp_path: Path) -> Path:
    path = tmp_path / "backend-access.log"
    path.write_text(
        "\n".join(
            (
                'INFO: 127.0.0.1:50000 - "GET /v1/econ/rightsizing/experiment HTTP/1.1" 405 Method Not Allowed',
                'INFO: 127.0.0.1:50001 - "POST /v1/econ/rightsizing/experiment HTTP/1.1" 422 Unprocessable Entity',
                'INFO: 127.0.0.1:50002 - "POST /v1/econ/rightsizing/experiment?x=1 HTTP/1.1" 422 Unprocessable Entity',
                'INFO: 127.0.0.1:50003 - "POST /econ/rightsizing/experiment HTTP/1.1" 200 OK',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_collects_exact_sealer_planes_tenant_scoped_without_exposing_identities(
    tmp_path: Path,
) -> None:
    service, economics = _databases(tmp_path)
    snapshot = collect_snapshot(
        service_database=service,
        economics_database=economics,
        backend_access_log=_access_log(tmp_path),
        tenant_id=TENANT,
        captured_at="2026-08-26T12:00:00Z",
    )

    assert set(snapshot) == {"schema_version", "tenant_id", "captured_at", "planes"}
    assert set(snapshot["planes"]) == {
        "measured_endpoint",
        "provider",
        "runs",
        "audits",
        "economics",
    }
    assert snapshot["planes"]["measured_endpoint"]["request_count"] == 1
    assert snapshot["planes"]["runs"]["count"] == 2
    assert snapshot["planes"]["audits"]["count"] == 1
    assert snapshot["planes"]["provider"]["call_count"] == 2
    assert snapshot["planes"]["economics"] == {
        "cost_event_count": 2,
        "total_cost_usd": "0.01000000",
        "reservation_count": 2,
        "held_cost_usd": "0.03000000",
    }
    for plane, field in (
        ("measured_endpoint", "last_request_id_sha256"),
        ("provider", "request_ids_sha256"),
        ("runs", "ids_sha256"),
        ("audits", "head_digest"),
    ):
        assert len(snapshot["planes"][plane][field]) == 64
    serialized = json.dumps(snapshot, sort_keys=True)
    for secret_identity in (
        "run-a",
        "run-z",
        "audit-a",
        "provider-a",
        "provider-reservation-only",
        "operation-a",
        "/v1/econ/rightsizing/experiment",
        "127.0.0.1:50001",
    ):
        assert secret_identity not in serialized


def test_collection_is_stable_and_does_not_modify_read_only_databases(tmp_path: Path) -> None:
    service, economics = _databases(tmp_path)
    access = _access_log(tmp_path)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (service, economics)}
    service.chmod(0o444)
    economics.chmod(0o444)

    first = collect_snapshot(
        service_database=service,
        economics_database=economics,
        backend_access_log=access,
        tenant_id=TENANT,
        captured_at="2026-08-26T12:00:00Z",
    )
    second = collect_snapshot(
        service_database=service,
        economics_database=economics,
        backend_access_log=access,
        tenant_id=TENANT,
        captured_at="2026-08-26T12:00:05Z",
    )

    assert first["planes"] == second["planes"]
    assert before == {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (service, economics)
    }


@pytest.mark.parametrize("unsafe", ("Authorization: Bearer abc", "api_key=sk-test-secret"))
def test_fails_closed_on_unsanitized_access_logs(tmp_path: Path, unsafe: str) -> None:
    service, economics = _databases(tmp_path)
    access = tmp_path / "backend-access.log"
    access.write_text(unsafe + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsanitized backend access log"):
        collect_snapshot(
            service_database=service,
            economics_database=economics,
            backend_access_log=access,
            tenant_id=TENANT,
        )


def test_fails_closed_on_missing_schema_and_nonzero_service_wal(tmp_path: Path) -> None:
    service, economics = _databases(tmp_path)
    with sqlite3.connect(economics) as database:
        database.execute("DROP TABLE execution_events")
    with pytest.raises(RuntimeError, match="execution_events"):
        collect_snapshot(
            service_database=service,
            economics_database=economics,
            backend_access_log=_access_log(tmp_path),
            tenant_id=TENANT,
        )

    service, economics = _databases(tmp_path / "wal")
    service.with_name(service.name + "-wal").write_bytes(b"not-empty")
    with pytest.raises(RuntimeError, match="WAL"):
        collect_snapshot(
            service_database=service,
            economics_database=economics,
            backend_access_log=_access_log(tmp_path / "wal"),
            tenant_id=TENANT,
        )


def test_atomic_json_refuses_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "runtime" / "before.json"
    write_json_atomic(destination, {"safe": True})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"safe": True}

    with pytest.raises(FileExistsError):
        write_json_atomic(destination, {"safe": False})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"safe": True}
    assert list(destination.parent.iterdir()) == [destination]


def test_health_fetch_is_exact_loopback_get_and_writes_no_secrets(tmp_path: Path) -> None:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.path)
            payload = json.dumps({"status": "ok", "campaign_id": TENANT}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        health = fetch_loopback_health(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert health == {"status": "ok", "campaign_id": TENANT}
    assert seen == ["/health"]
    for invalid in (
        "https://127.0.0.1:8122",
        "http://localhost:8122",
        "http://127.0.0.1:8122/base",
        "http://example.com:8122",
        "http://user:secret@127.0.0.1:8122",
    ):
        with pytest.raises(ValueError, match="loopback"):
            fetch_loopback_health(invalid)
