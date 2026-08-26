"""Phase A object-level tree policy, enforced before anything touches a disk.

``git ls-tree -r -l -z`` output over the fetched commit is parsed and every
entry is judged on mode, path shape, and size caps -- so symlinks, gitlinks,
in-tree ``.git`` entries, traversal-shaped names, and cap violations are
refused while the tree is still only objects in a bare cache, before any
working-tree materialization. LFS pointer detection is a follow-up pass: the
pointer signature lives in blob *content*, which ``ls-tree`` cannot see, so the
policy hands back the small-blob candidates for a batched ``git cat-file``
check.
"""

from __future__ import annotations

from dataclasses import dataclass

from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import CheckoutError, CheckoutFailureCode

_MODE_SYMLINK = "120000"
_MODE_GITLINK = "160000"
_FILE_MODES = frozenset({"100644", "100755"})
_ALLOWED_MODES = frozenset({"100644", "100755", "120000", "160000", "040000"})
_HOSTILE_COMPONENTS = frozenset({"", ".", ".."})
_DOTGIT_COMPONENTS = frozenset({".git", "git~1"})

_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
_LFS_CANDIDATE_MAX_BYTES = 400


@dataclass(frozen=True)
class TreeEntry:
    """One ``ls-tree`` record: mode, object type, sha, blob size, and path."""

    mode: str
    object_type: str
    sha: str
    size: int | None
    path: str


def parse_ls_tree(output: bytes) -> list[TreeEntry]:
    """Parse ``git ls-tree -r -l -z`` output into typed entries.

    Each NUL-terminated record is ``<mode> SP <type> SP <sha> SP+ <size> TAB
    <path>``; the size column is space-padded and reads ``-`` for non-blobs.
    Paths are decoded with ``surrogateescape`` so hostile non-UTF-8 names
    survive into the policy checks instead of crashing the parser.
    """
    entries: list[TreeEntry] = []
    for record in output.split(b"\x00"):
        if not record:
            continue
        head, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR, "malformed ls-tree record"
            )
        fields = head.split()
        if len(fields) != 4:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR, "malformed ls-tree record"
            )
        mode, object_type, sha, raw_size = (field.decode("ascii") for field in fields)
        entries.append(
            TreeEntry(
                mode=mode,
                object_type=object_type,
                sha=sha,
                size=int(raw_size) if raw_size.isdigit() else None,
                path=raw_path.decode("utf-8", "surrogateescape"),
            )
        )
    return entries


def enforce_tree_policy(entries: list[TreeEntry], config: GitHubAppConfig) -> None:
    """Refuse hostile shapes and cap violations; return None when clean.

    Raises:
        CheckoutError: With ``tree_symlink``, ``tree_submodule``,
            ``tree_dotgit``, ``tree_traversal``, ``caps_file_size``,
            ``caps_total_size``, or ``caps_file_count``. Messages are fixed
            templates -- entry names are attacker-chosen and never appear.
    """
    file_count = 0
    total_bytes = 0
    for entry in entries:
        if entry.mode not in _ALLOWED_MODES:
            raise _refusal(CheckoutFailureCode.TREE_TRAVERSAL, "unknown object mode")
        if entry.mode == _MODE_SYMLINK:
            raise _refusal(CheckoutFailureCode.TREE_SYMLINK, "symlink entry")
        if entry.mode == _MODE_GITLINK:
            raise _refusal(CheckoutFailureCode.TREE_SUBMODULE, "gitlink entry")
        _validate_path(entry.path)
        if entry.path == ".gitmodules":
            raise _refusal(CheckoutFailureCode.TREE_SUBMODULE, "submodule declaration")
        if entry.mode in _FILE_MODES:
            file_count += 1
            if file_count > config.max_file_count:
                raise _refusal(CheckoutFailureCode.CAPS_FILE_COUNT, "too many files")
            size = entry.size or 0
            if size > config.max_file_bytes:
                raise _refusal(CheckoutFailureCode.CAPS_FILE_SIZE, "file exceeds size cap")
            total_bytes += size
            if total_bytes > config.max_total_bytes:
                raise _refusal(
                    CheckoutFailureCode.CAPS_TOTAL_SIZE, "tree exceeds total size cap"
                )


def _refusal(code: CheckoutFailureCode, what: str) -> CheckoutError:
    """Build a template refusal that names the shape, never the entry."""
    return CheckoutError(code, f"tree policy refused the checkout ({what})")


def _validate_path(path: str) -> None:
    """Refuse ``.git`` components and traversal-shaped names."""
    if "\x00" in path or "\\" in path:
        raise _refusal(CheckoutFailureCode.TREE_TRAVERSAL, "hostile path byte")
    for component in path.split("/"):
        if component.lower() in _DOTGIT_COMPONENTS:
            raise _refusal(CheckoutFailureCode.TREE_DOTGIT, "in-tree .git component")
        if component in _HOSTILE_COMPONENTS:
            raise _refusal(CheckoutFailureCode.TREE_TRAVERSAL, "traversal path component")


def lfs_candidate_blobs(entries: list[TreeEntry]) -> list[tuple[str, str]]:
    """Return ``(sha, path)`` of blobs small enough to be LFS pointers.

    An LFS pointer file is a tiny text stanza, so only blobs under the
    candidate threshold need a content check.
    """
    return [
        (entry.sha, entry.path)
        for entry in entries
        if entry.mode in _FILE_MODES
        and entry.size is not None
        and entry.size < _LFS_CANDIDATE_MAX_BYTES
    ]


def is_lfs_pointer(content: bytes) -> bool:
    """True when blob content starts with the git-lfs pointer signature."""
    return content.startswith(_LFS_POINTER_PREFIX)


def parse_cat_file_batch(output: bytes) -> dict[str, bytes]:
    """Parse ``git cat-file --batch`` output into ``{sha: content}``.

    Missing objects (``<sha> missing``) are skipped -- the caller already knows
    which shas it asked about.
    """
    contents: dict[str, bytes] = {}
    cursor = 0
    while cursor < len(output):
        newline = output.find(b"\n", cursor)
        if newline == -1:
            break
        header = output[cursor:newline].decode("ascii", "replace").split()
        cursor = newline + 1
        if len(header) == 2 and header[1] == "missing":
            continue
        if len(header) != 3:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR, "malformed cat-file batch header"
            )
        sha, _object_type, raw_size = header
        size = int(raw_size)
        contents[sha] = output[cursor : cursor + size]
        cursor += size + 1  # skip the trailing newline after the content
    return contents


__all__ = [
    "TreeEntry",
    "enforce_tree_policy",
    "is_lfs_pointer",
    "lfs_candidate_blobs",
    "parse_cat_file_batch",
    "parse_ls_tree",
]
