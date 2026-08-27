"""Hardened walk over a freshly materialized directory tree.

A checkout produced from somebody else's repository is hostile input: it can
contain symlinks pointing outside the tree, FIFOs that block a naive reader
forever, ``.git`` directories that turn later ``git`` invocations against the
wrong repository, and files whose type changes between inspection and use.
``guard_tree`` walks such a tree once, refuses every one of those shapes, and
returns a deterministic content digest for what it accepted.

The walk reimplements the descriptor discipline of the release-side scanner
(``release/security/scan.py``): the root is opened ``O_NOFOLLOW | O_DIRECTORY``,
descent happens exclusively through ``dir_fd``-relative opens so no traversal
re-resolves a path from the root, and every regular file's opened descriptor is
compared against its ``lstat`` identity so a swap between the two calls is a
violation, not a silent read of something else. ``release/`` itself must never
be imported from ``src``, which is why the discipline lives here again.

Violation messages carry only the violation code and the caller-supplied
context -- never an entry name. Names inside a hostile tree are hostile text;
the caller knows which checkout it asked about and logs that instead.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "TreeGuardCode",
    "TreeGuardReport",
    "TreeGuardViolation",
    "guard_tree",
]

_CHUNK_BYTES = 1 << 20

_FILE_MODE_EXECUTABLE = 0o100755
_FILE_MODE_REGULAR = 0o100644


class TreeGuardCode(StrEnum):
    """Why a materialized tree was refused."""

    SYMLINK_FOUND = "symlink_found"
    SPECIAL_FILE_FOUND = "special_file_found"
    DOT_GIT_FOUND = "dot_git_found"
    FILE_TOO_LARGE = "file_too_large"
    TREE_TOO_LARGE = "tree_too_large"
    TOO_MANY_FILES = "too_many_files"
    TYPE_CHANGED = "type_changed"
    TRAVERSAL_COMPONENT = "traversal_component"


class TreeGuardViolation(ValueError):  # noqa: N818 -- a refusal verdict, named for what it reports
    """A materialized tree contains something the guard refuses.

    The message is a fixed template over the violation code and the
    caller-supplied context. Entry names never appear: they are attacker-chosen
    text, and the code plus the context identify the refused checkout.
    """

    def __init__(self, code: TreeGuardCode, *, context: str) -> None:
        super().__init__(f"tree guard refused ({code.value}) while walking {context}")
        self.code = code
        self.context = context


@dataclass(frozen=True)
class TreeGuardReport:
    """What an accepted tree contained."""

    file_count: int
    total_bytes: int
    tree_digest: str


@dataclass(frozen=True)
class _Limits:
    max_file_bytes: int
    max_total_bytes: int
    max_file_count: int
    context: str


@dataclass
class _Totals:
    file_count: int = 0
    total_bytes: int = 0


def guard_tree(
    root: Path,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
    max_file_count: int,
    context: str,
) -> TreeGuardReport:
    r"""Walk ``root``, refuse hostile shapes, and digest the accepted content.

    Args:
        root: Directory to walk. Opened ``O_NOFOLLOW | O_DIRECTORY``; a symlink
            here fails at the OS level before any traversal happens.
        max_file_bytes: Cap on any single regular file, enforced while
            streaming so a file that lies about its size still trips it.
        max_total_bytes: Cap on the byte total across the whole tree.
        max_file_count: Cap on the number of regular files.
        context: Caller-supplied label naming the checkout, used verbatim in
            violation messages in place of any entry name.

    Returns:
        A :class:`TreeGuardReport` whose ``tree_digest`` is ``sha256:<hex>``
        over, for each regular file in byte-sorted relative-path order,
        ``b"%o\\x00" % mode + path + b"\\x00" + content_sha256_hex + b"\\n"``,
        with the mode normalized to ``100755`` when any execute bit is set and
        ``100644`` otherwise. Directories contribute nothing, so an empty tree
        digests the empty string.

    Raises:
        TreeGuardViolation: The tree contains a symlink, a special file, a
            ``.git`` component, a traversal-shaped name, exceeds a cap, or an
            entry changed type between inspection and open.
        OSError: The tree could not be opened or read. This includes a
            symlinked ``root`` (``O_NOFOLLOW`` makes ``os.open`` raise
            ``ELOOP``): a symlinked checkout root is hostile, so the caller
            must treat a root-level ``OSError`` as a refusal, not as a broken
            disk.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("treeguard requires O_NOFOLLOW/O_DIRECTORY descriptor isolation")
    limits = _Limits(
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_file_count=max_file_count,
        context=context,
    )
    totals = _Totals()
    files: list[tuple[str, int, str]] = []
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _walk_directory(root_fd, (), limits, totals, files)
    finally:
        os.close(root_fd)
    files.sort(key=lambda item: os.fsencode(item[0]))
    tree_hash = hashlib.sha256()
    for relative_path, mode_bits, content_hex in files:
        tree_hash.update(b"%o\x00" % mode_bits)
        tree_hash.update(os.fsencode(relative_path))
        tree_hash.update(b"\x00")
        tree_hash.update(content_hex.encode("ascii"))
        tree_hash.update(b"\n")
    return TreeGuardReport(
        file_count=totals.file_count,
        total_bytes=totals.total_bytes,
        tree_digest=f"sha256:{tree_hash.hexdigest()}",
    )


