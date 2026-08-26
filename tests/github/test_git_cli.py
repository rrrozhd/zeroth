"""GitInvocation + build_scrubbed_env: scrubbing, injection, redaction, timeouts."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

import pytest

from tests.github.fake_github import FakeGitHubAPI
from zeroth.integrations.github import git_cli
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.git_cli import GitInvocation, build_scrubbed_env
from zeroth.integrations.github.models import CheckoutError, CheckoutFailureCode
from zeroth.integrations.github.token_broker import CredentialLease, InstallationTokenBroker
from zeroth.platform.secrets.provider import EnvSecretProvider

BASE_URL = "http://api.github.test"
REDACTION_MARKER = "[REDACTED:github-installation-token]"


async def _lease(config: GitHubAppConfig) -> tuple[GitHubAppClient, CredentialLease]:
    """Mint a real lease through the fake API so redaction history is populated."""
    api = FakeGitHubAPI(app_id=config.app_id)
    api.add_installation(1, "acme")
    api.add_repo(1, "acme/widgets", private=True)
    issuer = AppJwtIssuer(
        config, EnvSecretProvider({"GITHUB_APP_PRIVATE_KEY": api.private_key_pem})
    )
    client = GitHubAppClient(config, issuer, transport=api.transport())
    broker = InstallationTokenBroker(client)
    return client, await broker.lease(1, "widgets")


def test_scrubbed_env_contents_exactly_without_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-secret")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/evil-askpass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    config = GitHubAppConfig(app_id="424242")
    scratch = tmp_path / "scratch"
    env = build_scrubbed_env(config, None, scratch)
    hooks = scratch / "no-hooks"
    expected = {
        "PATH": os.environ["PATH"],
        "HOME": str(scratch),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_CONFIG_COUNT": "7",
        "GIT_CONFIG_KEY_0": "protocol.version",
        "GIT_CONFIG_VALUE_0": "2",
        "GIT_CONFIG_KEY_1": "core.symlinks",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "fetch.fsckObjects",
        "GIT_CONFIG_VALUE_2": "true",
        "GIT_CONFIG_KEY_3": "transfer.fsckObjects",
        "GIT_CONFIG_VALUE_3": "true",
        "GIT_CONFIG_KEY_4": "core.hooksPath",
        "GIT_CONFIG_VALUE_4": str(hooks),
        "GIT_CONFIG_KEY_5": "http.lowSpeedLimit",
        "GIT_CONFIG_VALUE_5": "1024",
        "GIT_CONFIG_KEY_6": "http.lowSpeedTime",
        "GIT_CONFIG_VALUE_6": "60",
    }
    assert env == expected  # nothing ambient leaks through, nothing is missing
    assert hooks.is_dir()


async def test_scrubbed_env_with_lease_injects_extraheader_never_argv(
    tmp_path: Path,
) -> None:
    config = GitHubAppConfig(
        app_id="424242", api_base_url=BASE_URL, allow_http_git=True
    )
    client, lease = await _lease(config)
    async with client:
        env = build_scrubbed_env(config, lease, tmp_path / "scratch")
    assert env["GIT_ALLOW_PROTOCOL"] == "http:https"
    assert env["GIT_CONFIG_COUNT"] == "8"
    assert env["GIT_CONFIG_KEY_0"] == f"http.{config.git_base_url}/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: {lease.basic_auth_header()}"
    # The credential rides ONLY in the env-config channel.
    assert lease.reveal() not in " ".join(git_cli.fetch_by_sha("a" * 40))


async def test_real_git_sees_injected_config_and_results_are_redacted(
    tmp_path: Path,
) -> None:
    config = GitHubAppConfig(app_id="424242", api_base_url=BASE_URL)
    client, lease = await _lease(config)
    runner = GitInvocation()
    env = build_scrubbed_env(config, lease, tmp_path / "scratch")
    async with client:
        listed = await runner.run_git(
            ["config", "-l"], cwd=tmp_path, env=env, timeout_seconds=30, lease=lease
        )
        alias = (
            f"!git config --get http.{config.git_base_url}/.extraheader 1>&2; exit 3"
        )
        leaked = await runner.run_git(
            ["-c", f"alias.leak={alias}", "leak"],
            cwd=tmp_path,
            env=env,
            timeout_seconds=30,
            lease=lease,
        )
    stdout = listed.stdout.decode()
    assert listed.returncode == 0
    assert "protocol.version=2" in stdout  # GIT_CONFIG_COUNT injection is live
    header_line = (
        f"http.{config.git_base_url}/.extraheader="
        f"Authorization: {lease.basic_auth_header()}"
    )
    assert header_line in stdout  # raw stdout is bytes for parsing, unredacted
    assert lease.basic_auth_header() not in lease.redact(stdout)

    # The alias echoed the credential to stderr and failed; the stored stderr
    # is redacted before anyone can see it.
    assert leaked.returncode == 3
    assert "Authorization:" in leaked.stderr_redacted
    assert lease.reveal() not in leaked.stderr_redacted
    assert lease.basic_auth_header() not in leaked.stderr_redacted
    assert REDACTION_MARKER in leaked.stderr_redacted


async def test_ambient_credentials_never_reach_the_child(tmp_path: Path) -> None:
    canary = "ambient-canary-value"
    os.environ["GITHUB_TOKEN"] = canary
    try:
        config = GitHubAppConfig(app_id="424242")
        env = build_scrubbed_env(config, None, tmp_path / "scratch")
        runner = GitInvocation()
        # `git var -l` dumps config+env-derived values; a scrubbed child knows
        # nothing about the ambient token, and the env dict itself has no room
        # for it (exact-contents test above), so assert both layers.
        completed = await runner.run_git(
            ["config", "-l"], cwd=tmp_path, env=env, timeout_seconds=30
        )
        assert canary not in completed.stdout.decode()
        assert "GITHUB_TOKEN" not in env
    finally:
        os.environ.pop("GITHUB_TOKEN", None)


def _tarpit() -> tuple[socket.socket, int]:
    """A listening socket that completes TCP handshakes but never answers HTTP."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


