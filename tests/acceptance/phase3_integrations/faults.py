"""A loopback HTTP proxy that injects delivery faults between an SDK client and the server.

The schedule decides per request (1-based index): ``"pass"`` forwards it,
an integer answers with that status without forwarding, ``"drop"`` closes the
connection without a reply. Faults never reach the server, so a faulted event
is exactly as absent from the ledger as a lost one in production.
"""

from __future__ import annotations

import http.client
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

Schedule = Callable[[int], int | str]


class Proxy:
    def __init__(self, origin: str, schedule: Schedule) -> None:
        self.origin, self.schedule = urlsplit(origin), schedule
        self.count = 0
        self.faulted: list[tuple[int, int | str]] = []
        self._lock = threading.Lock()

    def decide(self) -> tuple[int, int | str]:
        with self._lock:
            self.count += 1
            action = self.schedule(self.count)
            if action != "pass":
                self.faulted.append((self.count, action))
            return self.count, action


def _handler(proxy: Proxy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: D102 - quiet
            return

        def do_POST(self):  # noqa: N802 - http.server API
            self._relay()

        def do_GET(self):  # noqa: N802 - http.server API
            self._relay()

        def _relay(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            _, action = proxy.decide()
            if action == "drop":
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if isinstance(action, int):
                payload = b'{"detail":"injected by fault proxy"}'
                self.send_response(action)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            upstream = http.client.HTTPConnection(proxy.origin.hostname, proxy.origin.port, timeout=30)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()
            data = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            upstream.close()

    return Handler


@contextmanager
def faulty(origin: str, schedule: Schedule) -> Iterator[tuple[str, Proxy]]:
    proxy = Proxy(origin, schedule)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(proxy))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", proxy
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
