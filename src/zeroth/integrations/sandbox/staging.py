"""Workspace staging for the sandbox sidecar (ZER-37).

The sidecar accepts an uncompressed POSIX tar over HTTP, validates it without
extracting a single byte, and later re-authors a fresh archive whose headers
the sidecar itself wrote before any helper container parses it. The two halves
of that discipline live here:

* ``validate_spooled_tar`` walks the client's archive with :mod:`tarfile`
  (no extraction) and refuses anything but plain files and directories with
  clean relative names.
* ``reauthor_tar`` / ``split_reauthored_tar`` emit a brand-new GNU-format tar
  with normalized names, ``uid=gid=0``, ``mtime=0`` and clamped modes, copying
  file content in chunks. The helper container that populates a Docker volume
  therefore never parses attacker-controlled headers.

Error discipline: every rejection is a :class:`WorkspaceValidationError` — a
``ValueError`` so the sidecar app's existing exception→4xx mapping applies —
carrying a :class:`WorkspaceValidationCode` and a fixed message template.
Member names are attacker-controlled and are NEVER echoed into an error
string, matching the image-validator discipline pinned in
``tests/sandbox_sidecar/test_executor_argv.py``.
"""

from __future__ import annotations

import asyncio
import os
import re
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncIterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

#: Ceiling on the raw (and total-file) bytes of one staged workspace archive.
DEFAULT_MAX_WORKSPACE_BYTES = 134_217_728

#: How long an uploaded-but-unclaimed workspace spool survives before the TTL
#: sweep deletes it (the id stays burned; see :meth:`WorkspaceStore.sweep`).
DEFAULT_WORKSPACE_TTL_SECONDS = 900.0

#: Image used for the volume-populate and output-capture helper containers.
DEFAULT_HELPER_IMAGE = "busybox:stable"

#: Ceiling on how many members one workspace archive may carry.
DEFAULT_MAX_TAR_MEMBERS = 4096

#: Longest member name the sidecar will accept, in UTF-8 bytes.
_MAX_MEMBER_NAME_BYTES = 255

_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Compression magics refused at offset 0: the channel is uncompressed POSIX
#: tar only, so size caps measure what they claim to measure.
_COMPRESSION_MAGICS = (
    b"\x1f\x8b",  # gzip
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"\x28\xb5\x2f\xfd",  # zstd
)

_ALLOWED_MEMBER_TYPES = frozenset({tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE})


class WorkspaceValidationCode(StrEnum):
    """Stable machine-readable reasons a staging operation was refused."""

    TAR_TOO_LARGE = "tar_too_large"
    TAR_MEMBER_FORBIDDEN = "tar_member_forbidden"
    TAR_MEMBER_TRAVERSAL = "tar_member_traversal"
    TAR_SPARSE_FORBIDDEN = "tar_sparse_forbidden"
    TAR_COMPRESSED = "tar_compressed"
    TAR_MALFORMED = "tar_malformed"
    TAR_TOO_MANY_MEMBERS = "tar_too_many_members"
    TAR_MEMBER_NAME_TOO_LONG = "tar_member_name_too_long"
    TAR_DUPLICATE_MEMBER = "tar_duplicate_member"
    WORKSPACE_ID_INVALID = "workspace_id_invalid"
    WORKSPACE_UNKNOWN = "workspace_unknown"
    WORKSPACE_CONSUMED = "workspace_consumed"
    WORKSPACE_DUPLICATE = "workspace_duplicate"