async def _hung_fetch_setup(tmp_path: Path, port: int) -> tuple[GitInvocation, Path, dict]:
    config = GitHubAppConfig(
        app_id="424242",
        git_base_url=f"http://127.0.0.1:{port}",
        allow_http_git=True,
    )
    scratch = tmp_path / "scratch"
    env = build_scrubbed_env(config, None, scratch)
    cache = tmp_path / "cache.git"
    cache.mkdir()
    runner = GitInvocation()
    initialized = await runner.run_git(
        git_cli.init_bare_cache(), cwd=cache, env=env, timeout_seconds=30
    )
    assert initialized.returncode == 0
    remote = await runner.run_git(
        git_cli.add_origin(f"http://127.0.0.1:{port}/acme/widgets.git"),
        cwd=cache,
        env=env,
        timeout_seconds=30,
    )
    assert remote.returncode == 0
    return runner, cache, env


async def test_timeout_kills_the_hung_fetch(tmp_path: Path) -> None:
    sock, port = _tarpit()
    try:
        runner, cache, env = await _hung_fetch_setup(tmp_path, port)
        started = time.monotonic()
        with pytest.raises(CheckoutError) as excinfo:
            await runner.run_git(
                git_cli.fetch_by_sha("a" * 40), cwd=cache, env=env, timeout_seconds=1.5
            )
        elapsed = time.monotonic() - started
    finally:
        sock.close()
    assert excinfo.value.code is CheckoutFailureCode.FETCH_TIMEOUT
    assert elapsed < 10  # the process group died; nothing waited for curl


async def test_cancellation_kills_the_hung_fetch(tmp_path: Path) -> None:
    sock, port = _tarpit()
    try:
        runner, cache, env = await _hung_fetch_setup(tmp_path, port)
        task = asyncio.create_task(
            runner.run_git(
                git_cli.fetch_by_sha("a" * 40), cwd=cache, env=env, timeout_seconds=60
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - started
    finally:
        sock.close()
    assert elapsed < 10


async def test_nonzero_exit_does_not_raise(tmp_path: Path) -> None:
    config = GitHubAppConfig(app_id="424242")
    env = build_scrubbed_env(config, None, tmp_path / "scratch")
    runner = GitInvocation()
    completed = await runner.run_git(
        git_cli.cat_file_exists("a" * 40), cwd=tmp_path, env=env, timeout_seconds=30
    )
    assert completed.returncode != 0
