"""Loopback git smart-HTTP server that CGI-executes ``git http-backend``.

ZER-37 test substrate. Serves the bare fixture repositories under a project
root over real HTTP on 127.0.0.1:<ephemeral>, so a genuine ``git fetch`` --
protocol v2 included -- exercises the same transport a checkout client uses
against github.com. Per-repo auth is an exact-match ``Authorization`` header
(the shape an installation token takes via ``http.<url>.extraheader``), every
request is logged as ``(path, auth_header)``, and ``fail_all_with(status)``
turns the server into a brick for mid-fetch failure tests.
"""

from __future__ import annotations

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

_BACKEND_TIMEOUT_SECONDS = 60


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args  # silence default stderr chatter

    def do_GET(self) -> None:  # noqa: N802 -- http.server API
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802 -- http.server API
        self._dispatch()

    @property
    def _owner(self) -> GitSmartHTTPServer:
        return self.server.owner  # type: ignore[attr-defined]

    def _dispatch(self) -> None:
        owner = self._owner
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        auth = self.headers.get("Authorization")
        owner.requests.append((path, auth))
        if owner.fail_status is not None:
            self._respond(owner.fail_status, [], b"injected failure\n")
            return
        repo_name = path.lstrip("/").split("/", 1)[0]
        expected = owner.expected_auth.get(repo_name)
        if expected is not None and auth != expected:
            self._respond(401, [("WWW-Authenticate", 'Basic realm="fake-git"')], b"auth required\n")
            return
        body = self._read_body()
        stdout = self._run_backend(path, parsed.query, body)
        if stdout is None:
            self._respond(500, [], b"git http-backend failed\n")
            return
        status, headers, payload = self._parse_cgi_output(stdout)
        self._respond(status, headers, payload)

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline().strip()
                size = int(size_line.split(b";", 1)[0], 16)
                if size == 0:
                    self.rfile.readline()  # trailing CRLF after last-chunk
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # CRLF after each chunk
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _run_backend(self, path: str, query: str, body: bytes) -> bytes | None:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_PROJECT_ROOT": str(self._owner.project_root),
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": self.command,
            "REMOTE_ADDR": self.client_address[0],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        content_type = self.headers.get("Content-Type")
        if content_type:
            env["CONTENT_TYPE"] = content_type
        if body:
            env["CONTENT_LENGTH"] = str(len(body))
        git_protocol = self.headers.get("Git-Protocol")
        if git_protocol:
            env["GIT_PROTOCOL"] = git_protocol
        try:
            completed = subprocess.run(
                ["git", "http-backend"],
                input=body,
                cwd=self._owner.project_root,
                env=env,
                capture_output=True,
                timeout=_BACKEND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    @staticmethod
    def _parse_cgi_output(stdout: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
        for separator in (b"\r\n\r\n", b"\n\n"):
            head, found, payload = stdout.partition(separator)
            if found:
                break
        else:
            return 500, [], b"malformed CGI response\n"
        status = 200
        headers: list[tuple[str, str]] = []
        for raw_line in head.splitlines():
            line = raw_line.decode("latin-1").strip()
            if not line:
                continue
            name, _, value = line.partition(":")
            name = name.strip()
            value = value.strip()
            if name.lower() == "status":
                status = int(value.split(" ", 1)[0])
            else:
                headers.append((name, value))
        return status, headers, payload

    def _respond(self, status: int, headers: list[tuple[str, str]], payload: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    owner: GitSmartHTTPServer


class GitSmartHTTPServer:
    """Context-managed smart-HTTP frontend over the bare repos in ``project_root``."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)
        self.requests: list[tuple[str, str | None]] = []
        self.expected_auth: dict[str, str] = {}
        self.fail_status: int | None = None
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        server = _Server(("127.0.0.1", 0), _Handler)
        server.owner = self
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> GitSmartHTTPServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not running")
        return self._server.server_address[1]

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def url_for(self, repo_name: str) -> str:
        return f"{self.base_url()}{repo_name}"

    def set_expected_auth(self, repo_name: str, header_value: str) -> None:
        """Require this exact ``Authorization`` header for ``repo_name`` (else 401)."""
        self.expected_auth[repo_name] = header_value

    def fail_all_with(self, status: int | None) -> None:
        """Answer every request with ``status`` until called again with ``None``."""
        self.fail_status = status
