from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

import release.app_certification.cli as certification_cli
from release.app_certification import AppDeclaration, file_digest
from release.app_certification.cli import UrlHttpBoundary
from tests.app_certification.test_engine import (
    declaration_data,
    run_certification,
    write_inputs,
)


class _Response:
    status = 202

    def __init__(self, payload: bytes, *, drip: bool = False) -> None:
        self._stream = io.BytesIO(payload)
        self.drip = drip
        self.read1_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def read1(self, size: int) -> bytes:
        self.read1_calls += 1
        return b" " if self.drip else self._stream.read(size)


def _failed_smoke(tmp_path: Path, monkeypatch, response_factory):
    responses: list[_Response] = []

    def open_response(*args, **kwargs):
        response = response_factory()
        responses.append(response)
        return response

    monkeypatch.setattr(certification_cli, "urlopen", open_response)
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())
    boundary = UrlHttpBoundary("http://packaged", "http://ephemeral", {})
    report = run_certification(tmp_path, declaration, http=boundary)
    result = next(check for check in report.checks if check.name == "packaged-smoke")
    return report, result, responses


def test_smoke_response_over_byte_limit_is_retained_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b'{"padding":"' + b"x" * (1 << 20) + b'"}'

    report, result, _ = _failed_smoke(tmp_path, monkeypatch, lambda: _Response(payload))

    assert report.status == "failed"
    assert result.status == "failed"
    assert "response exceeded 1 MiB limit" in result.detail


def test_continuously_streamed_smoke_response_hits_overall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter((0.0, 0.0, 31.0, 100.0, 100.0, 131.0))
    monkeypatch.setattr(certification_cli, "monotonic", lambda: next(ticks), raising=False)

    report, result, responses = _failed_smoke(
        tmp_path, monkeypatch, lambda: _Response(b"", drip=True)
    )

    assert report.status == "failed"
    assert result.status == "failed"
    assert "response read deadline exceeded" in result.detail
    assert responses[0].read1_calls == 1


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
