"""Phase A tree policy against real ``git ls-tree`` output over the fixture repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.github.repo_fixtures import (
    RepoFixture,
    git_env,
    make_absolute_symlink_repo,
    make_benign_repo,
    make_dotgit_in_tree_repo,
    make_huge_file_repo,
    make_lfs_pointer_repo,
    make_many_files_repo,
    make_submodule_repo,
    make_symlink_escape_repo,
)
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import CheckoutError, CheckoutFailureCode
from zeroth.integrations.github.tree_policy import (
    TreeEntry,
    enforce_tree_policy,
    is_lfs_pointer,
    lfs_candidate_blobs,
    parse_cat_file_batch,
    parse_ls_tree,
)


def _config(**overrides) -> GitHubAppConfig:
    return GitHubAppConfig(app_id="424242", **overrides)


def _ls_tree(fixture: RepoFixture) -> bytes:
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "-l", "-z", fixture.head_sha],
        cwd=fixture.bare_path,
        env=git_env(),
        capture_output=True,
        check=True,
        timeout=60,
    )
    return completed.stdout


def _refusal_code(fixture: RepoFixture, config: GitHubAppConfig) -> CheckoutFailureCode:
    entries = parse_ls_tree(_ls_tree(fixture))
    with pytest.raises(CheckoutError) as excinfo:
        enforce_tree_policy(entries, config)
    return excinfo.value.code


def test_benign_repo_parses_and_passes(tmp_path: Path) -> None:
    fixture = make_benign_repo(tmp_path)
    entries = parse_ls_tree(_ls_tree(fixture))
    by_path = {entry.path: entry for entry in entries}
    assert set(by_path) == {".zeroth.yaml", "data/config.json", "scripts/train.py"}
    assert by_path["scripts/train.py"].mode == "100755"
    assert by_path[".zeroth.yaml"].mode == "100644"
    assert all(entry.size is not None and entry.size > 0 for entry in entries)
    enforce_tree_policy(entries, _config())  # does not raise


def test_symlink_repos_are_refused(tmp_path: Path) -> None:
    relative = make_symlink_escape_repo(tmp_path / "rel")
    absolute = make_absolute_symlink_repo(tmp_path / "abs")
    assert _refusal_code(relative, _config()) is CheckoutFailureCode.TREE_SYMLINK
    assert _refusal_code(absolute, _config()) is CheckoutFailureCode.TREE_SYMLINK


def test_gitlink_is_refused_as_submodule(tmp_path: Path) -> None:
    fixture = make_submodule_repo(tmp_path)
    entries = parse_ls_tree(_ls_tree(fixture))
    gitlinks = [entry for entry in entries if entry.mode == "160000"]
    assert gitlinks and gitlinks[0].object_type == "commit"
    assert gitlinks[0].size is None  # ls-tree prints '-' for non-blobs
    assert _refusal_code(fixture, _config()) is CheckoutFailureCode.TREE_SUBMODULE


def test_crafted_dotgit_tree_entry_is_refused(tmp_path: Path) -> None:
    fixture = make_dotgit_in_tree_repo(tmp_path)
    assert _refusal_code(fixture, _config()) is CheckoutFailureCode.TREE_DOTGIT


def test_file_size_cap(tmp_path: Path) -> None:
    fixture = make_huge_file_repo(tmp_path, file_bytes=2048)
    code = _refusal_code(fixture, _config(max_file_bytes=1024))
    assert code is CheckoutFailureCode.CAPS_FILE_SIZE


def test_total_size_cap(tmp_path: Path) -> None:
    fixture = make_benign_repo(tmp_path)
    code = _refusal_code(fixture, _config(max_total_bytes=10))
    assert code is CheckoutFailureCode.CAPS_TOTAL_SIZE


def test_file_count_cap(tmp_path: Path) -> None:
    fixture = make_many_files_repo(tmp_path, count=12)
    code = _refusal_code(fixture, _config(max_file_count=10))
    assert code is CheckoutFailureCode.CAPS_FILE_COUNT


def test_lfs_pointer_detection_via_batched_cat_file(tmp_path: Path) -> None:
    fixture = make_lfs_pointer_repo(tmp_path)
    entries = parse_ls_tree(_ls_tree(fixture))
    enforce_tree_policy(entries, _config())  # pointers are not hostile per se
    candidates = lfs_candidate_blobs(entries)
    assert [path for _sha, path in candidates] == ["model.bin"]
    batch_input = b"".join(sha.encode() + b"\n" for sha, _path in candidates)
    completed = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=fixture.bare_path,
        env=git_env(),
        input=batch_input,
        capture_output=True,
        check=True,
        timeout=60,
    )
    contents = parse_cat_file_batch(completed.stdout)
    assert len(contents) == 1
    assert all(is_lfs_pointer(content) for content in contents.values())


def test_benign_small_files_are_candidates_but_not_pointers(tmp_path: Path) -> None:
    fixture = make_benign_repo(tmp_path)
    entries = parse_ls_tree(_ls_tree(fixture))
    candidates = lfs_candidate_blobs(entries)
    assert candidates  # the manifest and config are tiny
    batch_input = b"".join(sha.encode() + b"\n" for sha, _path in candidates)
    completed = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=fixture.bare_path,
        env=git_env(),
        input=batch_input,
        capture_output=True,
        check=True,
        timeout=60,
    )
    contents = parse_cat_file_batch(completed.stdout)
    assert not any(is_lfs_pointer(content) for content in contents.values())


# -- synthetic entries for shapes no builder can produce ----------------------


def _entry(**overrides) -> TreeEntry:
    defaults = dict(mode="100644", object_type="blob", sha="a" * 40, size=10, path="ok.txt")
    defaults.update(overrides)
    return TreeEntry(**defaults)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (_entry(mode="040000", object_type="tree", size=None, path="dir"), None),
        (_entry(mode="777777"), CheckoutFailureCode.TREE_TRAVERSAL),
        (_entry(path=".gitmodules"), CheckoutFailureCode.TREE_SUBMODULE),
        (_entry(path="a/.GIT/config"), CheckoutFailureCode.TREE_DOTGIT),
        (_entry(path="a/git~1/x"), CheckoutFailureCode.TREE_DOTGIT),
        (_entry(path="a/../b"), CheckoutFailureCode.TREE_TRAVERSAL),
        (_entry(path="a//b"), CheckoutFailureCode.TREE_TRAVERSAL),
        (_entry(path="a\\b"), CheckoutFailureCode.TREE_TRAVERSAL),
        (_entry(path="a\x00b"), CheckoutFailureCode.TREE_TRAVERSAL),
        (_entry(path="."), CheckoutFailureCode.TREE_TRAVERSAL),
    ],
)
def test_synthetic_hostile_entries(
    entry: TreeEntry, expected: CheckoutFailureCode | None
) -> None:
    if expected is None:
        enforce_tree_policy([entry], _config())
        return
    with pytest.raises(CheckoutError) as excinfo:
        enforce_tree_policy([entry], _config())
    assert excinfo.value.code is expected


def test_policy_error_messages_never_carry_entry_names(tmp_path: Path) -> None:
    fixture = make_symlink_escape_repo(tmp_path)
    entries = parse_ls_tree(_ls_tree(fixture))
    with pytest.raises(CheckoutError) as excinfo:
        enforce_tree_policy(entries, _config())
    assert "escape" not in str(excinfo.value)
