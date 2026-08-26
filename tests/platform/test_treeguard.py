"""Hostile-tree refusal and deterministic digesting for guard_tree.

ZER-37: a checkout materialized from somebody else's repository is hostile
input. The guard must refuse symlinks, special files, ``.git`` components, and
cap violations without ever echoing an attacker-chosen entry name, and must
digest an accepted tree identically regardless of creation order.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from zeroth.platform.primitives.treeguard import (
    TreeGuardCode,
    TreeGuardViolation,
    guard_tree,
)

CONTEXT = "test checkout"

_ROOMY = {
    "max_file_bytes": 1 << 20,
    "max_total_bytes": 1 << 24,
    "max_file_count": 10_000,
    "context": CONTEXT,
}


def _guard(root: Path, **overrides):
    return guard_tree(root, **{**_ROOMY, **overrides})


def _violation(root: Path, **overrides) -> TreeGuardViolation:
    with pytest.raises(TreeGuardViolation) as excinfo:
        _guard(root, **overrides)
    return excinfo.value


def _populate(root: Path, entries: list[tuple[str, bytes]]) -> None:
    for relative, content in entries:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


_BENIGN_ENTRIES = [
    ("a.txt", b"alpha"),
    ("nested/b.txt", b"beta"),
    ("nested/deeper/c.bin", b"\x00\x01\x02"),
]


def test_benign_tree_digest_is_deterministic_and_order_independent(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _populate(first, _BENIGN_ENTRIES)
    _populate(second, list(reversed(_BENIGN_ENTRIES)))

    report_first = _guard(first)
    report_second = _guard(second)

    assert report_first == report_second
    assert report_first.file_count == 3
    assert report_first.total_bytes == sum(len(content) for _, content in _BENIGN_ENTRIES)
    assert report_first.tree_digest.startswith("sha256:")
    # Same walk again: byte-for-byte stable.
    assert _guard(first) == report_first


def test_content_change_changes_digest(tmp_path: Path) -> None:
    _populate(tmp_path, _BENIGN_ENTRIES)
    before = _guard(tmp_path).tree_digest
    (tmp_path / "a.txt").write_bytes(b"alpha-modified")
    assert _guard(tmp_path).tree_digest != before


def test_executable_bit_changes_digest(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_bytes(b"#!/bin/sh\n")
    plain = _guard(tmp_path).tree_digest
    script.chmod(0o755)
    assert _guard(tmp_path).tree_digest != plain


@pytest.mark.parametrize("target", ["../../etc/passwd", "/etc/passwd"])
def test_symlink_is_refused(tmp_path: Path, target: str) -> None:
    (tmp_path / "ok.txt").write_bytes(b"fine")
    os.symlink(target, tmp_path / "escape")
    assert _violation(tmp_path).code is TreeGuardCode.SYMLINK_FOUND


def test_fifo_is_refused(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    assert _violation(tmp_path).code is TreeGuardCode.SPECIAL_FILE_FOUND


@pytest.mark.parametrize("name", [".git", ".GIT", "git~1", "GIT~1"])
def test_dot_git_directory_is_refused(tmp_path: Path, name: str) -> None:
    (tmp_path / "nested" / name).mkdir(parents=True)
    assert _violation(tmp_path).code is TreeGuardCode.DOT_GIT_FOUND


def test_dot_git_file_is_refused(tmp_path: Path) -> None:
    nested = tmp_path / "vendor"
    nested.mkdir()
    (nested / ".git").write_bytes(b"gitdir: elsewhere\n")
    assert _violation(tmp_path).code is TreeGuardCode.DOT_GIT_FOUND


def test_per_file_cap(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 1024)
    assert _violation(tmp_path, max_file_bytes=1023).code is TreeGuardCode.FILE_TOO_LARGE


def test_total_cap(tmp_path: Path) -> None:
    _populate(tmp_path, [("one.bin", b"x" * 600), ("two.bin", b"y" * 600)])
    violation = _violation(tmp_path, max_file_bytes=1 << 20, max_total_bytes=1000)
    assert violation.code is TreeGuardCode.TREE_TOO_LARGE


def test_file_count_cap(tmp_path: Path) -> None:
    _populate(tmp_path, [(f"f{index}.txt", b"x") for index in range(4)])
    assert _violation(tmp_path, max_file_count=3).code is TreeGuardCode.TOO_MANY_FILES


def test_hostile_entry_name_never_appears_in_violation_text(tmp_path: Path) -> None:
    canary = "CANARY-secret-hostname.internal"
    os.symlink("/etc/passwd", tmp_path / canary)
    violation = _violation(tmp_path)
    assert canary not in str(violation)
    assert canary not in repr(violation)
    assert CONTEXT in str(violation)


def test_hostile_name_under_caps_never_appears_either(tmp_path: Path) -> None:
    canary = "CANARY-oversized-blob.bin"
    (tmp_path / canary).write_bytes(b"z" * 64)
    violation = _violation(tmp_path, max_file_bytes=10)
    assert violation.code is TreeGuardCode.FILE_TOO_LARGE
    assert canary not in str(violation)


def test_empty_tree(tmp_path: Path) -> None:
    report = _guard(tmp_path)
    assert report.file_count == 0
    assert report.total_bytes == 0
    assert report.tree_digest == f"sha256:{hashlib.sha256(b'').hexdigest()}"


def test_directories_alone_do_not_change_the_digest(tmp_path: Path) -> None:
    _populate(tmp_path, _BENIGN_ENTRIES)
    before = _guard(tmp_path)
    (tmp_path / "empty-dir").mkdir()
    assert _guard(tmp_path) == before


def test_symlinked_root_is_refused_at_open(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(OSError):
        _guard(link)
