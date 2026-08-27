"""Append-only, secret-rejecting evidence storage for live evaluation campaigns."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

AcceptanceStatus = Literal["pass", "fail", "blocked", "not_run"]

_FORBIDDEN_FIELD = re.compile(
    r"(?:^|_)(?:authorization|api_?key|provider_?key|service_?key|secret)(?:$|_)"
    r"|(?:^|_)(?:access_?token|refresh_?token|auth_?token|bearer_?token|session_?token|token)$",
    re.IGNORECASE,
)
_SAFE_AUTHORIZATION_ID_FIELD = re.compile(
    r"^authorization(?:_[a-z0-9]+)+_id$",
    re.IGNORECASE,
)
_SAFE_AUTHORIZATION_BOOLEAN_FIELDS = {
    "authorization_present",
    "authorization_value_retained",
}
_FORBIDDEN_TEXT = (
    # Header syntax must start a text line. Audit identifiers legitimately use
    # the namespace ``service.authorization:<event>`` and are not credentials.
    re.compile(r"(?:^|[\r\n])\s*Authorization\s*:\s*\S+", re.IGNORECASE),
    re.compile(r"\b(?:api|provider|service)[_-]?key\s*[=:]", re.IGNORECASE),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
)
_TEXT_ARTIFACT_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".log",
    ".md",
    ".ndjson",
    ".txt",
    ".xml",
}
_BINARY_ARTIFACT_SIGNATURES = {
    ".jpeg": (b"\xff\xd8\xff",),
    ".jpg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".webm": (b"\x1aE\xdf\xa3",),
}
_ARTIFACT_TOP_LEVEL = {
    "accessibility",
    "console",
    "network",
    "playwright-report",
    "reconciliation",
    "screenshots",
    "videos",
    "handoff",
}
_SAFE_COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CORRELATION_FIELDS = (
    "operation_id",
    "run_id",
    "audit_event_id",
    "cost_event_id",
    "provider_request_id",
    "ui_action_id",
)
_FINAL_FILE_NAMES = {"acceptance.json", "report.md", "SHA256SUMS"}


class UnsafeEvidenceError(ValueError):
    """Raised before content that could contain a credential reaches disk."""


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One acceptance gate and the durable records supporting its disposition."""

    criterion_id: str
    status: AcceptanceStatus
    evidence: tuple[str, ...] = ()
    note: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "blocked", "not_run"}:
            raise ValueError(f"invalid acceptance status: {self.status}")


