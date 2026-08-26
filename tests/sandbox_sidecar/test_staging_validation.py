"""Hostile-tar corpus for the sidecar workspace staging validator (ZER-37).

Every rejection must use a fixed message template: member names are
attacker-controlled and must never be echoed into an error string, matching
the image-validator discipline pinned in ``test_executor_argv.py``.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from zeroth.integrations.sandbox.staging import (
    WorkspaceValidationCode,
    WorkspaceValidationError,
    normalize_member_name,
    reauthor_tar,
    split_reauthored_tar,
    validate_spooled_tar,
    validate_workspace_id,
)

CANARY = "CANARY-9f2e7b"

MAX_BYTES = 1_048_576


def _tar_bytes(build) -> bytes:
    """Build an uncompressed tar in memory via a callback receiving the TarFile."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        build(archive)
    return buffer.getvalue()


def _add_file(archive: tarfile.TarFile, name: str, data: bytes = b"x", **attrs) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for key, value in attrs.items():
        setattr(info, key, value)
    archive.addfile(info, io.BytesIO(data))


def _add_special(archive: tarfile.TarFile, name: str, member_type: bytes, **attrs) -> None:
    info = tarfile.TarInfo(name)
    info.type = member_type
    for key, value in attrs.items():
        setattr(info, key, value)
    archive.addfile(info)


def _patch_typeflag(data: bytes, flag: bytes) -> bytes:
    """Rewrite the first header block's typeflag and fix its checksum."""
    block = bytearray(data[:512])
    block[156:157] = flag
    block[148:156] = b" " * 8
    checksum = sum(block)
    block[148:156] = f"{checksum:06o}\0 ".encode()
    return bytes(block) + data[512:]


