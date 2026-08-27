"""Controlled local OpenAI-compatible authentication-rejection sink."""

from __future__ import annotations

import argparse
import json
import os
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOOPBACK_HOST = "127.0.0.1"
_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
_HEALTH_PATH = "/health"
_EVIDENCE_KEYS = frozenset({"request_count", "path", "method", "authorization_present"})


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


AUTHENTICATION_ERROR_BODY = _json_bytes(
    {
        "error": {
            "message": "Authentication rejected by controlled local evaluation sink.",
            "type": "authentication_error",
            "param": None,
            "code": "invalid_api_key",
        }
    }
)
_NOT_FOUND_BODY = _json_bytes(
    {
        "error": {
            "message": "Not found.",
            "type": "invalid_request_error",
            "param": None,
            "code": "not_found",
        }
    }
)


class EvidenceLog:
    """Bounded JSONL writer for non-sensitive request metadata."""

    def __init__(self, path: Path, *, max_records: int) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.path = path
        self.max_records = max_records
        self._lock = threading.Lock()
        self._recorded_count = self._validate_existing()
        self._request_count = self._recorded_count
        self._write_failed = False

    def _validate_existing(self) -> int:
        if not self.path.exists():
            return 0
        if not self.path.is_file():
            raise ValueError(f"evidence path is not a regular file: {self.path}")

        count = 0
        with self.path.open(encoding="utf-8") as evidence_file:
            for count, line in enumerate(evidence_file, start=1):
                if count > self.max_records:
                    raise ValueError("existing evidence exceeds max_records")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid evidence JSON on line {count}") from exc
                if not self._valid_record(record, expected_count=count):
                    raise ValueError(f"unsafe evidence record on line {count}")
        return count

    @staticmethod
    def _valid_record(record: object, *, expected_count: int) -> bool:
        if not isinstance(record, dict) or set(record) != _EVIDENCE_KEYS:
            return False
        request_count = record["request_count"]
        return (
            isinstance(request_count, int)
            and not isinstance(request_count, bool)
            and request_count == expected_count
            and record["path"] == _CHAT_COMPLETIONS_PATH
            and record["method"] == "POST"
            and isinstance(record["authorization_present"], bool)
        )

    def record_authentication_rejection(self, *, authorization_present: bool) -> None:
        """Count a request and append its safe projection if capacity remains."""
        with self._lock:
            self._request_count += 1
            if self._recorded_count >= self.max_records or self._write_failed:
                return
            record = {
                "request_count": self._request_count,
                "path": _CHAT_COMPLETIONS_PATH,
                "method": "POST",
                "authorization_present": authorization_present,
            }
            encoded = _json_bytes(record) + b"\n"
            try:
                self._append(encoded)
            except OSError:
                self._write_failed = True
                return
            self._recorded_count += 1

    def _append(self, encoded: bytes) -> None:
        """Append one already-sanitized record without following a final symlink."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("zero-length evidence write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def health(self) -> dict[str, int | bool | str]:
        """Return a safe in-memory snapshot without writing evidence."""
        with self._lock:
            return {
                "status": "degraded" if self._write_failed else "ok",
                "request_count": self._request_count,
                "recorded_count": self._recorded_count,
                "max_records": self.max_records,
                "at_capacity": self._recorded_count >= self.max_records,
                "evidence_write_failed": self._write_failed,
            }


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _handler_for(evidence: EvidenceLog) -> type[BaseHTTPRequestHandler]:
    class AuthenticationRejectionHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != _CHAT_COMPLETIONS_PATH:
                self._send_json(404, _NOT_FOUND_BODY)
                return
            evidence.record_authentication_rejection(
                authorization_present="Authorization" in self.headers
            )
            self._send_json(401, AUTHENTICATION_ERROR_BODY)

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != _HEALTH_PATH:
                self._send_json(404, _NOT_FOUND_BODY)
                return
            self._send_json(200, _json_bytes(evidence.health()))

        def _send_json(self, status: int, body: bytes) -> None:
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return AuthenticationRejectionHandler


def create_server(
    *, evidence_path: Path, max_records: int = 10_000, port: int = 8001
) -> ThreadingHTTPServer:
    """Create a server bound to IPv4 loopback; no configurable bind host exists."""
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    evidence = EvidenceLog(evidence_path, max_records=max_records)
    return _LoopbackHTTPServer((_LOOPBACK_HOST, port), _handler_for(evidence))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zeroth-auth-rejection-sink",
        description="Run a loopback-only OpenAI-compatible deterministic 401 sink.",
    )
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--evidence-file",
        type=Path,
        default=Path("auth-rejection-evidence.jsonl"),
    )
    parser.add_argument("--max-records", type=_positive_int, default=10_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = create_server(
        evidence_path=args.evidence_file,
        max_records=args.max_records,
        port=args.port,
    )
    host, port = server.server_address[:2]
    print(f"authentication-rejection sink listening at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
