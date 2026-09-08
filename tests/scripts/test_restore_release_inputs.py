"""External release inputs must be pinned, complete and safe to restore."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/restore_release_inputs.py"


def _fixture(tmp_path, *, member_name="release/load/baseline.json", symlink=False, duplicate=False):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("/release/load/baseline.json\n")
    data = b'{"measured": 42}\n'
    archive = tmp_path / "inputs.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        item = tarfile.TarInfo(member_name)
        if symlink:
            item.type = tarfile.SYMTYPE
            item.linkname = "../../outside"
            tar.addfile(item)
        else:
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
            if duplicate:
                tar.addfile(item, io.BytesIO(data))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "archive_size": archive.stat().st_size,
                "files": {
                    "release/load/baseline.json": {
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                },
            }
        )
    )
    return root, archive, manifest, data


def _run(root, archive, manifest):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--manifest",
            str(manifest),
            "--archive",
            str(archive),
        ],
        capture_output=True,
        text=True,
    )


def test_restores_exact_ignored_inputs_and_can_repeat(tmp_path):
    root, archive, manifest, data = _fixture(tmp_path)
    for _ in range(2):
        result = _run(root, archive, manifest)
        assert result.returncode == 0, result.stderr
        assert (root / "release/load/baseline.json").read_bytes() == data
    assert subprocess.check_output(["git", "-C", str(root), "ls-files"]) == b""


def test_archive_digest_mismatch_writes_nothing(tmp_path):
    root, archive, manifest, _ = _fixture(tmp_path)
    raw = bytearray(archive.read_bytes())
    raw[-1] ^= 1
    archive.write_bytes(raw)
    result = _run(root, archive, manifest)
    assert result.returncode != 0
    assert "archive digest mismatch" in result.stderr
    assert not (root / "release").exists()


@pytest.mark.parametrize(
    "options",
    [
        {"member_name": "../outside"},
        {"symlink": True},
        {"duplicate": True},
        {"member_name": "release/load/unexpected.json"},
    ],
)
def test_invalid_archive_structure_writes_nothing(tmp_path, options):
    root, archive, manifest, _ = _fixture(tmp_path, **options)
    result = _run(root, archive, manifest)
    assert result.returncode != 0
    assert "invalid archive member" in result.stderr
    assert not (root / "release").exists()
    assert not (tmp_path / "outside").exists()


def test_file_digest_mismatch_writes_nothing(tmp_path):
    root, archive, manifest, _ = _fixture(tmp_path)
    value = json.loads(manifest.read_text())
    value["files"]["release/load/baseline.json"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(value))
    result = _run(root, archive, manifest)
    assert result.returncode != 0
    assert "file digest mismatch" in result.stderr
    assert not (root / "release").exists()


def test_refuses_modified_existing_input(tmp_path):
    root, archive, manifest, _ = _fixture(tmp_path)
    target = root / "release/load/baseline.json"
    target.parent.mkdir(parents=True)
    target.write_text("retain this local work")
    result = _run(root, archive, manifest)
    assert result.returncode != 0
    assert "existing input differs" in result.stderr
    assert target.read_text() == "retain this local work"


def test_refuses_tracked_input(tmp_path):
    root, archive, manifest, data = _fixture(tmp_path)
    target = root / "release/load/baseline.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    subprocess.run(["git", "-C", str(root), "add", "-f", str(target)], check=True)
    result = _run(root, archive, manifest)
    assert result.returncode != 0
    assert "input is tracked" in result.stderr


def test_refuses_destination_symlink(tmp_path):
    root, archive, manifest, _ = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "release").symlink_to(outside, target_is_directory=True)
    result = _run(root, archive, manifest)
    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert list(outside.iterdir()) == []
