from __future__ import annotations

import hashlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import release.app_certification.cli as certification_cli
from release.app_certification import AppDeclaration, file_digest
from release.app_certification.cli import UrlHttpBoundary
from release.app_certification.cli import _probe_readiness
from tests.app_certification.test_engine import (
    declaration_data,
    run_certification,
    write_inputs,
)


def _failed_smoke(tmp_path: Path, monkeypatch, error: Exception):
    def fail_exchange(*args, **kwargs):
        raise error

    monkeypatch.setattr(certification_cli, "run_http_exchange", fail_exchange)
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())
    boundary = UrlHttpBoundary("http://packaged", "http://ephemeral", {})
    report = run_certification(tmp_path, declaration, http=boundary)
    result = next(check for check in report.checks if check.name == "packaged-smoke")
    return report, result


def test_smoke_response_over_byte_limit_is_retained_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, result = _failed_smoke(
        tmp_path, monkeypatch, ValueError("HTTP response exceeded 1 MiB limit")
    )

    assert report.status == "failed"
    assert result.status == "failed"
    assert "response exceeded 1 MiB limit" in result.detail


def test_continuously_streamed_smoke_response_hits_overall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, result = _failed_smoke(
        tmp_path,
        monkeypatch,
        TimeoutError("HTTP exchange exceeded total deadline"),
    )

    assert report.status == "failed"
    assert result.status == "failed"
    assert "exchange exceeded total deadline" in result.detail


class _ChunkedReader:
    def __init__(self, stream, sizes: list[int]) -> None:
        self.stream = stream
        self.sizes = sizes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def read(self, size: int = -1) -> bytes:
        self.sizes.append(size)
        return self.stream.read(size)


def test_file_digest_reads_fixed_size_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"archive" * 400_000
    archive = tmp_path / "image.tar"
    archive.write_bytes(payload)
    sizes: list[int] = []
    path_open = Path.open

    def reject_whole_read(self):
        raise AssertionError("whole-file reads are forbidden")

    def guarded_open(self, *args, **kwargs):
        stream = path_open(self, *args, **kwargs)
        return _ChunkedReader(stream, sizes) if self == archive else stream

    monkeypatch.setattr(Path, "read_bytes", reject_whole_read)
    monkeypatch.setattr(Path, "open", guarded_open)

    assert file_digest(archive) == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert len(sizes) >= 3
    assert set(sizes) == {1 << 20}


def _write_candidate_import_attack(root: Path, marker: Path) -> None:
    (root / "candidate_attack.py").write_text(
        "from pydantic import BaseModel\n"
        "from zeroth.contracts.graph import Graph, EntrypointNode\n"
        "class Payload(BaseModel):\n    value: str\n"
        "CONTRACTS = {'contract://payload': Payload}\n"
        "def forged_builder():\n"
        "    node = EntrypointNode(node_id='start', graph_version_ref='attack@1', "
        "input_contract_ref='contract://payload', output_contract_ref='contract://payload')\n"
        "    return Graph(graph_id='attack', name='attack', version=1, "
        "entry_step='start', nodes=[node], edges=[])\n"
        "try:\n    from release.app_certification import checks\n"
        "except ImportError:\n    pass\n"
        "else:\n"
        "    checks._contracts = lambda declaration: CONTRACTS\n"
        "    checks._load_target = lambda reference: forged_builder\n",
        encoding="utf-8",
    )
    (root / "invalid_target.py").write_text(
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "def build_graph():\n"
        "    MARKER.write_text('executed', encoding='utf-8')\n"
        "    raise RuntimeError('declared invalid target executed')\n",
        encoding="utf-8",
    )


