"""Run one HTTP exchange behind a killable certifier-owned boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

_RESPONSE_LIMIT = 1 << 20
_READ_CHUNK = 64 * 1024


def _head_size(status: int, reason: str, headers: Any) -> int:
    start = f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1", errors="replace")
    fields = sum(
        len(f"{name}: {value}\r\n".encode("latin-1", errors="replace"))
        for name, value in headers.items()
    )
    return len(start) + fields + 2


class _BoundedRedirects(HTTPRedirectHandler):
    def __init__(self) -> None:
        self.bytes_used = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.bytes_used += _head_size(code, msg, headers)
        if self.bytes_used > _RESPONSE_LIMIT:
            raise ValueError("HTTP response exceeded 1 MiB limit")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_json(response: Any, budget: int) -> Any:
    body = bytearray()
    while True:
        chunk = response.read(min(_READ_CHUNK, budget + 1 - len(body)))
        if not chunk:
            return json.loads(body)
        body.extend(chunk)
        if len(body) > budget:
            raise ValueError("HTTP response exceeded 1 MiB limit")


def _response_result(response: Any, redirects: _BoundedRedirects) -> dict[str, Any]:
    used = redirects.bytes_used + _head_size(
        response.status, response.reason or "", response.headers
    )
    if used > _RESPONSE_LIMIT:
        raise ValueError("HTTP response exceeded 1 MiB limit")
    return {
        "status_code": response.status,
        "json_body": _read_json(response, _RESPONSE_LIMIT - used),
    }


def _worker() -> int:
    request_data = json.load(sys.stdin)
    body = request_data["body"]
    encoded_body = None
    if body is not None:
        encoded_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    request = Request(
        request_data["url"],
        data=encoded_body,
        method=request_data["method"],
        headers=request_data["headers"],
    )
    redirects = _BoundedRedirects()
    opener = build_opener(redirects)
    try:
        with opener.open(request, timeout=request_data["timeout"]) as response:  # noqa: S310
            result = _response_result(response, redirects)
    except HTTPError as error:
        with error:
            result = _response_result(error, redirects)
    output = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if len(output.encode()) > _RESPONSE_LIMIT:
        raise ValueError("HTTP response exceeded 1 MiB limit")
    print(output)
    return 0


def run_http_exchange(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: Any,
    timeout: float,
) -> tuple[int, Any]:
    """Return one bounded JSON response or terminate the complete exchange."""
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    payload = json.dumps(
        {"body": body, "headers": headers, "method": method, "timeout": timeout, "url": url}
    )
    try:
        stdout, stderr = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise TimeoutError("HTTP exchange exceeded total deadline") from error
    if process.returncode:
        detail = stderr.strip() or stdout.strip() or "HTTP worker failed"
        raise ValueError(detail[-500:])
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError("HTTP worker returned malformed JSON") from error
    if not isinstance(result, dict) or set(result) != {"json_body", "status_code"}:
        raise ValueError("HTTP worker returned malformed result")
    status = result["status_code"]
    if not isinstance(status, int):
        raise ValueError("HTTP worker returned malformed status")
    return status, result["json_body"]


if __name__ == "__main__":
    raise SystemExit(_worker())
