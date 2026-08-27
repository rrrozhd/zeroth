"""ZER-37: directory confinement for a manifest's working directory.

``confine_path`` deliberately refuses the root itself because its callers name
*files*. A working directory of ``"."`` legitimately names the checkout root,
so ``confine_directory`` permits equality with the root while keeping the same
resolve-before-check discipline against traversal and symlink escapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zeroth.platform.primitives import (
    DestinationNotADirectoryError,
    OutboundDestinationError,
    confine_directory,
)

CONTEXT = "repository manifest working_directory"


@pytest.mark.parametrize("path", [".", "./"])
def test_the_root_itself_is_permitted(path: str, tmp_path: Path) -> None:
    resolved = confine_directory(path, root=tmp_path, context=CONTEXT)

    assert resolved == tmp_path.resolve()


def test_a_subdirectory_is_permitted(tmp_path: Path) -> None:
    (tmp_path / "nested" / "deeper").mkdir(parents=True)

    resolved = confine_directory("nested/deeper", root=tmp_path, context=CONTEXT)

    assert resolved == (tmp_path / "nested" / "deeper").resolve()


def test_traversal_out_of_the_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OutboundDestinationError):
        confine_directory("../escaped", root=tmp_path, context=CONTEXT)


def test_an_absolute_path_outside_the_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OutboundDestinationError):
        confine_directory("/etc", root=tmp_path, context=CONTEXT)


def test_a_symlinked_directory_escaping_the_root_is_refused(tmp_path: Path) -> None:
    """Containment is checked on the resolved path, so the symlink cannot escape."""
    root = tmp_path / "checkout"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)

    with pytest.raises(OutboundDestinationError) as excinfo:
        confine_directory("link", root=root, context=CONTEXT)

    # An escape is a containment refusal, not a directory-shape refusal.
    assert not isinstance(excinfo.value, DestinationNotADirectoryError)


def test_a_symlinked_directory_inside_the_root_is_permitted(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target)

    resolved = confine_directory("alias", root=tmp_path, context=CONTEXT)

    assert resolved == target.resolve()


def test_a_regular_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("not a directory")

    with pytest.raises(DestinationNotADirectoryError):
        confine_directory("file.txt", root=tmp_path, context=CONTEXT)


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DestinationNotADirectoryError):
        confine_directory("does-not-exist", root=tmp_path, context=CONTEXT)


@pytest.mark.parametrize("path", ["", "   "])
def test_an_empty_path_is_refused(path: str, tmp_path: Path) -> None:
    with pytest.raises(OutboundDestinationError):
        confine_directory(path, root=tmp_path, context=CONTEXT)


def test_error_message_names_the_context(tmp_path: Path) -> None:
    with pytest.raises(OutboundDestinationError, match="working_directory"):
        confine_directory("../escaped", root=tmp_path, context=CONTEXT)