def _validate_component(name: str, *, context: str) -> None:
    if name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise TreeGuardViolation(TreeGuardCode.TRAVERSAL_COMPONENT, context=context)
    if name.lower() in {".git", "git~1"}:
        raise TreeGuardViolation(TreeGuardCode.DOT_GIT_FOUND, context=context)


def _walk_directory(
    dir_fd: int,
    parts: tuple[str, ...],
    limits: _Limits,
    totals: _Totals,
    files: list[tuple[str, int, str]],
) -> None:
    with os.scandir(dir_fd) as iterator:
        names = sorted((entry.name for entry in iterator), key=os.fsencode)
    for name in names:
        _validate_component(name, context=limits.context)
        status = os.lstat(name, dir_fd=dir_fd)
        mode = status.st_mode
        if stat.S_ISLNK(mode):
            raise TreeGuardViolation(TreeGuardCode.SYMLINK_FOUND, context=limits.context)
        if stat.S_ISDIR(mode):
            _descend_into(dir_fd, name, status, parts, limits, totals, files)
        elif stat.S_ISREG(mode):
            _consume_file(dir_fd, name, status, parts, limits, totals, files)
        else:
            raise TreeGuardViolation(TreeGuardCode.SPECIAL_FILE_FOUND, context=limits.context)


def _descend_into(
    parent_fd: int,
    name: str,
    listed: os.stat_result,
    parts: tuple[str, ...],
    limits: _Limits,
    totals: _Totals,
    files: list[tuple[str, int, str]],
) -> None:
    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        opened = os.fstat(child_fd)
        identity_moved = (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino)
        if identity_moved or not stat.S_ISDIR(opened.st_mode):
            raise TreeGuardViolation(TreeGuardCode.TYPE_CHANGED, context=limits.context)
        _walk_directory(child_fd, (*parts, name), limits, totals, files)
    finally:
        os.close(child_fd)


def _consume_file(
    parent_fd: int,
    name: str,
    listed: os.stat_result,
    parts: tuple[str, ...],
    limits: _Limits,
    totals: _Totals,
    files: list[tuple[str, int, str]],
) -> None:
    totals.file_count += 1
    if totals.file_count > limits.max_file_count:
        raise TreeGuardViolation(TreeGuardCode.TOO_MANY_FILES, context=limits.context)
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        opened = os.fstat(file_fd)
        identity_moved = (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino)
        if identity_moved or not stat.S_ISREG(opened.st_mode):
            raise TreeGuardViolation(TreeGuardCode.TYPE_CHANGED, context=limits.context)
        content_hex = _stream_content(file_fd, limits, totals)
    finally:
        os.close(file_fd)
    executable = stat.S_IMODE(opened.st_mode) & 0o111
    mode_bits = _FILE_MODE_EXECUTABLE if executable else _FILE_MODE_REGULAR
    files.append(("/".join((*parts, name)), mode_bits, content_hex))


def _stream_content(file_fd: int, limits: _Limits, totals: _Totals) -> str:
    content_hash = hashlib.sha256()
    file_bytes = 0
    while chunk := os.read(file_fd, _CHUNK_BYTES):
        file_bytes += len(chunk)
        if file_bytes > limits.max_file_bytes:
            raise TreeGuardViolation(TreeGuardCode.FILE_TOO_LARGE, context=limits.context)
        totals.total_bytes += len(chunk)
        if totals.total_bytes > limits.max_total_bytes:
            raise TreeGuardViolation(TreeGuardCode.TREE_TOO_LARGE, context=limits.context)
        content_hash.update(chunk)
    return content_hash.hexdigest()