def test_candidate_import_cannot_replace_certifier_collectors(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "invalid-target-executed"
    _write_candidate_import_attack(tmp_path, marker)
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"
    data["targets"]["graph_builders"] = ["invalid_target:build_graph"]
    runner = certification_cli.CertificationRunner(tmp_path, AppDeclaration.model_validate(data))

    result = runner._command("graph")

    assert result.status == "failed"
    assert "declared invalid target executed" in result.detail
    assert marker.read_text(encoding="utf-8") == "executed"


@pytest.mark.parametrize("recovery", ["frame", "gc"])
def test_candidate_reflection_cannot_replace_certifier_collectors(
    tmp_path: Path, recovery: str
) -> None:
    marker = tmp_path / "invalid-target-executed"
    recover = (
        "frame = sys._getframe()\n"
        "while frame is not None:\n"
        "    owned = frame.f_globals.get('checks')\n"
        "    if getattr(owned, '__name__', '') == 'release.app_certification.checks':\n"
        "        break\n"
        "    frame = frame.f_back\n"
        if recovery == "frame"
        else "owned = next(obj for obj in gc.get_objects() "
        "if isinstance(obj, types.ModuleType) and "
        "getattr(obj, '__name__', '') == 'release.app_certification.checks')\n"
    )
    (tmp_path / "candidate_frame_attack.py").write_text(
        "import gc, sys, types\n"
        "from pydantic import BaseModel\n"
        "from zeroth.contracts.graph import Graph, EntrypointNode\n"
        "class Payload(BaseModel):\n    value: str\n"
        "CONTRACTS = {'contract://payload': Payload}\n"
        "def forged_graph():\n"
        "    node = EntrypointNode(node_id='start', graph_version_ref='attack@1', "
        "input_contract_ref='contract://payload', output_contract_ref='contract://payload')\n"
        "    return Graph(graph_id='attack', name='attack', version=1, "
        "entry_step='start', nodes=[node], edges=[])\n"
        + recover
        + "owned._contracts = lambda declaration: CONTRACTS\n"
        "owned._graphs = lambda declaration: [forged_graph()]\n",
        encoding="utf-8",
    )
    (tmp_path / "invalid_target.py").write_text(
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "def build_graph():\n"
        "    MARKER.write_text('executed', encoding='utf-8')\n"
        "    raise RuntimeError('declared invalid target executed')\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_frame_attack:CONTRACTS"
    data["targets"]["graph_builders"] = ["invalid_target:build_graph"]
    runner = certification_cli.CertificationRunner(tmp_path, AppDeclaration.model_validate(data))

    result = runner._command("graph")

    assert result.status == "failed"
    assert "declared invalid target executed" in result.detail
    assert marker.read_text(encoding="utf-8") == "executed"


def _write_frontend_toolchain(root: Path, *, handoff: Path | None = None) -> None:
    scripts = root / "scripts"
    compiler = root / "frontend/node_modules/.bin/openapi-typescript"
    scripts.mkdir(parents=True)
    compiler.parent.mkdir(parents=True)
    generator = (
        "from argparse import ArgumentParser\n"
        "from pathlib import Path\n"
        "parser = ArgumentParser()\n"
        "parser.add_argument('--out', type=Path, required=True)\n"
        "args = parser.parse_args()\n"
        + (
            f"Path({str(handoff)!r}).write_text('forged', encoding='utf-8')\n"
            if handoff
            else ""
        )
        + "args.out.write_text('{}\\n', encoding='utf-8')\n"
    )
    for name in ("dump_openapi.py", "dump_regulus_openapi.py"):
        (scripts / name).write_text(generator, encoding="utf-8")
    compiler.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        + (f"Path({str(handoff)!r}).write_text('forged', encoding='utf-8')\n" if handoff else "")
        + "Path(sys.argv[sys.argv.index('-o') + 1]).write_text('types\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    compiler.chmod(0o755)


def test_frontend_check_never_executes_candidate_compiler_with_handoff_access(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted"
    candidate = tmp_path / "candidate"
    handoff = tmp_path / "report.json"
    handoff.write_text("trusted", encoding="utf-8")
    _write_frontend_toolchain(trusted)
    _write_frontend_toolchain(candidate, handoff=handoff)
    checker = trusted / "scripts/check_frontend_api.py"
    shutil.copyfile(Path("scripts/check_frontend_api.py"), checker)
    frontend = candidate / "frontend"
    for path in (
        frontend / "openapi.json",
        frontend / "openapi.regulus.json",
    ):
        path.write_text("{}\n", encoding="utf-8")
    for path in (
        frontend / "app/lib/api-types.ts",
        frontend / "app/lib/api-types.regulus.ts",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("types\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(checker), "--frontend", str(frontend)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert handoff.read_text(encoding="utf-8") == "trusted"


class _SlowRedirectHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        self._drip(
            b"HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Length: 0\r\n\r\n"
        )

    def do_GET(self) -> None:
        self._drip(
            b"HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\n"
            b"Content-Length: 56\r\n\r\n"
            b'{"status":"accepted","result":{"case":"fixed"}}'
        )

    def _drip(self, payload: bytes) -> None:
        try:
            for byte in payload:
                self.connection.sendall(bytes((byte,)))
                time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _loopback_server(handler=BaseHTTPRequestHandler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_smoke_total_deadline_covers_slow_status_headers_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread = _loopback_server(_SlowRedirectHandler)
    monkeypatch.setattr(certification_cli, "_HTTP_TIMEOUT_SECONDS", 0.08)
    boundary = UrlHttpBoundary(
        f"http://127.0.0.1:{server.server_port}",
        f"http://127.0.0.1:{server.server_port}",
        {},
    )
    smoke = AppDeclaration.model_validate(declaration_data()).smoke.model_copy(
        update={"path": "/redirect"}
    )
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            boundary("packaged-smoke", smoke)
    finally:
        elapsed = time.monotonic() - started
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert elapsed < 0.4


class _LargeReadinessHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        payload = b'{"status":"ok","padding":"' + b"x" * (1 << 20) + b'"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _LargeHeaderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        try:
            self.send_response(200)
            for index in range(20):
                self.send_header(f"X-Padding-{index}", "x" * 60_000)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        except (BrokenPipeError, ConnectionResetError):
            pass


def test_readiness_response_has_cumulative_byte_limit() -> None:
    server, thread = _loopback_server(_LargeReadinessHandler)
    try:
        with pytest.raises(ValueError, match="exceeded 1 MiB limit"):
            _probe_readiness(f"http://127.0.0.1:{server.server_port}/ready")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_readiness_headers_share_the_cumulative_byte_limit() -> None:
    server, thread = _loopback_server(_LargeHeaderHandler)
    try:
        with pytest.raises(ValueError, match="exceeded 1 MiB limit"):
            _probe_readiness(f"http://127.0.0.1:{server.server_port}/ready")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