#: Fixed message templates. Deliberately closed over the code alone: member
#: names and other client-controlled strings never reach an error message.
_MESSAGES: dict[WorkspaceValidationCode, str] = {
    WorkspaceValidationCode.TAR_TOO_LARGE: (
        "workspace archive exceeds the configured size cap"
    ),
    WorkspaceValidationCode.TAR_MEMBER_FORBIDDEN: (
        "workspace archive contains a forbidden member type"
    ),
    WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL: (
        "workspace archive contains a member with a non-relative or unsafe path"
    ),
    WorkspaceValidationCode.TAR_SPARSE_FORBIDDEN: (
        "workspace archive contains a sparse member"
    ),
    WorkspaceValidationCode.TAR_COMPRESSED: (
        "workspace archive must be an uncompressed POSIX tar stream"
    ),
    WorkspaceValidationCode.TAR_MALFORMED: (
        "workspace archive is not a readable tar stream"
    ),
    WorkspaceValidationCode.TAR_TOO_MANY_MEMBERS: (
        "workspace archive exceeds the member count cap"
    ),
    WorkspaceValidationCode.TAR_MEMBER_NAME_TOO_LONG: (
        "workspace archive contains an over-long member name"
    ),
    WorkspaceValidationCode.TAR_DUPLICATE_MEMBER: (
        "workspace archive contains duplicate member names"
    ),
    WorkspaceValidationCode.WORKSPACE_ID_INVALID: (
        "workspace id must match the permitted identifier charset"
    ),
    WorkspaceValidationCode.WORKSPACE_UNKNOWN: (
        "workspace is not staged on this sidecar"
    ),
    WorkspaceValidationCode.WORKSPACE_CONSUMED: (
        "workspace has already been consumed or expired"
    ),
    WorkspaceValidationCode.WORKSPACE_DUPLICATE: (
        "workspace id has already been staged"
    ),
}


class WorkspaceValidationError(ValueError):
    """A staging request was refused; ``code`` says why, the message stays generic."""

    def __init__(self, code: WorkspaceValidationCode) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])


def validate_workspace_id(workspace_id: str) -> str:
    """Refuse any id that is not a docker-volume-safe identifier."""
    if not _WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise WorkspaceValidationError(WorkspaceValidationCode.WORKSPACE_ID_INVALID)
    return workspace_id


def normalize_member_name(name: str) -> str:
    """Return a clean relative POSIX name for a tar member, or refuse it.

    ``.`` parts and a trailing slash normalize away; an empty result is legal
    only for a directory member (the archive root), which callers skip.
    """
    if "\x00" in name or "\\" in name:
        raise WorkspaceValidationError(WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL)
    if len(name.encode("utf-8", errors="surrogateescape")) > _MAX_MEMBER_NAME_BYTES:
        raise WorkspaceValidationError(WorkspaceValidationCode.TAR_MEMBER_NAME_TOO_LONG)
    if name.startswith("/"):
        raise WorkspaceValidationError(WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL)
    trimmed = name.removesuffix("/")
    parts: list[str] = []
    for part in trimmed.split("/"):
        if part == ".":
            continue
        if part in ("", ".."):
            raise WorkspaceValidationError(WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL)
        parts.append(part)
    return "/".join(parts)


@dataclass
class TarSummary:
    """What a validated workspace archive contains."""

    member_count: int = 0
    total_file_bytes: int = 0
    raw_bytes: int = 0


def _validate_member(member: tarfile.TarInfo) -> None:
    """Refuse every member kind the staging channel does not carry."""
    if (
        member.type == tarfile.GNUTYPE_SPARSE
        or member.sparse is not None
        or any(key.startswith("GNU.sparse") for key in member.pax_headers)
    ):
        raise WorkspaceValidationError(WorkspaceValidationCode.TAR_SPARSE_FORBIDDEN)
    if member.type not in _ALLOWED_MEMBER_TYPES:
        raise WorkspaceValidationError(WorkspaceValidationCode.TAR_MEMBER_FORBIDDEN)


def validate_spooled_tar(
    path: Path,
    *,
    max_total_file_bytes: int,
    max_members: int = DEFAULT_MAX_TAR_MEMBERS,
) -> TarSummary:
    """Walk a spooled archive without extracting anything and refuse hostility.

    Only plain files and directories with clean relative names pass. The size
    total is enforced mid-walk so a lying header fails as early as a real one.
    """
    with path.open("rb") as handle:
        head = handle.read(6)
    if any(head.startswith(magic) for magic in _COMPRESSION_MAGICS):
        raise WorkspaceValidationError(WorkspaceValidationCode.TAR_COMPRESSED)

    summary = TarSummary(raw_bytes=path.stat().st_size)
    seen: set[str] = set()
    try:
        with tarfile.open(path, mode="r:", errorlevel=2) as archive:
            for member in archive:
                summary.member_count += 1
                if summary.member_count > max_members:
                    raise WorkspaceValidationError(
                        WorkspaceValidationCode.TAR_TOO_MANY_MEMBERS
                    )
                _validate_member(member)
                normalized = normalize_member_name(member.name)
                if not normalized:
                    if member.isdir():
                        continue
                    raise WorkspaceValidationError(
                        WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL
                    )
                if normalized in seen:
                    raise WorkspaceValidationError(
                        WorkspaceValidationCode.TAR_DUPLICATE_MEMBER
                    )
                seen.add(normalized)
                if member.isreg():
                    summary.total_file_bytes += member.size
                    if summary.total_file_bytes > max_total_file_bytes:
                        raise WorkspaceValidationError(
                            WorkspaceValidationCode.TAR_TOO_LARGE
                        )
    except tarfile.TarError as exc:
        raise WorkspaceValidationError(WorkspaceValidationCode.TAR_MALFORMED) from exc
    return summary


