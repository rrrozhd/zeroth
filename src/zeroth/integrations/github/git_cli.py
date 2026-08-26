"""The single subprocess seam for every git invocation the integration makes.

All process spawning, environment scrubbing, credential injection, timeout
enforcement, and stderr redaction happen here and nowhere else. The token
never appears in argv -- it rides in ``GIT_CONFIG_*`` environment entries as an
``http.<base>.extraheader`` -- and stderr is passed through the lease's
redactor before it is stored on any result or exception. Processes start in
their own session so a timeout or cancellation can kill the whole process
group, including any helpers git spawned.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import CheckoutError, CheckoutFailureCode
from zeroth.integrations.github.token_broker import CredentialLease

_STDERR_LIMIT_BYTES = 65536


@dataclass(frozen=True)
class CompletedGit:
    """Outcome of one git invocation; stderr is already redacted text."""

    returncode: int
    stdout: bytes
    stderr_redacted: str


class GitInvocation:
    """Run git subprocesses with group-kill timeout and redacted stderr."""

    async def run_git(
        self,
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
        lease: CredentialLease | None = None,
        input_bytes: bytes | None = None,
    ) -> CompletedGit:
        """Run ``git *args`` and capture its output.

        Non-zero exits do NOT raise -- command semantics belong to the caller.
        Timeouts kill the process group and raise ``fetch_timeout``;
        cancellation kills the group, reaps, and re-raises so asyncio
        cancellation semantics stay intact.

        Raises:
            CheckoutError: With ``fetch_timeout`` when the deadline passes.
            asyncio.CancelledError: When the surrounding task is cancelled.
        """
        stdin = asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            env=env,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_bytes), timeout=timeout_seconds
            )
        except TimeoutError:
            _kill_process_group(process)
            await process.wait()
            raise CheckoutError(
                CheckoutFailureCode.FETCH_TIMEOUT,
                f"git command exceeded {timeout_seconds}s and was killed",
            ) from None
        except asyncio.CancelledError:
            _kill_process_group(process)
            await process.wait()
            raise
        text = stderr[:_STDERR_LIMIT_BYTES].decode("utf-8", "replace")
        if lease is not None:
            text = lease.redact(text)
        return CompletedGit(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr_redacted=text,
        )


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole session the child leads; fall back to killing the child."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def build_scrubbed_env(
    config: GitHubAppConfig,
    lease: CredentialLease | None,
    scratch_home: Path,
) -> dict[str, str]:
    """Build the exact environment a git child is allowed to see.

    Nothing ambient survives except ``PATH``. ``HOME`` points into the scratch
    directory, system/global config are disabled, prompts and askpass are dead
    ends, LFS smudge is off, and the transport protocol allowlist admits only
    https (plus http when ``config.allow_http_git`` permits loopback tests).
    Hardening config (protocol v2, fsck on transfer, no symlink checkout, dead
    hooks path, low-speed cutoffs) is injected via ``GIT_CONFIG_COUNT``
    entries; when a lease is present, the credential rides the same channel as
    an ``http.<git_base_url>/.extraheader`` -- the token never appears in argv.
    """
    hooks_dir = scratch_home / "no-hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, str]] = []
    if lease is not None:
        entries.append(
            (
                f"http.{config.git_base_url}/.extraheader",
                f"Authorization: {lease.basic_auth_header()}",
            )
        )
    entries.extend(
        [
            ("protocol.version", "2"),
            ("core.symlinks", "false"),
            ("fetch.fsckObjects", "true"),
            ("transfer.fsckObjects", "true"),
            ("core.hooksPath", str(hooks_dir)),
            ("http.lowSpeedLimit", "1024"),
            ("http.lowSpeedTime", "60"),
        ]
    )
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(scratch_home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_ALLOW_PROTOCOL": "http:https" if config.allow_http_git else "https",
        "GIT_CONFIG_COUNT": str(len(entries)),
    }
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


# -- command builders ----------------------------------------------------------


def init_bare_cache() -> list[str]:
    """Initialize the current directory as a bare object cache."""
    return ["init", "--bare", "--quiet"]


def add_origin(url: str) -> list[str]:
    """Point the cache's ``origin`` at the repository URL."""
    return ["remote", "add", "origin", url]


def cat_file_exists(sha: str) -> list[str]:
    """Probe whether the pinned commit already exists in the cache."""
    return ["cat-file", "-e", f"{sha}^{{commit}}"]


def fetch_by_sha(sha: str) -> list[str]:
    """Fetch exactly the pinned commit, shallow, no tags, no submodules."""
    return [
        "fetch",
        "--depth",
        "1",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "origin",
        sha,
    ]


def ls_remote_heads() -> list[str]:
    """Cheap remote-health probe: list origin's branch heads."""
    return ["ls-remote", "--heads", "origin"]


def ls_tree(sha: str) -> list[str]:
    """List the commit's full tree with modes and blob sizes, NUL-delimited."""
    return ["ls-tree", "-r", "-l", "-z", sha]


def cat_file_batch() -> list[str]:
    """Stream blob contents for the sha list fed on stdin."""
    return ["cat-file", "--batch"]


def rev_parse_tree(sha: str) -> list[str]:
    """Resolve the commit's tree object id."""
    return ["rev-parse", f"{sha}^{{tree}}"]


def checkout_into(git_dir: Path, work_tree: Path, sha: str) -> list[str]:
    """Materialize the pinned commit into ``work_tree`` from the bare cache.

    Run with ``cwd=work_tree`` and ``GIT_INDEX_FILE`` pointing into scratch so
    the bare cache is never mutated and the pathspec resolves inside the
    destination.
    """
    return [
        "--git-dir",
        str(git_dir),
        "--work-tree",
        str(work_tree),
        "-c",
        "core.bare=false",
        "checkout",
        "-f",
        "--no-recurse-submodules",
        sha,
        "--",
        ".",
    ]


__all__ = [
    "CompletedGit",
    "GitInvocation",
    "add_origin",
    "build_scrubbed_env",
    "cat_file_batch",
    "cat_file_exists",
    "checkout_into",
    "fetch_by_sha",
    "init_bare_cache",
    "ls_remote_heads",
    "ls_tree",
    "rev_parse_tree",
]
