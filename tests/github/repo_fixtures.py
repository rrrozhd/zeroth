"""Bare git repository builders for the ZER-37 checkout substrate.

Every builder materializes a working repo in a scratch directory, commits with
a scrubbed environment (no system or user config, fixed identity, fixed
dates), and publishes a BARE clone -- the shape ``git http-backend`` serves.
``uploadpack.allowReachableSHA1InWant`` is enabled on each bare repo so a
client can fetch a pinned commit by SHA; the benign builder can disable it to
reproduce the fetch-by-unadvertised-SHA refusal (the TOCTOU repro).

The hostile builders produce trees a checkout guard must refuse: symlinks that
escape the tree, a gitlink (mode 160000), and a crafted in-tree ``.git`` entry
built from raw objects so no working tree ever contains it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_FIXED_DATE = "2026-01-02T03:04:05 +0000"

_LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    f"oid sha256:{hashlib.sha256(b'weights').hexdigest()}\n"
    "size 12345\n"
)

_TRAIN_SCRIPT = """\
#!/usr/bin/env python3
import json
import sys

payload = json.load(sys.stdin)
json.dump({"ok": True, "ready": "READY", "echo": payload}, sys.stdout)
"""

_MANIFEST = """\
schema_version: 1
scripts:
  train:
    entry: "scripts/train.py"
    runtime: "python3"
    working_directory: "."
    input: {mode: "json_stdin"}
    output: {mode: "json_stdout"}
    resources: {timeout_seconds: 60}
    network: {access: "none"}
    smoke:
      files_exist: ["data/config.json"]
      exit_code: 0
"""

_MULTI_SCRIPT_MANIFEST = """\
schema_version: 1
scripts:
  train:
    entry: "scripts/train.py"
    runtime: "python3"
    working_directory: "."
    input: {mode: "json_stdin"}
    output: {mode: "json_stdout"}
    resources: {timeout_seconds: 60}
    network: {access: "none"}
    smoke:
      files_exist: ["data/config.json"]
      exit_code: 0
  evaluate:
    entry: "scripts/evaluate.py"
    runtime: "python3"
    working_directory: "."
    input: {mode: "json_stdin"}
    output: {mode: "json_stdout"}
    resources: {timeout_seconds: 30}
    network: {access: "none"}
    smoke:
      files_exist: ["data/config.json"]
      exit_code: 0
"""


@dataclass(frozen=True)
class RepoFixture:
    bare_path: Path
    head_sha: str
    extra: dict[str, str] = field(default_factory=dict)


def git_env() -> dict[str, str]:
    """A scrubbed environment: no system/user config, fixed identity and dates."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": _FIXED_DATE,
        "GIT_COMMITTER_DATE": _FIXED_DATE,
    }


def _git(args: list[str], cwd: Path, *, input_bytes: bytes | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=git_env(),
        input=input_bytes,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.decode("utf-8", "replace").strip()


def _commit(workdir: Path, message: str, *, add_all: bool = True) -> str:
    if add_all:
        _git(["add", "-A"], workdir)
    _git(
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
        workdir,
    )
    return _git(["rev-parse", "HEAD"], workdir)


def _init_work(base_dir: Path, name: str) -> Path:
    workdir = base_dir / "_work" / name
    workdir.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], workdir)
    return workdir


def _publish_bare(workdir: Path, bare_path: Path, *, allow_reachable_sha1: bool = True) -> Path:
    _git(["clone", "-q", "--bare", str(workdir), str(bare_path)], workdir.parent)
    _set_allow_reachable(bare_path, allow_reachable_sha1)
    return bare_path


def _set_allow_reachable(bare_path: Path, enabled: bool) -> None:
    value = "true" if enabled else "false"
    _git(["config", "uploadpack.allowReachableSHA1InWant", value], bare_path)