def split_reauthored_tar(
    spool_path: Path, routes: Sequence[tuple[str, BinaryIO]]
) -> None:
    """Re-author a validated spool into one fresh tar stream per route.

    ``routes`` pairs a normalized relative prefix with an output stream; the
    ``""`` route is the main workspace volume and MUST be present. A member is
    written to the longest matching prefix's stream with that prefix stripped
    (its volume mounts at ``/workspace/<prefix>``). A member exactly equal to a
    non-empty prefix is skipped: the mountpoint itself is created by Docker.

    Every emitted header is authored here: normalized name, ``uid=gid=0``,
    empty owner names, ``mtime=0``, mode clamped to ``0o755``/``0o644``, GNU
    format, no pax headers. Content is copied in chunks by :mod:`tarfile`.
    """
    prefixes = [prefix for prefix, _ in routes if prefix]
    if len({prefix for prefix, _ in routes}) != len(routes):
        raise ValueError("routes must carry distinct prefixes")
    if not any(prefix == "" for prefix, _ in routes):
        raise ValueError("routes must include the '' main route")
    ordered = sorted(prefixes, key=len, reverse=True)

    with ExitStack() as stack:
        writers: dict[str, tarfile.TarFile] = {
            prefix: stack.enter_context(
                tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT)
            )
            for prefix, stream in routes
        }
        with tarfile.open(spool_path, mode="r:", errorlevel=2) as archive:
            for member in archive:
                _validate_member(member)
                name = normalize_member_name(member.name)
                if not name:
                    continue
                target_prefix = ""
                out_name = name
                skip = False
                for prefix in ordered:
                    if name == prefix:
                        skip = True
                        break
                    if name.startswith(prefix + "/"):
                        target_prefix = prefix
                        out_name = name[len(prefix) + 1 :]
                        break
                if skip:
                    continue
                info = tarfile.TarInfo(out_name)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                if member.isdir():
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    writers[target_prefix].addfile(info)
                else:
                    info.type = tarfile.REGTYPE
                    info.size = member.size
                    info.mode = 0o755 if member.mode & 0o111 else 0o644
                    writers[target_prefix].addfile(info, archive.extractfile(member))


def reauthor_tar(spool_path: Path, out_stream: BinaryIO) -> None:
    """Re-author a validated spool into a single fresh sidecar-written tar."""
    split_reauthored_tar(spool_path, [("", out_stream)])


@dataclass
class _SpoolEntry:
    """One uploaded workspace: where it spooled and whether it is spent."""

    path: Path
    created_at: float
    consumed: bool = field(default=False)