@dataclass(frozen=True, slots=True)
class CorrelationIds:
    """Runtime-validated identifiers used to join campaign evidence domains."""

    operation_id: str | None = None
    run_id: str | None = None
    audit_event_id: str | None = None
    cost_event_id: str | None = None
    provider_request_id: str | None = None
    ui_action_id: str | None = None

    def __post_init__(self) -> None:
        populated = {
            field: value
            for field in _CORRELATION_FIELDS
            if (value := getattr(self, field)) is not None
        }
        if not populated:
            raise ValueError("typed correlation requires at least one identifier")
        for field, value in populated.items():
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 512
                or any(character.isspace() or ord(character) < 32 for character in value)
            ):
                raise ValueError(f"invalid typed correlation identifier: {field}")
        if len(set(populated.values())) != len(populated):
            raise ValueError("typed correlation identifiers must be distinct by namespace")

    def as_dict(self) -> dict[str, str]:
        return {
            field: value
            for field in _CORRELATION_FIELDS
            if (value := getattr(self, field)) is not None
        }


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _assert_safe(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            safe_authorization_boolean = (
                key_text.lower() in _SAFE_AUTHORIZATION_BOOLEAN_FIELDS
                and isinstance(child, bool)
                and (key_text.lower() != "authorization_value_retained" or child is False)
            )
            if (
                _FORBIDDEN_FIELD.search(key_text)
                and not _SAFE_AUTHORIZATION_ID_FIELD.fullmatch(key_text)
                and not safe_authorization_boolean
            ):
                raise UnsafeEvidenceError(f"forbidden evidence field at {path}.{key_text}")
            _assert_safe(child, path=f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _FORBIDDEN_TEXT:
            if pattern.search(value):
                raise UnsafeEvidenceError(f"secret-shaped evidence value at {path}")


def _find_correlation_fields(value: object) -> set[str]:
    if isinstance(value, Mapping):
        found = {str(key) for key in value if str(key) in _CORRELATION_FIELDS}
        for child in value.values():
            found.update(_find_correlation_fields(child))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for child in value:
            found.update(_find_correlation_fields(child))
        return found
    return set()


def _assert_safe_bytes(payload: bytes, *, path: str) -> None:
    """Scan text fully and binary files for embedded ASCII credential material."""
    text = payload.decode("utf-8", errors="ignore")
    _assert_safe(text, path=path)


def _assert_safe_file_payload(payload: bytes, *, relative_path: Path) -> None:
    _assert_safe_bytes(payload, path=relative_path.as_posix())
    if relative_path.suffix.lower() == ".json":
        try:
            _assert_safe(json.loads(payload))
        except json.JSONDecodeError as exc:
            raise UnsafeEvidenceError(f"invalid JSON evidence: {relative_path.as_posix()}") from exc
    elif relative_path.suffix.lower() == ".ndjson":
        for line_number, line in enumerate(payload.splitlines(), start=1):
            try:
                _assert_safe(json.loads(line))
            except json.JSONDecodeError as exc:
                raise UnsafeEvidenceError(
                    f"invalid NDJSON evidence at {relative_path.as_posix()}:{line_number}"
                ) from exc


def _assert_safe_sqlite_file(path: Path, *, relative_path: Path) -> None:
    """Inspect SQLite cell values so structured secrets cannot hide in binary pages."""
    try:
        # Evidence snapshots are immutable by contract.  Opening a WAL-mode
        # snapshot with only ``mode=ro`` still creates ``-wal``/``-shm`` files,
        # which makes a recursive safety scan mutate the bundle it is proving.
        # SQLite's immutable URI flag suppresses recovery and sidecar creation;
        # the online-backup producer has already delivered a complete snapshot.
        connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            tables = connection.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
            for (table_name,) in tables:
                quoted_table = '"' + str(table_name).replace('"', '""') + '"'
                cursor = connection.execute(f"select * from {quoted_table}")
                columns = [item[0] for item in cursor.description or ()]
                for column in columns:
                    column_text = str(column)
                    if _FORBIDDEN_FIELD.search(
                        column_text
                    ) and not _SAFE_AUTHORIZATION_ID_FIELD.fullmatch(column_text):
                        raise UnsafeEvidenceError(
                            "forbidden evidence field at "
                            f"{relative_path.as_posix()}:{table_name}.{column}"
                        )
                for row_index, row in enumerate(cursor):
                    for column, value in zip(columns, row, strict=True):
                        cell_path = f"{relative_path.as_posix()}:{table_name}.{column}[{row_index}]"
                        if isinstance(value, str):
                            _assert_safe(value, path=cell_path)
                            if value.lstrip().startswith(("{", "[")):
                                try:
                                    decoded = json.loads(value)
                                except json.JSONDecodeError:
                                    continue
                                _assert_safe(decoded, path=cell_path)
                        elif isinstance(value, bytes):
                            _assert_safe_bytes(value, path=cell_path)
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        # Snapshot integrity is validated by the control/campaign gate. The
        # evidence scanner owns credential detection and must not replace that
        # gate's stable, operator-facing corruption error.
        return


def _validate_artifact_payload(payload: bytes, *, relative_path: Path) -> None:
    suffix = relative_path.suffix.lower()
    if suffix in _TEXT_ARTIFACT_SUFFIXES:
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("text artifact must be UTF-8") from exc
    elif signatures := _BINARY_ARTIFACT_SIGNATURES.get(suffix):
        if not any(payload.startswith(signature) for signature in signatures):
            raise ValueError("artifact does not match its declared binary type")
    else:
        raise ValueError(f"unsupported artifact type: {suffix or '<none>'}")


class EvidenceStore:
    """Write immutable records and append-only events below one campaign root."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def is_sealed(self) -> bool:
        return (self.root / "SHA256SUMS").is_file()

    def _assert_unsealed(self) -> None:
        if self.is_sealed:
            raise RuntimeError("evidence bundle is sealed")

    @contextmanager
    def _bundle_lock(self):
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def validate(self, value: object) -> None:
        """Preflight content with the same rejection rules used by every writer."""
        _assert_safe(value)

    def _atomic_bytes_exclusive(self, relative_path: Path, payload: bytes) -> Path:
        self._assert_unsealed()
        destination = self.root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _write_exclusive(self, relative_path: Path, value: object) -> Path:
        _assert_safe(value)
        with self._bundle_lock():
            return self._atomic_bytes_exclusive(relative_path, _json_bytes(value))

    def write_manifest(self, manifest: Mapping[str, object]) -> Path:
        return self._write_exclusive(Path("manifest.json"), dict(manifest))

    def write_acceptance(self, criteria: Sequence[AcceptanceCriterion]) -> Path:
        payload = {
            "criteria": [asdict(criterion) for criterion in criteria],
            "schema_version": 1,
        }
        return self._write_exclusive(Path("acceptance.json"), payload)

    def write_report(self, markdown: str) -> Path:
        _assert_safe(markdown)
        with self._bundle_lock():
            return self._atomic_bytes_exclusive(Path("report.md"), markdown.encode())

    def snapshot_sqlite(self, source: Path, *, name: str) -> Path:
        if Path(name).name != name or not name.endswith((".sqlite", ".sqlite3", ".db")):
            raise ValueError("snapshot name must be a safe SQLite filename")
        if not source.is_file():
            raise FileNotFoundError(source)
        with self._bundle_lock():
            self._assert_unsealed()
            destination = self.root / "database-snapshots" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
            if destination.exists():
                raise FileExistsError(destination)
            try:
                with sqlite3.connect(source) as live, sqlite3.connect(temporary) as snapshot:
                    live.backup(snapshot)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.link(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return destination

    def append_event(
        self,
        event_type: str,
        data: Mapping[str, object],
        *,
        event_id: str | None = None,
        timestamp: str | None = None,
        correlation: CorrelationIds | None = None,
    ) -> str:
        reserved = _find_correlation_fields(data) | ({"correlation"} & set(data))
        if reserved:
            raise ValueError("correlation identifiers require typed correlation input")
        self._require_event_correlation(event_type, correlation)
        record = {
            "data": dict(data),
            "event_id": event_id or str(uuid.uuid4()),
            "timestamp": timestamp or datetime.now(UTC).isoformat(),
            "type": event_type,
        }
        if correlation is not None:
            record["correlation"] = correlation.as_dict()
        _assert_safe(record)
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self._bundle_lock():
            self._assert_unsealed()
            existing = self.read_events()
            if any(row.get("event_id") == record["event_id"] for row in existing):
                raise ValueError(f"duplicate evidence event_id: {record['event_id']}")
            if correlation is not None:
                self._validate_correlation_consistency(correlation, existing)
            destination = self.root / "events.ndjson"
            descriptor = os.open(destination, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                written = 0
                while written < len(encoded):
                    written += os.write(descriptor, encoded[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return str(record["event_id"])

    @staticmethod
    def _require_event_correlation(event_type: str, correlation: CorrelationIds | None) -> None:
        tokens = set(event_type.lower().split("."))
        required = {
            "operation": "operation_id",
            "run": "run_id",
            "audit": "audit_event_id",
            "cost": "cost_event_id",
            "economics": "cost_event_id",
            "provider": "provider_request_id",
            "ui": "ui_action_id",
        }
        for token, field in required.items():
            if token in tokens and (correlation is None or getattr(correlation, field) is None):
                raise ValueError(f"{event_type} requires typed {field}")

    @staticmethod
    def _validate_correlation_consistency(
        correlation: CorrelationIds,
        existing: Sequence[Mapping[str, object]],
    ) -> None:
        current = correlation.as_dict()
        for event in existing:
            prior_value = event.get("correlation")
            if not isinstance(prior_value, Mapping):
                continue
            prior = {
                str(field): str(value)
                for field, value in prior_value.items()
                if field in _CORRELATION_FIELDS
            }
            for current_field, current_id in current.items():
                for prior_field, prior_id in prior.items():
                    if current_id == prior_id and current_field != prior_field:
                        raise ValueError(
                            "correlation identifier aliases a different identity namespace"
                        )
                if prior.get(current_field) != current_id:
                    continue
                # Cardinality is directional: one provider operation belongs to
                # at most one run, while a run legitimately contains many
                # operations (for example, retrieval embedding plus chat).
                scope_fields = (
                    ()
                    if current_field == "run_id"
                    else ("run_id",)
                    if current_field == "operation_id"
                    else ("operation_id", "run_id")
                )
                for scope_field in scope_fields:
                    before = prior.get(scope_field)
                    after = current.get(scope_field)
                    if before is not None and after is not None and before != after:
                        raise ValueError(f"correlation has conflicting {scope_field}: {current_id}")

    def read_events(self) -> tuple[dict[str, object], ...]:
        source = self.root / "events.ndjson"
        if not source.exists():
            return ()
        records: list[dict[str, object]] = []
        for line_number, line in enumerate(source.read_text().splitlines(), start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid evidence event at line {line_number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"invalid evidence event at line {line_number}")
            records.append(record)
        return tuple(records)

    def ingest_artifact(self, source: Path, relative_path: str | Path) -> Path:
        """Copy one policy-approved artifact into the append-only bundle."""
        if source.is_symlink():
            raise ValueError("artifact source must be a regular file")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("artifact source must be a regular file")
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) < 2
            or relative.parts[0] not in _ARTIFACT_TOP_LEVEL
        ):
            raise ValueError("invalid artifact destination")
        payload = source.read_bytes()
        _validate_artifact_payload(payload, relative_path=relative)
        _assert_safe_file_payload(payload, relative_path=relative)
        with self._bundle_lock():
            return self._atomic_bytes_exclusive(relative, payload)

    def validate_evidence_references(self, criteria: Sequence[AcceptanceCriterion]) -> None:
        event_ids = {
            str(record.get("event_id"))
            for record in self.read_events()
            if record.get("event_id") is not None
        }
        for criterion in criteria:
            for reference in criterion.evidence:
                path_text, separator, fragment = reference.partition("#")
                relative = Path(path_text)
                if len(relative.parts) == 1 and relative.name in _FINAL_FILE_NAMES:
                    raise ValueError(f"final-file evidence reference is circular: {reference!r}")
                if relative.is_absolute() or ".." in relative.parts or not path_text:
                    raise ValueError(f"invalid evidence reference: {reference!r}")
                target = self.root / relative
                if not target.is_file():
                    raise ValueError(f"missing evidence reference: {reference!r}")
                if separator and path_text == "events.ndjson" and fragment not in event_ids:
                    raise ValueError(f"unknown event evidence reference: {reference!r}")

    def scan_recursive(self) -> None:
        """Fail closed on symlinks or credential material introduced out of band."""
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink():
                raise UnsafeEvidenceError(f"symlink is not allowed in evidence: {path}")
            if path.is_file() and path.name != "SHA256SUMS":
                relative_path = path.relative_to(self.root)
                if relative_path.parts[0] in _ARTIFACT_TOP_LEVEL:
                    try:
                        _validate_artifact_payload(path.read_bytes(), relative_path=relative_path)
                    except ValueError as exc:
                        raise UnsafeEvidenceError(
                            f"unsupported artifact type at {relative_path.as_posix()}"
                        ) from exc
                _assert_safe_file_payload(path.read_bytes(), relative_path=relative_path)
                if relative_path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                    _assert_safe_sqlite_file(path, relative_path=relative_path)

    def record_command(
        self,
        *,
        sequence: int,
        name: str,
        argv: Sequence[str],
        working_directory: Path,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> Path:
        if not _SAFE_COMMAND_NAME.fullmatch(name):
            raise ValueError("command evidence name must be a safe slug")
        relative_path = Path("commands") / f"{sequence:04d}-{name}.json"
        record = {
            "argv": list(argv),
            "exit_code": exit_code,
            "name": name,
            "stderr": stderr,
            "stdout": stdout,
            "working_directory": str(working_directory),
        }
        destination = self._write_exclusive(relative_path, record)
        self.append_event(
            "command.completed",
            {
                "evidence_path": relative_path.as_posix(),
                "exit_code": exit_code,
                "name": name,
            },
        )
        return destination

    def _write_checksums_locked(self) -> Path:
        destination = self.root / "SHA256SUMS"
        if destination.exists():
            raise FileExistsError(destination)
        entries: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path == destination:
                continue
            relative_path = path.relative_to(self.root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(f"{digest}  {relative_path}")
        payload = ("\n".join(entries) + ("\n" if entries else "")).encode()
        return self._atomic_bytes_exclusive(Path("SHA256SUMS"), payload)

    def write_checksums(self) -> Path:
        """Scan and irreversibly seal the current bundle."""
        with self._bundle_lock():
            self._assert_unsealed()
            self.scan_recursive()
            return self._write_checksums_locked()

    def finalize_bundle(
        self,
        *,
        acceptance: Sequence[AcceptanceCriterion],
        report_markdown: str,
    ) -> None:
        """Validate and atomically publish final records before sealing the bundle."""
        payload = {
            "criteria": [asdict(criterion) for criterion in acceptance],
            "schema_version": 1,
        }
        _assert_safe(payload)
        _assert_safe(report_markdown)
        self.validate_evidence_references(acceptance)
        self.scan_recursive()
        with self._bundle_lock():
            self._assert_unsealed()
            final_payloads = (
                (Path("acceptance.json"), _json_bytes(payload)),
                (Path("report.md"), report_markdown.encode()),
            )
            created: list[Path] = []
            try:
                for relative_path, expected in final_payloads:
                    destination = self.root / relative_path
                    if destination.exists():
                        if destination.read_bytes() != expected:
                            raise FileExistsError(destination)
                        continue
                    self._atomic_bytes_exclusive(relative_path, expected)
                    created.append(destination)
            except Exception:
                for destination in created:
                    destination.unlink(missing_ok=True)
                raise
            self.scan_recursive()
            self._write_checksums_locked()