def _spool(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "spool.tar"
    path.write_bytes(data)
    return path


def _validate(tmp_path: Path, data: bytes, **caps):
    caps.setdefault("max_total_file_bytes", MAX_BYTES)
    return validate_spooled_tar(_spool(tmp_path, data), **caps)


def _rejection_code(tmp_path: Path, data: bytes, **caps) -> WorkspaceValidationError:
    with pytest.raises(WorkspaceValidationError) as excinfo:
        _validate(tmp_path, data, **caps)
    assert CANARY not in str(excinfo.value)
    return excinfo.value


def test_a_plain_workspace_tar_is_accepted(tmp_path: Path) -> None:
    data = _tar_bytes(
        lambda archive: (
            _add_special(archive, "pkg", tarfile.DIRTYPE),
            _add_file(archive, "pkg/main.py", b"print('hi')"),
            _add_file(archive, "README.md", b"# hello"),
        )
    )

    summary = validate_spooled_tar(_spool(tmp_path, data), max_total_file_bytes=MAX_BYTES)

    assert summary.member_count == 3
    assert summary.total_file_bytes == len(b"print('hi')") + len(b"# hello")
    assert summary.raw_bytes == len(data)


@pytest.mark.parametrize(
    ("member_type", "attrs"),
    [
        (tarfile.SYMTYPE, {"linkname": "/etc/passwd"}),
        (tarfile.LNKTYPE, {"linkname": "elsewhere"}),
        (tarfile.CHRTYPE, {"devmajor": 1, "devminor": 3}),
        (tarfile.BLKTYPE, {"devmajor": 8, "devminor": 0}),
        (tarfile.FIFOTYPE, {}),
    ],
)
def test_non_regular_members_are_forbidden(tmp_path: Path, member_type, attrs) -> None:
    data = _tar_bytes(lambda archive: _add_special(archive, f"{CANARY}.bin", member_type, **attrs))

    error = _rejection_code(tmp_path, data)

    assert error.code is WorkspaceValidationCode.TAR_MEMBER_FORBIDDEN


def test_gnu_sparse_members_are_forbidden(tmp_path: Path) -> None:
    plain = _tar_bytes(lambda archive: _add_file(archive, f"{CANARY}.dat", b""))
    data = _patch_typeflag(plain, tarfile.GNUTYPE_SPARSE)

    error = _rejection_code(tmp_path, data)

    assert error.code is WorkspaceValidationCode.TAR_SPARSE_FORBIDDEN


def test_pax_sparse_members_are_forbidden(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(f"{CANARY}.dat")
        info.pax_headers = {"GNU.sparse.name": f"{CANARY}.dat"}
        archive.addfile(info)

    error = _rejection_code(tmp_path, buffer.getvalue())

    assert error.code is WorkspaceValidationCode.TAR_SPARSE_FORBIDDEN


@pytest.mark.parametrize(
    "name",
    [
        f"/{CANARY}.txt",
        f"../{CANARY}.txt",
        f"a/../{CANARY}.txt",
        f"a//{CANARY}.txt",
        f"a\\{CANARY}.txt",
    ],
)
def test_traversal_member_names_are_rejected(tmp_path: Path, name: str) -> None:
    data = _tar_bytes(lambda archive: _add_file(archive, name))

    error = _rejection_code(tmp_path, data)

    assert error.code is WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL


def test_nul_and_backslash_names_are_rejected_by_the_name_validator() -> None:
    # An embedded NUL terminates the on-disk header name when read back, so the
    # corpus exercises the guard directly instead of through a crafted archive.
    for name in (f"a\x00{CANARY}", f"a\\{CANARY}"):
        with pytest.raises(WorkspaceValidationError) as excinfo:
            normalize_member_name(name)
        assert excinfo.value.code is WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL
        assert CANARY not in str(excinfo.value)


def test_current_directory_parts_normalize_away() -> None:
    assert normalize_member_name("./a/b.txt") == "a/b.txt"
    assert normalize_member_name("dir/") == "dir"
    assert normalize_member_name("./") == ""


def test_member_names_longer_than_255_bytes_are_rejected(tmp_path: Path) -> None:
    name = f"{CANARY}/" + "a" * 300
    data = _tar_bytes(lambda archive: _add_file(archive, name))

    error = _rejection_code(tmp_path, data)

    assert error.code is WorkspaceValidationCode.TAR_MEMBER_NAME_TOO_LONG


def test_duplicate_member_names_are_rejected(tmp_path: Path) -> None:
    data = _tar_bytes(
        lambda archive: (
            _add_file(archive, f"{CANARY}.txt"),
            _add_file(archive, f"./{CANARY}.txt"),
        )
    )

    error = _rejection_code(tmp_path, data)

    assert error.code is WorkspaceValidationCode.TAR_DUPLICATE_MEMBER


def test_compressed_streams_are_rejected(tmp_path: Path) -> None:
    plain = _tar_bytes(lambda archive: _add_file(archive, f"{CANARY}.txt"))

    error = _rejection_code(tmp_path, gzip.compress(plain))

    assert error.code is WorkspaceValidationCode.TAR_COMPRESSED


def test_garbage_bytes_are_rejected_as_malformed(tmp_path: Path) -> None:
    error = _rejection_code(tmp_path, b"not a tar stream" * 40)

    assert error.code is WorkspaceValidationCode.TAR_MALFORMED


def test_total_file_bytes_over_the_cap_are_rejected(tmp_path: Path) -> None:
    data = _tar_bytes(
        lambda archive: (
            _add_file(archive, "a.bin", b"x" * 600),
            _add_file(archive, f"{CANARY}.bin", b"y" * 600),
        )
    )

    error = _rejection_code(tmp_path, data, max_total_file_bytes=1000)

    assert error.code is WorkspaceValidationCode.TAR_TOO_LARGE


def test_member_count_over_the_cap_is_rejected(tmp_path: Path) -> None:
    data = _tar_bytes(
        lambda archive: [_add_file(archive, f"{CANARY}-{index}.txt") for index in range(3)]
    )

    error = _rejection_code(tmp_path, data, max_members=2)

    assert error.code is WorkspaceValidationCode.TAR_TOO_MANY_MEMBERS


@pytest.mark.parametrize(
    "workspace_id",
    ["", "-leading-dash", ".hidden", "has/slash", "has:colon", "a" * 65, "søme"],
)
def test_invalid_workspace_ids_are_rejected(workspace_id: str) -> None:
    with pytest.raises(WorkspaceValidationError) as excinfo:
        validate_workspace_id(workspace_id)

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_ID_INVALID
    assert workspace_id not in str(excinfo.value) or workspace_id == ""


@pytest.mark.parametrize("workspace_id", ["ws1", "A.b_c-d", "0" * 64, "9start"])
def test_docker_volume_safe_workspace_ids_are_accepted(workspace_id: str) -> None:
    assert validate_workspace_id(workspace_id) == workspace_id


def test_reauthored_headers_are_normalized(tmp_path: Path) -> None:
    data = _tar_bytes(
        lambda archive: (
            _add_special(
                archive, "pkg", tarfile.DIRTYPE, uid=1234, gid=99, uname="mallory", mode=0o707
            ),
            _add_file(
                archive,
                "pkg/tool.sh",
                b"#!/bin/sh\n",
                uid=1234,
                gid=99,
                uname="mallory",
                gname="crew",
                mtime=1_699_999_999,
                mode=0o750,
            ),
            _add_file(archive, "pkg/data.txt", b"payload", mode=0o600, mtime=1_699_999_999),
        )
    )
    out = io.BytesIO()

    reauthor_tar(_spool(tmp_path, data), out)

    raw = out.getvalue()
    assert raw[257:265] == b"ustar  \0"  # GNU magic authored by the sidecar
    out.seek(0)
    with tarfile.open(fileobj=out, mode="r:") as archive:
        members = {member.name: member for member in archive.getmembers()}
    assert set(members) == {"pkg", "pkg/tool.sh", "pkg/data.txt"}
    for member in members.values():
        assert (member.uid, member.gid) == (0, 0)
        assert (member.uname, member.gname) == ("", "")
        assert member.mtime == 0
        assert not member.pax_headers
    assert members["pkg"].mode == 0o755
    assert members["pkg/tool.sh"].mode == 0o755  # exec bit -> 755
    assert members["pkg/data.txt"].mode == 0o644  # no exec bit -> 644


def test_reauthoring_preserves_file_content(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 300
    data = _tar_bytes(lambda archive: _add_file(archive, "blob.bin", payload))
    out = io.BytesIO()

    reauthor_tar(_spool(tmp_path, data), out)

    out.seek(0)
    with tarfile.open(fileobj=out, mode="r:") as archive:
        extracted = archive.extractfile("blob.bin")
        assert extracted is not None
        assert extracted.read() == payload


def test_split_routes_members_by_read_only_prefix(tmp_path: Path) -> None:
    data = _tar_bytes(
        lambda archive: (
            _add_file(archive, "a.txt", b"main"),
            _add_special(archive, "cfg", tarfile.DIRTYPE),
            _add_file(archive, "cfg/x.conf", b"ro-root"),
            _add_file(archive, "cfg/sub/y", b"ro-nested"),
            _add_file(archive, "data/z", b"main-nested"),
        )
    )
    main_out, ro_out = io.BytesIO(), io.BytesIO()

    split_reauthored_tar(_spool(tmp_path, data), [("", main_out), ("cfg", ro_out)])

    main_out.seek(0)
    ro_out.seek(0)
    with tarfile.open(fileobj=main_out, mode="r:") as archive:
        assert [member.name for member in archive.getmembers()] == ["a.txt", "data/z"]
    with tarfile.open(fileobj=ro_out, mode="r:") as archive:
        # The mountpoint prefix is stripped: the volume mounts at /workspace/cfg.
        assert [member.name for member in archive.getmembers()] == ["x.conf", "sub/y"]
        extracted = archive.extractfile("x.conf")
        assert extracted is not None
        assert extracted.read() == b"ro-root"