class WorkspaceStore:
    """Spooled workspace uploads keyed by a permanently burned workspace id.

    The seen-id set mirrors the executor's ``_executions`` replay guard: an id
    that ever registered stays refused forever, while a partial or invalid
    upload registers nothing so the same id can retry. Lifecycle is
    UPLOADED → CONSUMED (claim) or UPLOADED → expired (TTL sweep, id kept).
    """

    def __init__(self, spool_dir: Path) -> None:
        self._spool_dir = Path(spool_dir)
        self._lock = asyncio.Lock()
        self._seen: set[str] = set()
        self._inflight: set[str] = set()
        self._entries: dict[str, _SpoolEntry] = {}

    async def ingest(
        self,
        workspace_id: str,
        stream: AsyncIterable[bytes],
        *,
        max_raw_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES,
        max_members: int = DEFAULT_MAX_TAR_MEMBERS,
    ) -> TarSummary:
        """Spool, cap mid-stream, validate, and only then register the id."""
        validate_workspace_id(workspace_id)
        async with self._lock:
            if workspace_id in self._seen or workspace_id in self._inflight:
                raise WorkspaceValidationError(
                    WorkspaceValidationCode.WORKSPACE_DUPLICATE
                )
            self._inflight.add(workspace_id)
        # The spool filename is SIDE-CAR-generated: client strings never touch
        # the filesystem namespace.
        spool_path = self._spool_dir / f"{uuid.uuid4().hex}.tar"
        try:
            self._spool_dir.mkdir(parents=True, exist_ok=True)
            raw_bytes = 0
            with spool_path.open("wb") as spool:
                async for chunk in stream:
                    raw_bytes += len(chunk)
                    if raw_bytes > max_raw_bytes:
                        raise WorkspaceValidationError(
                            WorkspaceValidationCode.TAR_TOO_LARGE
                        )
                    spool.write(chunk)
            summary = await asyncio.to_thread(
                validate_spooled_tar,
                spool_path,
                max_total_file_bytes=max_raw_bytes,
                max_members=max_members,
            )
        except BaseException:
            spool_path.unlink(missing_ok=True)
            async with self._lock:
                self._inflight.discard(workspace_id)
            raise
        async with self._lock:
            self._inflight.discard(workspace_id)
            self._seen.add(workspace_id)
            self._entries[workspace_id] = _SpoolEntry(
                path=spool_path, created_at=time.monotonic()
            )
        return summary

    async def claim(self, workspace_id: str) -> Path:
        """Atomically flip UPLOADED → CONSUMED and hand over the spool path.

        The caller owns the returned file and deletes it when done.
        """
        async with self._lock:
            if workspace_id not in self._seen:
                raise WorkspaceValidationError(WorkspaceValidationCode.WORKSPACE_UNKNOWN)
            entry = self._entries.get(workspace_id)
            if entry is None or entry.consumed:
                raise WorkspaceValidationError(
                    WorkspaceValidationCode.WORKSPACE_CONSUMED
                )
            entry.consumed = True
            return entry.path

    async def sweep(self, ttl_seconds: float) -> int:
        """Delete unconsumed spools older than the TTL; their ids stay burned."""
        now = time.monotonic()
        removed = 0
        async with self._lock:
            for entry in self._entries.values():
                if not entry.consumed and now - entry.created_at > ttl_seconds:
                    entry.consumed = True
                    entry.path.unlink(missing_ok=True)
                    removed += 1
        return removed

    def startup_gc(self) -> int:
        """Remove spool files a previous process left behind."""
        tracked = {entry.path for entry in self._entries.values()}
        removed = 0
        if self._spool_dir.is_dir():
            for path in self._spool_dir.glob("*.tar"):
                if path not in tracked:
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed


def resolve_max_workspace_bytes() -> int:
    """Byte cap for one staged workspace, env-overridable."""
    return int(os.getenv("ZEROTH_SIDECAR_MAX_WORKSPACE_BYTES") or DEFAULT_MAX_WORKSPACE_BYTES)


def resolve_workspace_ttl_seconds() -> float:
    """TTL for unclaimed workspace spools, env-overridable."""
    return float(
        os.getenv("ZEROTH_SIDECAR_WORKSPACE_TTL_SECONDS") or DEFAULT_WORKSPACE_TTL_SECONDS
    )


def resolve_helper_image() -> str:
    """Helper-container image for volume populate/capture, env-overridable."""
    return os.getenv("ZEROTH_SIDECAR_HELPER_IMAGE") or DEFAULT_HELPER_IMAGE


def resolve_workspace_spool_dir() -> Path:
    """Directory workspace uploads spool into, env-overridable."""
    configured = os.getenv("ZEROTH_SIDECAR_WORKSPACE_SPOOL_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "zeroth-sidecar-workspaces"


__all__ = [
    "DEFAULT_HELPER_IMAGE",
    "DEFAULT_MAX_TAR_MEMBERS",
    "DEFAULT_MAX_WORKSPACE_BYTES",
    "DEFAULT_WORKSPACE_TTL_SECONDS",
    "TarSummary",
    "WorkspaceStore",
    "WorkspaceValidationCode",
    "WorkspaceValidationError",
    "normalize_member_name",
    "reauthor_tar",
    "resolve_helper_image",
    "resolve_max_workspace_bytes",
    "resolve_workspace_spool_dir",
    "resolve_workspace_ttl_seconds",
    "split_reauthored_tar",
    "validate_spooled_tar",
    "validate_workspace_id",
]
