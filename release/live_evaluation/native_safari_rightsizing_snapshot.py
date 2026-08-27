"""Produce read-only state snapshots for the native Safari Rightsizing boundary gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

_EXPERIMENT_PATH = "/v1/econ/rightsizing/experiment"
_ACCESS_LINE = re.compile(
    r'^INFO:\s+\S+\s+-\s+"(?P<method>[A-Z]+) (?P<path>\S+) '
    r'HTTP/(?:1\.0|1\.1|2)" (?P<status>[1-5][0-9]{2})(?:\s+.*)?$'
)
_UNSAFE_LOG = re.compile(
    r"authorization|bearer\s+|api[_-]?key|service[_-]?key|webhook[_-]?secret|\bsk-[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_UNSAFE_KEY = re.compile(
    r"authorization|api[_-]?key|service[_-]?key|secret|password|credential|token",
    re.IGNORECASE,
)
_UNSAFE_VALUE = re.compile(r"bearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}", re.IGNORECASE)
_REQUIRED_SCHEMA: dict[str, dict[str, set[str]]] = {
    "service": {
        "runs": {"run_id", "tenant_id"},
        "node_audits": {
            "audit_id",
            "run_id",
            "node_id",
            "graph_version_ref",
            "deployment_ref",
            "created_at",
            "record_json",
            "cost_usd",
            "cost_event_id",
            "chain_sequence",
            "tenant_id",
        },
    },
    "economics": {
        "execution_events": {
            "tenant_id",
            "execution_id",
            "provider_request_id",
            "token_cost_usd",
            "tool_cost_usd",
            "compute_cost_usd",
        },
        "cost_reservations": {
            "tenant_id",
            "operation_id",
            "provider_request_id",
            "held_cost_usd",
            "actual_cost_usd",
        },
    },
}


def _validate_tenant(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("tenant_id must be a nonblank explicit value")
    if len(value) > 128 or any(character in value for character in "\r\n\x00"):
        raise ValueError("tenant_id is invalid")
    return value


def _validate_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("captured_at must be an explicit timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("captured_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    return value


def _assert_safe_json(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or _UNSAFE_KEY.search(key):
                raise RuntimeError(f"unsafe JSON key at {path}")
            _assert_safe_json(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_json(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _UNSAFE_VALUE.search(value):
        raise RuntimeError(f"unsafe JSON value at {path}")


def _database_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser()
    if resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError(f"{label} database is missing or not a regular file")
    wal = resolved.with_name(f"{resolved.name}-wal")
    if wal.exists() and (wal.is_symlink() or not wal.is_file() or wal.stat().st_size > 0):
        raise RuntimeError(f"{label} database has a non-empty WAL; snapshot would be stale")
    return resolved.resolve(strict=True)


@contextmanager
def _readonly_database(path: Path, *, label: str) -> Iterator[sqlite3.Connection]:
    resolved = _database_path(path, label=label)
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro&immutable=1"
    try:
        database = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"unable to open {label} database read-only") from exc
    try:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only = ON")
        integrity = database.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(f"{label} database integrity check failed")
        yield database
    except sqlite3.Error as exc:
        raise RuntimeError(f"unable to read {label} database") from exc
    finally:
        database.close()


def _require_schema(database: sqlite3.Connection, *, plane: str) -> None:
    for table, required_columns in _REQUIRED_SCHEMA[plane].items():
        columns = {str(row[1]) for row in database.execute(f'PRAGMA table_info("{table}")')}
        if not required_columns.issubset(columns):
            missing = ", ".join(sorted(required_columns - columns)) or "table"
            raise RuntimeError(f"{table} schema is missing required columns: {missing}")


def _digest(rows: Iterable[Sequence[object]]) -> str:
    normalized = [list(row) for row in rows]
    payload = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _money(value: object, *, label: str) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, bool):
        raise RuntimeError(f"invalid {label}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not result.is_finite() or result < 0:
        raise RuntimeError(f"invalid {label}")
    return result


def _service_planes(database: sqlite3.Connection, tenant_id: str) -> dict[str, object]:
    _require_schema(database, plane="service")
    run_ids = [
        (str(row["run_id"]),)
        for row in database.execute(
            "SELECT run_id FROM runs WHERE tenant_id = ? ORDER BY run_id", (tenant_id,)
        )
    ]
    audit_rows = [
        tuple(row)
        for row in database.execute(
            """SELECT audit_id, run_id, node_id, graph_version_ref, deployment_ref,
                      created_at, record_json, cost_usd, cost_event_id, chain_sequence
               FROM node_audits WHERE tenant_id = ? ORDER BY audit_id""",
            (tenant_id,),
        )
    ]
    return {
        "runs": {"count": len(run_ids), "ids_sha256": _digest(run_ids)},
        "audits": {"count": len(audit_rows), "head_digest": _digest(audit_rows)},
    }


def _economics_planes(database: sqlite3.Connection, tenant_id: str) -> dict[str, object]:
    _require_schema(database, plane="economics")
    event_rows = list(
        database.execute(
            """SELECT execution_id, provider_request_id, token_cost_usd,
                      tool_cost_usd, compute_cost_usd
               FROM execution_events WHERE tenant_id = ? ORDER BY execution_id""",
            (tenant_id,),
        )
    )
    reservation_rows = list(
        database.execute(
            """SELECT operation_id, provider_request_id, held_cost_usd, actual_cost_usd
               FROM cost_reservations WHERE tenant_id = ? ORDER BY operation_id""",
            (tenant_id,),
        )
    )
    provider_ids = sorted(
        {
            str(value).strip()
            for value in (
                *(row["provider_request_id"] for row in event_rows),
                *(row["provider_request_id"] for row in reservation_rows),
            )
            if value is not None and str(value).strip()
        }
    )
    total_cost = sum(
        (
            _money(row["token_cost_usd"], label="token cost")
            + _money(row["tool_cost_usd"], label="tool cost")
            + _money(row["compute_cost_usd"], label="compute cost")
            for row in event_rows
        ),
        Decimal(0),
    )
    held_cost = sum(
        (_money(row["held_cost_usd"], label="held cost") for row in reservation_rows),
        Decimal(0),
    )
    return {
        "provider": {
            "call_count": len(provider_ids),
            "request_ids_sha256": _digest((provider_id,) for provider_id in provider_ids),
        },
        "economics": {
            "cost_event_count": len(event_rows),
            "total_cost_usd": format(total_cost, ".8f"),
            "reservation_count": len(reservation_rows),
            "held_cost_usd": format(held_cost, ".8f"),
        },
    }


def _access_plane_lines(lines: Iterable[str]) -> dict[str, object]:
    count = 0
    last_fingerprint = hashlib.sha256(b"[]").hexdigest()
    for raw_line in lines:
        if len(raw_line) > 8192:
            raise RuntimeError("backend access log contains an oversized line")
        if _UNSAFE_LOG.search(raw_line):
            raise RuntimeError("unsanitized backend access log contains secret material")
        match = _ACCESS_LINE.fullmatch(raw_line.rstrip("\r\n"))
        if match is None:
            continue
        if match["method"] != "POST" or match["path"] != _EXPERIMENT_PATH:
            continue
        count += 1
        sanitized = f"{count}|POST|rightsizing-experiment|{match['status']}"
        last_fingerprint = hashlib.sha256(sanitized.encode("ascii")).hexdigest()
    return {
        "request_count": count,
        "last_request_id_sha256": last_fingerprint,
    }


def _access_plane(path: Path) -> dict[str, object]:
    resolved = path.expanduser()
    if resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError("backend access log is missing or not a regular file")
    with resolved.open("r", encoding="utf-8", errors="strict") as handle:
        return _access_plane_lines(handle)


def read_docker_access_log(
    container: str,
    *,
    runner: Callable[..., object] = subprocess.run,
) -> str:
    """Read the exact campaign backend log without retaining raw log evidence."""
    if container != "zeroth-dev-backend-1":
        raise ValueError("container must be the exact campaign backend")
    try:
        result = runner(
            ["docker", "logs", container],
            capture_output=True,
            check=False,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("docker logs failed") from exc
    if getattr(result, "returncode", None) != 0:
        raise RuntimeError("docker logs failed")
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise RuntimeError("docker logs returned invalid output")
    return stdout + stderr


def collect_snapshot(
    *,
    service_database: Path,
    economics_database: Path,
    backend_access_log: Path | None = None,
    backend_access_text: str | None = None,
    tenant_id: str,
    captured_at: str | None = None,
) -> dict[str, object]:
    """Return the exact five-plane contract consumed by the Safari checkpoint sealer."""
    tenant = _validate_tenant(tenant_id)
    if (backend_access_log is None) == (backend_access_text is None):
        raise ValueError("supply exactly one backend access source")
    with _readonly_database(service_database, label="service") as service:
        service_planes = _service_planes(service, tenant)
    with _readonly_database(economics_database, label="economics") as economics:
        economics_planes = _economics_planes(economics, tenant)
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "tenant_id": tenant,
        "captured_at": _validate_timestamp(captured_at),
        "planes": {
            "measured_endpoint": (
                _access_plane(backend_access_log)
                if backend_access_log is not None
                else _access_plane_lines(backend_access_text.splitlines(keepends=True))
            ),
            "provider": economics_planes["provider"],
            "runs": service_planes["runs"],
            "audits": service_planes["audits"],
            "economics": economics_planes["economics"],
        },
    }
    _assert_safe_json(snapshot)
    return snapshot


def write_json_atomic(destination: Path, value: Mapping[str, object]) -> Path:
    """Atomically create a canonical JSON file without replacing existing evidence."""
    _assert_safe_json(value)
    destination = destination.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    except FileExistsError:
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def fetch_loopback_health(base_url: str, *, timeout_seconds: float = 3.0) -> dict[str, object]:
    """Fetch exactly ``GET /health`` from an explicit IPv4 loopback service."""
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("health base URL must be exact loopback HTTP") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("health base URL must be exact loopback HTTP")
    request = Request(
        f"http://127.0.0.1:{port}/health",
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            if response.status != 200:
                raise RuntimeError("loopback health returned a non-200 response")
            payload = response.read(1_000_001)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("loopback health request failed") from exc
    if len(payload) > 1_000_000:
        raise RuntimeError("loopback health response is too large")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("loopback health response is not JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("loopback health response must be an object")
    _assert_safe_json(value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-db", required=True, type=Path)
    parser.add_argument("--econ-db", required=True, type=Path)
    access_source = parser.add_mutually_exclusive_group(required=True)
    access_source.add_argument("--access-log", type=Path)
    access_source.add_argument("--docker-container")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--health-base-url")
    parser.add_argument("--health-output", type=Path)
    args = parser.parse_args()
    if (args.health_base_url is None) != (args.health_output is None):
        parser.error("--health-base-url and --health-output must be supplied together")
    snapshot = collect_snapshot(
        service_database=args.service_db,
        economics_database=args.econ_db,
        backend_access_log=args.access_log,
        backend_access_text=(
            read_docker_access_log(args.docker_container)
            if args.docker_container is not None
            else None
        ),
        tenant_id=args.tenant_id,
    )
    write_json_atomic(args.output, snapshot)
    result: dict[str, object] = {"snapshot": str(args.output), "sealed_input": True}
    if args.health_base_url is not None:
        assert args.health_output is not None
        write_json_atomic(args.health_output, fetch_loopback_health(args.health_base_url))
        result["health"] = str(args.health_output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