def make_benign_repo(base_dir: Path, *, allow_reachable_sha1: bool = True) -> RepoFixture:
    """Two commits: data first, then the manifest and executable train script.

    ``allow_reachable_sha1=False`` sets ``uploadpack.allowReachableSHA1InWant``
    off on the bare repo. Note this is only observable to a **protocol v0**
    client: git's protocol v2 ``fetch`` does not honor that setting, so a v2
    client (the shape the main fetch tests and the task's mandate use) still
    fetches the unadvertised parent SHA successfully. To simulate "this pinned
    SHA is no longer fetchable" against a v2 client, fetch an *unknown* SHA
    (v2 fails those) or use ``GitSmartHTTPServer.fail_all_with``.
    """
    workdir = _init_work(base_dir, "benign")
    (workdir / "data").mkdir()
    (workdir / "data" / "config.json").write_text('{"dataset": "demo"}\n')
    parent_sha = _commit(workdir, "add dataset config")
    (workdir / ".zeroth.yaml").write_text(_MANIFEST)
    scripts = workdir / "scripts"
    scripts.mkdir()
    train = scripts / "train.py"
    train.write_text(_TRAIN_SCRIPT)
    train.chmod(0o755)
    head_sha = _commit(workdir, "add manifest and train script")
    bare = _publish_bare(
        workdir, base_dir / "benign.git", allow_reachable_sha1=allow_reachable_sha1
    )
    return RepoFixture(bare, head_sha, {"parent_sha": parent_sha})


def make_symlink_escape_repo(base_dir: Path) -> RepoFixture:
    """A relative symlink pointing outside any checkout root."""
    workdir = _init_work(base_dir, "symlink-escape")
    (workdir / "README.md").write_text("hostile fixture\n")
    os.symlink("../../etc/passwd", workdir / "escape")
    head_sha = _commit(workdir, "add escaping symlink")
    bare = _publish_bare(workdir, base_dir / "symlink-escape.git")
    return RepoFixture(bare, head_sha, {"symlink_name": "escape"})


def make_absolute_symlink_repo(base_dir: Path) -> RepoFixture:
    """An absolute symlink to a host file."""
    workdir = _init_work(base_dir, "symlink-absolute")
    (workdir / "README.md").write_text("hostile fixture\n")
    os.symlink("/etc/passwd", workdir / "abs-escape")
    head_sha = _commit(workdir, "add absolute symlink")
    bare = _publish_bare(workdir, base_dir / "symlink-absolute.git")
    return RepoFixture(bare, head_sha, {"symlink_name": "abs-escape"})


def make_submodule_repo(base_dir: Path) -> RepoFixture:
    """A crafted mode-160000 gitlink entry (no real submodule objects needed)."""
    workdir = _init_work(base_dir, "submodule")
    (workdir / "README.md").write_text("hostile fixture\n")
    _git(["add", "README.md"], workdir)
    gitlink_sha = hashlib.sha1(b"submodule", usedforsecurity=False).hexdigest()
    _git(
        ["update-index", "--add", "--cacheinfo", f"160000,{gitlink_sha},vendor/dep"],
        workdir,
    )
    # No ``add -A`` here: the gitlink's path does not exist on disk, so a
    # tree-wide re-add would stage its removal.
    head_sha = _commit(workdir, "add gitlink", add_all=False)
    bare = _publish_bare(workdir, base_dir / "submodule.git")
    return RepoFixture(bare, head_sha, {"gitlink_sha": gitlink_sha})


