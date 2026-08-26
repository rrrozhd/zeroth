"""CheckoutService end-to-end against the full substrate: fake API + real git HTTP."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

import pytest

from tests.github.fake_github import FakeGitHubAPI
from tests.github.git_http_server import GitSmartHTTPServer
from tests.github.repo_fixtures import (
    RepoFixture,
    make_absolute_symlink_repo,
    make_benign_repo,
    make_dotgit_in_tree_repo,
    make_huge_file_repo,
    make_lfs_pointer_repo,
    make_many_files_repo,
    make_submodule_repo,
    make_symlink_escape_repo,
)
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.checkout import CheckoutService
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.git_cli import GitInvocation
from zeroth.integrations.github.materializer import LocalCheckoutMaterializer
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    CheckoutRequest,
)
from zeroth.integrations.github.token_broker import InstallationTokenBroker
from zeroth.platform.secrets.provider import EnvSecretProvider

BASE_URL = "http://api.github.test"


def _first_token_header() -> str:
    """The Authorization header git must send for the first minted token."""
    encoded = base64.b64encode(b"x-access-token:ghs_fake_1").decode("ascii")
    return f"Basic {encoded}"


class RecordingStore:
    """In-memory CheckoutStateStore capturing every transition."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def record_state(self, checkout_id: str, state: str, **fields: object) -> None:
        self.records.append((checkout_id, state, dict(fields)))

    def states(self) -> list[str]:
        return [state for _checkout_id, state, _fields in self.records]


@dataclass
class Rig:
    """One fully wired checkout stack over a served fixture repository."""

    api: FakeGitHubAPI
    server: GitSmartHTTPServer
    config: GitHubAppConfig
    client: GitHubAppClient
    broker: InstallationTokenBroker
    service: CheckoutService
    store: RecordingStore
    fixture: RepoFixture
    tmp_path: Path
    destinations: int = field(default=0)

    def request(self, **overrides) -> CheckoutRequest:
        defaults = dict(
            installation_id=1, owner="acme", name="widgets", ref="main", expected_commit_sha=None
        )
        defaults.update(overrides)
        return CheckoutRequest(**defaults)

    def destination(self) -> Path:
        self.destinations += 1
        return self.tmp_path / f"dest-{self.destinations}"

    async def aclose(self) -> None:
        await self.client.aclose()
        self.server.stop()

    def upload_pack_requests(self) -> list[tuple[str, str | None]]:
        return [item for item in self.server.requests if "upload-pack" in item[0]]


def make_rig(
    tmp_path: Path,
    builder: Callable[[Path], RepoFixture] = make_benign_repo,
    *,
    private: bool = False,
    git_base_url: str | None = None,
    **config_overrides,
) -> Rig:
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    fixture = builder(fixtures_dir)
    project_root = tmp_path / "served"
    (project_root / "acme").mkdir(parents=True)
    shutil.move(str(fixture.bare_path), str(project_root / "acme" / "widgets.git"))
    server = GitSmartHTTPServer(project_root)
    server.start()

    api = FakeGitHubAPI()
    api.add_installation(1, "acme")
    api.add_repo(1, "acme/widgets", private=private)
    api.set_ref("acme/widgets", "heads/main", fixture.head_sha)

    config = GitHubAppConfig(
        app_id=api.app_id,
        api_base_url=BASE_URL,
        git_base_url=git_base_url or server.base_url().rstrip("/"),
        allow_http_git=True,
        **config_overrides,
    )
    issuer = AppJwtIssuer(
        config, EnvSecretProvider({"GITHUB_APP_PRIVATE_KEY": api.private_key_pem})
    )
    client = GitHubAppClient(config, issuer, transport=api.transport())
    broker = InstallationTokenBroker(client)
    store = RecordingStore()
    service = CheckoutService(
        config,
        client,
        broker,
        GitInvocation(),
        cache_dir=tmp_path / "cache",
        store=store,
        fetch_timeout_seconds=60.0,
        command_timeout_seconds=60.0,
    )
    return Rig(api, server, config, client, broker, service, store, fixture, tmp_path)


async def _expect_failure(
    rig: Rig, code: CheckoutFailureCode, *, destination: Path | None = None, **request_overrides
) -> CheckoutError:
    destination = destination if destination is not None else rig.destination()
    with pytest.raises(CheckoutError) as excinfo:
        await rig.service.stage(
            rig.request(**request_overrides), destination=destination, tenant_id="tenant-a"
        )
    assert excinfo.value.code is code, str(excinfo.value)
    # The destination is always left empty, whatever failed.
    assert not destination.exists() or not any(destination.iterdir())
    return excinfo.value


