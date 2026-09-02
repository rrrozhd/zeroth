"""Loopback-only SMTP receiver for deterministic delivery fault tests."""

import socketserver
import threading
from contextlib import contextmanager


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        self.wfile.write(b"220 local.test ESMTP\r\n")
        while line := self.rfile.readline():
            command = line.decode("ascii", errors="replace").strip()
            if command.upper().startswith(("EHLO", "HELO")):
                self.wfile.write(b"250 local.test\r\n")
            elif command.upper().startswith("MAIL"):
                self.wfile.write(b"250 sender ok\r\n")
            elif command.upper().startswith("RCPT"):
                reject = self.server.mode == "reject" or (
                    self.server.mode == "partial" and "rejected@" in command
                )
                self.wfile.write(
                    b"550 recipient rejected\r\n" if reject else b"250 recipient ok\r\n"
                )
            elif command.upper() == "DATA":
                self.wfile.write(b"354 end with dot\r\n")
                parts = []
                while (part := self.rfile.readline()) not in (b".\r\n", b""):
                    parts.append(part[1:] if part.startswith(b"..") else part)
                self.server.messages.append(b"".join(parts))
                if self.server.mode == "timeout":
                    self.server.release.wait(2)
                    return
                self.wfile.write(b"250 relay accepted\r\n")
            elif command.upper() == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


@contextmanager
def receiver(mode="accept"):
    if mode not in {"accept", "reject", "partial", "timeout"}:
        raise ValueError("unsupported local fault")
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        server.mode = mode
        server.messages = []
        server.release = threading.Event()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server
        finally:
            server.release.set()
            server.shutdown()
            thread.join(timeout=2)