def make_dotgit_in_tree_repo(base_dir: Path) -> RepoFixture:
    """A committed tree carrying a ``.git`` FILE entry, built from raw objects.

    Git refuses to stage a ``.git`` path from a working tree, so the tree
    object is written directly (``hash-object -t tree`` over raw entry bytes)
    and committed with ``commit-tree`` inside a bare repo.
    """
    bare = base_dir / "dotgit-in-tree.git"
    bare.mkdir(parents=True)
    _git(["init", "-q", "--bare", "-b", "main"], bare)
    dotgit_blob = _git(
        ["hash-object", "-w", "--stdin"], bare, input_bytes=b"gitdir: elsewhere\n"
    )
    readme_blob = _git(
        ["hash-object", "-w", "--stdin"], bare, input_bytes=b"hostile fixture\n"
    )
    tree_bytes = b"".join(
        b"100644 " + name + b"\x00" + bytes.fromhex(blob)
        for name, blob in ((b".git", dotgit_blob), (b"README.md", readme_blob))
    )
    # --literally: git's fsck refuses to write a tree containing a ``.git``
    # entry (``hasDotgit``); crafting the hostile object is the whole point.
    tree_sha = _git(
        ["hash-object", "-t", "tree", "-w", "--literally", "--stdin"],
        bare,
        input_bytes=tree_bytes,
    )
    head_sha = _git(["commit-tree", tree_sha, "-m", "tree with .git entry"], bare)
    _git(["update-ref", "refs/heads/main", head_sha], bare)
    _set_allow_reachable(bare, True)
    return RepoFixture(bare, head_sha, {"tree_sha": tree_sha})


def make_huge_file_repo(base_dir: Path, *, file_bytes: int) -> RepoFixture:
    """One file of exactly ``file_bytes`` bytes."""
    workdir = _init_work(base_dir, "huge")
    (workdir / "big.bin").write_bytes(b"\x00" * file_bytes)
    head_sha = _commit(workdir, "add oversized file")
    bare = _publish_bare(workdir, base_dir / "huge.git")
    return RepoFixture(bare, head_sha, {"file_bytes": str(file_bytes)})


def make_many_files_repo(base_dir: Path, *, count: int) -> RepoFixture:
    """``count`` small files."""
    workdir = _init_work(base_dir, "many")
    files = workdir / "files"
    files.mkdir()
    for index in range(count):
        (files / f"f_{index:05d}.txt").write_text(f"{index}\n")
    head_sha = _commit(workdir, "add many files")
    bare = _publish_bare(workdir, base_dir / "many.git")
    return RepoFixture(bare, head_sha, {"count": str(count)})


def make_lfs_pointer_repo(base_dir: Path) -> RepoFixture:
    """A file whose content is a syntactically valid git-lfs pointer."""
    workdir = _init_work(base_dir, "lfs-pointer")
    (workdir / "model.bin").write_text(_LFS_POINTER)
    head_sha = _commit(workdir, "add lfs pointer")
    bare = _publish_bare(workdir, base_dir / "lfs-pointer.git")
    return RepoFixture(bare, head_sha, {"pointer_name": "model.bin"})


def make_multi_script_manifest_repo(base_dir: Path) -> RepoFixture:
    """A manifest declaring two scripts, both present and executable."""
    workdir = _init_work(base_dir, "multi-script")
    (workdir / "data").mkdir()
    (workdir / "data" / "config.json").write_text('{"dataset": "demo"}\n')
    (workdir / ".zeroth.yaml").write_text(_MULTI_SCRIPT_MANIFEST)
    scripts = workdir / "scripts"
    scripts.mkdir()
    for script_name in ("train.py", "evaluate.py"):
        script = scripts / script_name
        script.write_text(_TRAIN_SCRIPT)
        script.chmod(0o755)
    head_sha = _commit(workdir, "add multi-script manifest")
    bare = _publish_bare(workdir, base_dir / "multi-script.git")
    return RepoFixture(bare, head_sha)


def make_no_config_repo(base_dir: Path) -> RepoFixture:
    """A repo with no ``.zeroth.yaml`` at all."""
    workdir = _init_work(base_dir, "no-config")
    (workdir / "README.md").write_text("no manifest here\n")
    head_sha = _commit(workdir, "add readme")
    bare = _publish_bare(workdir, base_dir / "no-config.git")
    return RepoFixture(bare, head_sha)
