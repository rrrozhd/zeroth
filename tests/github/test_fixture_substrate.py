"""Proofs for the ZER-37 GitHub test substrate itself.

Three layers, each proven independently before any checkout client exists:
the fake GitHub API really verifies RS256 App JWTs and scopes tokens; the
smart-HTTP server serves fixtures to a REAL ``git fetch`` (protocol v2,
by-SHA, with and without an injected Authorization header); and the hostile
repo builders produce exactly the tree shapes their names promise.
"""

from __future__ import annotations

import base64
import subprocess
import time
from pathlib import Path

import httpx
import jwt

from tests.github.fake_github import FakeGitHubAPI
from tests.github.git_http_server import GitSmartHTTPServer
from tests.github.repo_fixtures import (
    git_env,
    make_absolute_symlink_repo,
    make_benign_repo,
    make_dotgit_in_tree_repo,
    make_huge_file_repo,
    make_lfs_pointer_repo,
    make_many_files_repo,
    make_multi_script_manifest_repo,
    make_no_config_repo,
    make_submodule_repo,
    make_symlink_escape_repo,
)

BASE_URL = "http://api.github.test"

HEAD_SHA = "a" * 40


def _client(api: FakeGitHubAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=api.transport(), base_url=BASE_URL)


def _app_jwt(api: FakeGitHubAPI, *, key: str | None = None, exp_delta: int = 300) -> str:
    now = int(time.time())
    payload = {"iat": now - 30, "exp": now + exp_delta, "iss": api.app_id}
    return jwt.encode(payload, key or api.private_key_pem, algorithm="RS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _standard_api() -> FakeGitHubAPI:
    api = FakeGitHubAPI()
    api.add_installation(1, "acme")
    api.add_repo(1, "acme/widgets", private=True)
    return api


async def _mint(api: FakeGitHubAPI, client: httpx.AsyncClient, *, repositories=None) -> str:
    body = {} if repositories is None else {"repositories": repositories}
    response = await client.post(
        "/app/installations/1/access_tokens", json=body, headers=_auth(_app_jwt(api))
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


# -- 1. FakeGitHubAPI ---------------------------------------------------------


async def test_correctly_signed_app_jwt_passes() -> None:
    api = _standard_api()
    async with _client(api) as client:
        response = await client.get("/app/installations/1", headers=_auth(_app_jwt(api)))
    assert response.status_code == 200
    assert response.json()["account"]["login"] == "acme"


async def test_wrong_key_expired_and_absent_jwts_are_rejected() -> None:
    api = _standard_api()
    stranger = FakeGitHubAPI()  # a different keypair
    async with _client(api) as client:
        wrong_key = await client.get(
            "/app/installations/1",
            headers=_auth(_app_jwt(api, key=stranger.private_key_pem)),
        )
        expired = await client.get(
            "/app/installations/1", headers=_auth(_app_jwt(api, exp_delta=-60))
        )
        absent = await client.get("/app/installations/1")
        bad_issuer_token = jwt.encode(
            {"iat": int(time.time()), "exp": int(time.time()) + 300, "iss": "999"},
            api.private_key_pem,
            algorithm="RS256",
        )
        bad_issuer = await client.get("/app/installations/1", headers=_auth(bad_issuer_token))
    assert wrong_key.status_code == 401
    assert expired.status_code == 401
    assert absent.status_code == 401
    assert bad_issuer.status_code == 401


async def test_minted_token_is_recorded_verbatim() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token = await _mint(api, client)
    assert token.startswith("ghs_fake_")
    assert api.minted_tokens == [token]


async def test_revoked_installation_is_gone() -> None:
    api = _standard_api()
    api.revoke_installation(1)
    async with _client(api) as client:
        lookup = await client.get("/app/installations/1", headers=_auth(_app_jwt(api)))
        mint = await client.post(
            "/app/installations/1/access_tokens", headers=_auth(_app_jwt(api))
        )
    assert lookup.status_code == 404
    assert mint.status_code == 404


async def test_suspended_installation_refuses_minting() -> None:
    api = _standard_api()
    api.suspend(1)
    async with _client(api) as client:
        mint = await client.post(
            "/app/installations/1/access_tokens", headers=_auth(_app_jwt(api))
        )
    assert mint.status_code == 403


async def test_removed_repo_falls_out_of_scope() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token = await _mint(api, client)
        headers = {"Authorization": f"token {token}"}
        before = await client.get("/repos/acme/widgets", headers=headers)
        api.remove_repo(1, "acme/widgets")
        after = await client.get("/repos/acme/widgets", headers=headers)
    assert before.status_code == 200
    assert after.status_code == 404


async def test_repo_scoped_token_cannot_see_out_of_scope_repo() -> None:
    api = _standard_api()
    api.add_repo(1, "acme/other")
    async with _client(api) as client:
        token = await _mint(api, client, repositories=["widgets"])
        headers = {"Authorization": f"token {token}"}
        listing = await client.get("/installation/repositories", headers=headers)
        in_scope = await client.get("/repos/acme/widgets", headers=headers)
        out_of_scope = await client.get("/repos/acme/other", headers=headers)
    assert listing.status_code == 200
    assert [repo["full_name"] for repo in listing.json()["repositories"]] == ["acme/widgets"]
    assert in_scope.status_code == 200
    assert out_of_scope.status_code == 404


async def test_out_of_scope_mint_request_is_refused() -> None:
    api = _standard_api()
    async with _client(api) as client:
        response = await client.post(
            "/app/installations/1/access_tokens",
            json={"repositories": ["not-a-repo"]},
            headers=_auth(_app_jwt(api)),
        )
    assert response.status_code == 403


async def test_refs_and_commits_endpoints() -> None:
    api = _standard_api()
    api.set_ref("acme/widgets", "heads/main", HEAD_SHA)
    api.set_ref("acme/widgets", "tags/v1", HEAD_SHA)
    async with _client(api) as client:
        token = await _mint(api, client)
        headers = {"Authorization": f"token {token}"}
        head = await client.get("/repos/acme/widgets/git/ref/heads/main", headers=headers)
        tag = await client.get("/repos/acme/widgets/git/ref/tags/v1", headers=headers)
        missing = await client.get("/repos/acme/widgets/git/ref/heads/gone", headers=headers)
        commit = await client.get(f"/repos/acme/widgets/git/commits/{HEAD_SHA}", headers=headers)
        unknown = await client.get(f"/repos/acme/widgets/git/commits/{'b' * 40}", headers=headers)
    assert head.status_code == 200
    assert head.json()["object"]["sha"] == HEAD_SHA
    assert tag.status_code == 200
    assert missing.status_code == 404
    assert commit.status_code == 200
    assert unknown.status_code == 404


async def test_expired_and_revoked_tokens_are_rejected() -> None:
    api = _standard_api()
    api.expire_next_token()
    async with _client(api) as client:
        expired_token = await _mint(api, client)
        expired = await client.get(
            "/installation/repositories", headers={"Authorization": f"token {expired_token}"}
        )
        live_token = await _mint(api, client)
        headers = {"Authorization": f"token {live_token}"}
        revoke = await client.request("DELETE", "/installation/token", headers=headers)
        after_revoke = await client.get("/installation/repositories", headers=headers)
    assert expired.status_code == 401
    assert revoke.status_code == 204
    assert api.revoked_tokens == [live_token]
    assert after_revoke.status_code == 401


async def test_fail_next_fails_exactly_one_request() -> None:
    api = _standard_api()
    api.fail_next(500)
    async with _client(api) as client:
        failed = await client.get("/app/installations/1", headers=_auth(_app_jwt(api)))
        recovered = await client.get("/app/installations/1", headers=_auth(_app_jwt(api)))
    assert failed.status_code == 500
    assert recovered.status_code == 200


# -- 2/3/4. Real git fetch through the smart-HTTP server ----------------------


def _fetch_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = git_env()
    env.update(
        {
            "GIT_ALLOW_PROTOCOL": "http",
            "GIT_ASKPASS": "/bin/false",
        }
    )
    if extra:
        env.update(extra)
    return env


def _fetch_by_sha(
    clone_dir: Path,
    url: str,
    sha: str,
    *,
    extra_env: dict[str, str] | None = None,
    protocol_version: str = "2",
) -> subprocess.CompletedProcess:
    clone_dir.mkdir(parents=True, exist_ok=True)
    env = _fetch_env(extra_env)
    subprocess.run(
        ["git", "init", "-q"], cwd=clone_dir, env=env, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", url],
        cwd=clone_dir,
        env=env,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        [
            "git",
            "-c",
            f"protocol.version={protocol_version}",
            "fetch",
            "--depth",
            "1",
            "--no-tags",
            "origin",
            sha,
        ],
        cwd=clone_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _has_commit(clone_dir: Path, sha: str) -> bool:
    verify = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=clone_dir,
        env=_fetch_env(),
        capture_output=True,
    )
    return verify.returncode == 0


def _basic_auth_header(token: str) -> str:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return f"Basic {encoded}"


def test_real_git_fetch_of_public_repo_by_sha(tmp_path: Path) -> None:
    fixture = make_benign_repo(tmp_path / "repos")
    with GitSmartHTTPServer(tmp_path / "repos") as server:
        result = _fetch_by_sha(tmp_path / "clone", server.url_for("benign.git"), fixture.head_sha)
        assert result.returncode == 0, result.stderr
        assert _has_commit(tmp_path / "clone", fixture.head_sha)


def test_private_repo_requires_and_receives_injected_auth_header(tmp_path: Path) -> None:
    fixture = make_benign_repo(tmp_path / "repos")
    token = "ghs_fake_1"
    header_value = _basic_auth_header(token)
    with GitSmartHTTPServer(tmp_path / "repos") as server:
        server.set_expected_auth("benign.git", header_value)
        url = server.url_for("benign.git")

        anonymous = _fetch_by_sha(tmp_path / "clone-anon", url, fixture.head_sha)
        assert anonymous.returncode != 0

        extra_env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{server.base_url()}.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: {header_value}",
        }
        authorized = _fetch_by_sha(
            tmp_path / "clone-auth", url, fixture.head_sha, extra_env=extra_env
        )
        assert authorized.returncode == 0, authorized.stderr
        assert _has_commit(tmp_path / "clone-auth", fixture.head_sha)
        # The injection path is proven end-to-end: the server saw the header.
        assert any(auth == header_value for _, auth in server.requests)


def test_unadvertised_sha_fetch_fails_without_allow_reachable(tmp_path: Path) -> None:
    """The TOCTOU repro: pinned-SHA fetch refused once allowReachable is off.

    Uses protocol v0 deliberately: git's protocol v2 ``fetch`` command does not
    enforce ``uploadpack.allowReachableSHA1InWant`` (a long-standing behavior),
    so the server's refusal of an unadvertised object is only observable over
    v0. The property under test is the server's refusal, not the wire version.
    """
    fixture = make_benign_repo(tmp_path / "repos", allow_reachable_sha1=False)
    parent_sha = fixture.extra["parent_sha"]
    with GitSmartHTTPServer(tmp_path / "repos") as server:
        result = _fetch_by_sha(
            tmp_path / "clone",
            server.url_for("benign.git"),
            parent_sha,
            protocol_version="0",
        )
    assert result.returncode != 0
    assert "unadvertised object" in result.stderr
    assert not _has_commit(tmp_path / "clone", parent_sha)


def test_fail_all_with_brings_the_server_down(tmp_path: Path) -> None:
    fixture = make_benign_repo(tmp_path / "repos")
    with GitSmartHTTPServer(tmp_path / "repos") as server:
        server.fail_all_with(503)
        result = _fetch_by_sha(tmp_path / "clone", server.url_for("benign.git"), fixture.head_sha)
    assert result.returncode != 0


# -- 5. Hostile builders produce the promised tree shapes ---------------------


def _ls_tree(bare_path: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "ls-tree", "-r", sha],
        cwd=bare_path,
        env=git_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_symlink_fixture_has_mode_120000_entry(tmp_path: Path) -> None:
    fixture = make_symlink_escape_repo(tmp_path)
    listing = _ls_tree(fixture.bare_path, fixture.head_sha)
    assert "120000 blob" in listing
    assert "\tescape" in listing


def test_submodule_fixture_has_mode_160000_gitlink(tmp_path: Path) -> None:
    fixture = make_submodule_repo(tmp_path)
    listing = _ls_tree(fixture.bare_path, fixture.head_sha)
    assert f"160000 commit {fixture.extra['gitlink_sha']}\tvendor/dep" in listing


def test_dotgit_fixture_has_in_tree_dot_git_file(tmp_path: Path) -> None:
    fixture = make_dotgit_in_tree_repo(tmp_path)
    listing = _ls_tree(fixture.bare_path, fixture.head_sha)
    assert "\t.git" in listing
    assert "100644 blob" in listing


def test_dotgit_fixture_is_fetchable(tmp_path: Path) -> None:
    """The crafted repo must still serve a real fetch -- that is its whole point."""
    fixture = make_dotgit_in_tree_repo(tmp_path / "repos")
    with GitSmartHTTPServer(tmp_path / "repos") as server:
        result = _fetch_by_sha(
            tmp_path / "clone", server.url_for("dotgit-in-tree.git"), fixture.head_sha
        )
    assert result.returncode == 0, result.stderr


def test_every_builder_produces_a_resolvable_bare_repo(tmp_path: Path) -> None:
    """Every builder runs and yields a bare repo whose HEAD commit resolves.

    A builder that trips git's fsck (the ``.git`` tree needed ``--literally``)
    fails only when it is actually called. Exercising all ten here means the
    next author does not discover a broken builder mid-collection with no clue
    the substrate was never run.
    """
    fixtures = [
        make_benign_repo(tmp_path),
        make_symlink_escape_repo(tmp_path),
        make_absolute_symlink_repo(tmp_path),
        make_submodule_repo(tmp_path),
        make_dotgit_in_tree_repo(tmp_path),
        make_huge_file_repo(tmp_path, file_bytes=1024),
        make_many_files_repo(tmp_path, count=5),
        make_lfs_pointer_repo(tmp_path),
        make_multi_script_manifest_repo(tmp_path),
        make_no_config_repo(tmp_path),
    ]
    assert len(fixtures) == 10
    for fixture in fixtures:
        assert fixture.bare_path.is_dir()
        resolved = subprocess.run(
            ["git", "cat-file", "-e", f"{fixture.head_sha}^{{commit}}"],
            cwd=fixture.bare_path,
            env=git_env(),
            capture_output=True,
        )
        assert resolved.returncode == 0, fixture.bare_path