# -- happy paths ---------------------------------------------------------------


async def test_happy_path_public_repo(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    try:
        destination = rig.destination()
        staged = await rig.service.stage(
            rig.request(), destination=destination, tenant_id="tenant-a"
        )
    finally:
        await rig.aclose()

    assert staged.commit_sha == rig.fixture.head_sha
    assert re.fullmatch(r"[0-9a-f]{40}", staged.git_tree_id)
    assert staged.git_tree_id != staged.commit_sha
    assert staged.tree_digest.startswith("sha256:")
    assert staged.file_count == 3
    assert staged.size_bytes > 0
    assert staged.has_lfs_pointers is False

    assert (destination / ".zeroth.yaml").is_file()
    assert (destination / "data" / "config.json").is_file()
    assert not (destination / ".git").exists()  # credential-free materialization
    train = destination / "scripts" / "train.py"
    assert train.is_file()
    assert os.access(train, os.X_OK)  # exec bit survived

    assert rig.store.states() == [
        "resolving",
        "fetching",
        "scanning",
        "materializing",
        "verifying",
        "ready",
    ]


async def test_private_repo_fetch_carries_the_installation_credential(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path, private=True)
    rig.server.set_expected_auth("acme", _first_token_header())
    try:
        destination = rig.destination()
        staged = await rig.service.stage(
            rig.request(), destination=destination, tenant_id="tenant-a"
        )
    finally:
        await rig.aclose()
    assert staged.commit_sha == rig.fixture.head_sha
    # The server enforced the exact header; seeing it in the log proves the
    # lease-injected extraheader was what git actually sent.
    assert any(auth == _first_token_header() for _path, auth in rig.server.requests)


async def test_cache_hit_skips_the_fetch_but_still_verifies(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    verify_calls = 0
    original_verify = rig.broker.verify_installation

    async def counting_verify(installation_id: int):
        nonlocal verify_calls
        verify_calls += 1
        return await original_verify(installation_id)

    rig.broker.verify_installation = counting_verify  # type: ignore[method-assign]
    try:
        first = await rig.service.stage(
            rig.request(), destination=rig.destination(), tenant_id="tenant-a"
        )
        requests_after_first = len(rig.server.requests)
        assert rig.upload_pack_requests()  # the first stage really fetched
        second = await rig.service.stage(
            rig.request(), destination=rig.destination(), tenant_id="tenant-a"
        )
    finally:
        await rig.aclose()
    assert len(rig.server.requests) == requests_after_first  # no second fetch at all
    assert verify_calls == 2  # installation health re-checked on the hit path
    assert second.tree_digest == first.tree_digest
    assert second.commit_sha == first.commit_sha


async def test_lfs_pointer_repo_is_flagged(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, make_lfs_pointer_repo)
    try:
        staged = await rig.service.stage(
            rig.request(), destination=rig.destination(), tenant_id="tenant-a"
        )
    finally:
        await rig.aclose()
    assert staged.has_lfs_pointers is True


# -- installation-state refusals (before any git process) ----------------------


async def test_revoked_installation_fails_with_no_git_traffic(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.api.revoke_installation(1)
    try:
        await _expect_failure(rig, CheckoutFailureCode.INSTALLATION_REVOKED)
    finally:
        await rig.aclose()
    assert rig.server.requests == []  # refused before any git process ran
    assert rig.store.states()[-1] == "failed"
    assert rig.store.records[-1][2]["code"] == "installation_revoked"


async def test_suspended_installation_fails_with_no_git_traffic(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.api.suspend(1)
    try:
        await _expect_failure(rig, CheckoutFailureCode.INSTALLATION_SUSPENDED)
    finally:
        await rig.aclose()
    assert rig.server.requests == []


async def test_unknown_ref_is_ref_not_found(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    try:
        await _expect_failure(rig, CheckoutFailureCode.REF_NOT_FOUND, ref="no-such-branch")
    finally:
        await rig.aclose()
    assert rig.server.requests == []


# -- fetch-layer failures ------------------------------------------------------


async def test_unknown_pinned_sha_is_commit_unreachable(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    ghost_sha = "c" * 40
    rig.api.add_commit("acme/widgets", ghost_sha)  # the API knows it; the repo does not
    try:
        await _expect_failure(
            rig, CheckoutFailureCode.COMMIT_UNREACHABLE, expected_commit_sha=ghost_sha
        )
    finally:
        await rig.aclose()


async def test_midfetch_auth_failure_raises_redacted_error(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, private=True)
    rig.server.fail_all_with(401)
    try:
        error = await _expect_failure(rig, CheckoutFailureCode.GIT_ERROR)
    finally:
        await rig.aclose()
    message = str(error)
    assert rig.api.minted_tokens  # a token was really minted and used
    for token in rig.api.minted_tokens:
        assert token not in message
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        assert encoded not in message


# -- hostile trees (Phase A: refused before materialization) -------------------


@pytest.mark.parametrize(
    ("builder", "expected_code"),
    [
        (make_symlink_escape_repo, CheckoutFailureCode.TREE_SYMLINK),
        (make_absolute_symlink_repo, CheckoutFailureCode.TREE_SYMLINK),
        (make_submodule_repo, CheckoutFailureCode.TREE_SUBMODULE),
        (make_dotgit_in_tree_repo, CheckoutFailureCode.TREE_DOTGIT),
    ],
    ids=["symlink-escape", "symlink-absolute", "submodule", "dotgit-in-tree"],
)
async def test_hostile_repo_is_refused_before_materialization(
    tmp_path: Path,
    builder: Callable[[Path], RepoFixture],
    expected_code: CheckoutFailureCode,
) -> None:
    rig = make_rig(tmp_path, builder)
    destination = rig.destination()
    try:
        await _expect_failure(rig, expected_code, destination=destination)
    finally:
        await rig.aclose()
    # Refused at the object level (or at fetch fsck): nothing was materialized.
    assert not destination.exists() or not any(destination.iterdir())
    assert "materializing" not in rig.store.states()


async def test_file_size_cap_refuses_before_materialization(tmp_path: Path) -> None:
    rig = make_rig(
        tmp_path, lambda base: make_huge_file_repo(base, file_bytes=2048), max_file_bytes=1024
    )
    try:
        await _expect_failure(rig, CheckoutFailureCode.CAPS_FILE_SIZE)
    finally:
        await rig.aclose()
    assert "materializing" not in rig.store.states()


async def test_file_count_cap_refuses_before_materialization(tmp_path: Path) -> None:
    rig = make_rig(
        tmp_path, lambda base: make_many_files_repo(base, count=12), max_file_count=10
    )
    try:
        await _expect_failure(rig, CheckoutFailureCode.CAPS_FILE_COUNT)
    finally:
        await rig.aclose()
    assert "materializing" not in rig.store.states()


async def test_total_size_cap_refuses_before_materialization(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, max_total_bytes=10)
    try:
        await _expect_failure(rig, CheckoutFailureCode.CAPS_TOTAL_SIZE)
    finally:
        await rig.aclose()
    assert "materializing" not in rig.store.states()


# -- cancellation --------------------------------------------------------------


async def test_cancellation_mid_fetch_cleans_destination_and_scratch(
    tmp_path: Path,
) -> None:
    tarpit = socket.socket()
    tarpit.bind(("127.0.0.1", 0))
    tarpit.listen(8)
    port = tarpit.getsockname()[1]
    rig = make_rig(tmp_path, git_base_url=f"http://127.0.0.1:{port}")
    destination = rig.destination()
    try:
        task = asyncio.create_task(
            rig.service.stage(rig.request(), destination=destination, tenant_id="tenant-a")
        )
        await asyncio.sleep(1.0)  # API resolution done; git fetch hung on the tarpit
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        tarpit.close()
        await rig.aclose()
    assert not destination.exists() or not any(destination.iterdir())
    assert list(tmp_path.glob(".zeroth-stage-*")) == []  # scratch removed
    assert rig.store.records[-1][1] == "failed"
    assert rig.store.records[-1][2]["code"] == "cancelled"


# -- local materializer --------------------------------------------------------


def test_local_materializer_copies_preserving_exec_bits(tmp_path: Path) -> None:
    source = tmp_path / "staged"
    (source / "scripts").mkdir(parents=True)
    script = source / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    (source / "data.txt").write_text("plain\n")
    destination = tmp_path / "out"
    LocalCheckoutMaterializer().materialize(source, destination)
    assert (destination / "data.txt").read_text() == "plain\n"
    copied = destination / "scripts" / "run.sh"
    assert os.access(copied, os.X_OK)
    assert not os.access(destination / "data.txt", os.X_OK)


def test_local_materializer_refuses_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "staged"
    source.mkdir()
    (source / "ok.txt").write_text("fine\n")
    os.symlink("/etc/passwd", source / "sneaky")
    with pytest.raises(CheckoutError) as excinfo:
        LocalCheckoutMaterializer().materialize(source, tmp_path / "out")
    assert excinfo.value.code is CheckoutFailureCode.TREE_SYMLINK
