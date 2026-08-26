"""In-process fake of the GitHub App REST surface, served over httpx.ASGITransport.

ZER-37 test substrate. The fake covers exactly the endpoints a GitHub App
integration client needs -- App-JWT installation lookup, installation token
minting, repo/ref/commit reads, token revocation -- with real RS256 JWT
verification against a per-instance keypair, so a client that signs badly or
not at all is rejected the way api.github.com would reject it.

Fault injection: ``fail_next(status)`` fails exactly one request,
``expire_next_token()`` makes the next minted token already expired,
``latency_seconds`` delays every request, ``revoke_installation`` /
``suspend`` / ``remove_repo`` flip the corresponding account states.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_DEFAULT_TOKEN_TTL_SECONDS = 3600


def _generate_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def _unauthorized() -> JSONResponse:
    return JSONResponse({"message": "Bad credentials"}, status_code=401)


def _not_found() -> JSONResponse:
    return JSONResponse({"message": "Not Found"}, status_code=404)


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=403)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@dataclass
class _Repo:
    repo_id: int
    full_name: str
    private: bool
    default_branch: str

    def as_json(self) -> dict:
        owner, _, name = self.full_name.partition("/")
        return {
            "id": self.repo_id,
            "full_name": self.full_name,
            "name": name,
            "owner": {"login": owner},
            "private": self.private,
            "default_branch": self.default_branch,
        }


@dataclass
class _Installation:
    installation_id: int
    account: str
    repos: dict[str, _Repo] = field(default_factory=dict)
    suspended: bool = False
    revoked: bool = False


@dataclass
class _MintedToken:
    token: str
    installation_id: int
    repositories: tuple[str, ...] | None  # None means every installation repo, live
    expires_at: float
    revoked: bool = False


class FakeGitHubAPI:
    """A stateful fake GitHub REST v3 App surface behind an ASGI transport."""

    def __init__(
        self,
        *,
        app_id: str = "424242",
        token_ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS,
        private_key_pem: str | None = None,
        public_key_pem: str | None = None,
    ) -> None:
        if private_key_pem is None:
            private_key_pem, public_key_pem = _generate_keypair()
        elif public_key_pem is None:
            raise ValueError("public_key_pem is required when private_key_pem is supplied")
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem
        self._token_ttl_seconds = token_ttl_seconds
        self._installations: dict[int, _Installation] = {}
        self._refs: dict[str, dict[str, str]] = {}
        self._commits: dict[str, set[str]] = {}
        self._minted: list[_MintedToken] = []
        self.revoked_tokens: list[str] = []
        self._token_serial = 0
        self._fail_next_status: int | None = None
        self._expire_next_token = False
        self.latency_seconds = 0.0
        self._app = self._build_app()

    # -- wiring for the client under test -------------------------------------

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def private_key_pem(self) -> str:
        return self._private_key_pem

    @property
    def minted_tokens(self) -> list[str]:
        """Exact token strings minted so far, in minting order."""
        return [record.token for record in self._minted]

    @property
    def token_records(self) -> list[_MintedToken]:
        return list(self._minted)

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self)

    async def __call__(self, scope, receive, send) -> None:
        """ASGI entrypoint applying fault injection before routing."""
        if scope["type"] == "http":
            if self._fail_next_status is not None:
                status = self._fail_next_status
                self._fail_next_status = None
                response = JSONResponse({"message": "injected failure"}, status_code=status)
                await response(scope, receive, send)
                return
            if self.latency_seconds:
                await asyncio.sleep(self.latency_seconds)
        await self._app(scope, receive, send)

    # -- state setup and fault toggles ----------------------------------------

    def add_installation(self, installation_id: int, account: str) -> None:
        self._installations[installation_id] = _Installation(installation_id, account)

    def add_repo(
        self,
        installation_id: int,
        full_name: str,
        *,
        private: bool = False,
        default_branch: str = "main",
    ) -> None:
        installation = self._installations[installation_id]
        repo_id = 1000 + sum(len(inst.repos) for inst in self._installations.values())
        installation.repos[full_name] = _Repo(repo_id, full_name, private, default_branch)

    def remove_repo(self, installation_id: int, full_name: str) -> None:
        self._installations[installation_id].repos.pop(full_name, None)

    def set_ref(self, full_name: str, ref: str, sha: str) -> None:
        """Set ``refs/<ref>`` (e.g. ``heads/main``, ``tags/v1``) and register the sha."""
        self._refs.setdefault(full_name, {})[ref] = sha
        self.add_commit(full_name, sha)

    def add_commit(self, full_name: str, sha: str) -> None:
        self._commits.setdefault(full_name, set()).add(sha)

    def revoke_installation(self, installation_id: int) -> None:
        self._installations[installation_id].revoked = True

    def suspend(self, installation_id: int) -> None:
        self._installations[installation_id].suspended = True

    def expire_next_token(self) -> None:
        self._expire_next_token = True

    def fail_next(self, status_code: int) -> None:
        self._fail_next_status = status_code

    # -- auth helpers ----------------------------------------------------------

    def _app_jwt_error(self, request: Request) -> JSONResponse | None:
        header = request.headers.get("authorization", "")
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return _unauthorized()
        try:
            jwt.decode(
                credential,
                self._public_key_pem,
                algorithms=["RS256"],
                issuer=self._app_id,
                options={"require": ["exp", "iss"]},
            )
        except jwt.PyJWTError:
            return _unauthorized()
        return None

    def _installation_token(self, request: Request) -> _MintedToken | None:
        header = request.headers.get("authorization", "")
        scheme, _, credential = header.partition(" ")
        if scheme.lower() not in {"token", "bearer"} or not credential:
            return None
        for record in self._minted:
            if record.token != credential:
                continue
            if record.revoked or record.expires_at <= time.time():
                return None
            installation = self._installations.get(record.installation_id)
            if installation is None or installation.revoked:
                return None
            return record
        return None

    def _scoped_repos(self, record: _MintedToken) -> dict[str, _Repo]:
        installation = self._installations[record.installation_id]
        if record.repositories is None:
            return dict(installation.repos)
        return {
            full_name: repo
            for full_name, repo in installation.repos.items()
            if full_name in record.repositories
        }

    # -- endpoints -------------------------------------------------------------

    async def _get_installation(self, request: Request) -> Response:
        if (error := self._app_jwt_error(request)) is not None:
            return error
        installation = self._installations.get(int(request.path_params["installation_id"]))
        if installation is None or installation.revoked:
            return _not_found()
        return JSONResponse(
            {
                "id": installation.installation_id,
                "account": {"login": installation.account},
                "app_id": int(self._app_id),
                "suspended_at": _iso(time.time()) if installation.suspended else None,
            }
        )

    async def _mint_token(self, request: Request) -> Response:
        if (error := self._app_jwt_error(request)) is not None:
            return error
        installation = self._installations.get(int(request.path_params["installation_id"]))
        if installation is None or installation.revoked:
            return _not_found()
        if installation.suspended:
            return _forbidden("This installation has been suspended")
        body = await request.body()
        requested_names: list[str] | None = None
        if body:
            requested_names = json.loads(body).get("repositories")
        repositories: tuple[str, ...] | None = None
        if requested_names is not None:
            by_short_name = {name.rsplit("/", 1)[-1]: name for name in installation.repos}
            missing = [name for name in requested_names if name not in by_short_name]
            if missing:
                return _forbidden("Resource not accessible by integration")
            repositories = tuple(by_short_name[name] for name in requested_names)
        self._token_serial += 1
        expires_at = time.time() + self._token_ttl_seconds
        if self._expire_next_token:
            expires_at = time.time() - 1
            self._expire_next_token = False
        record = _MintedToken(
            token=f"ghs_fake_{self._token_serial}",
            installation_id=installation.installation_id,
            repositories=repositories,
            expires_at=expires_at,
        )
        self._minted.append(record)
        scoped = self._scoped_repos(record)
        return JSONResponse(
            {
                "token": record.token,
                "expires_at": _iso(record.expires_at),
                "repositories": [repo.as_json() for repo in scoped.values()],
            },
            status_code=201,
        )

    async def _list_repositories(self, request: Request) -> Response:
        record = self._installation_token(request)
        if record is None:
            return _unauthorized()
        scoped = self._scoped_repos(record)
        return JSONResponse(
            {
                "total_count": len(scoped),
                "repositories": [repo.as_json() for repo in scoped.values()],
            }
        )

    def _scoped_repo_for(self, request: Request) -> tuple[_MintedToken, _Repo] | Response:
        record = self._installation_token(request)
        if record is None:
            return _unauthorized()
        full_name = f"{request.path_params['owner']}/{request.path_params['repo']}"
        repo = self._scoped_repos(record).get(full_name)
        if repo is None:
            return _not_found()
        return record, repo

    async def _get_repo(self, request: Request) -> Response:
        resolved = self._scoped_repo_for(request)
        if isinstance(resolved, Response):
            return resolved
        _, repo = resolved
        return JSONResponse(repo.as_json())

    async def _get_ref(self, request: Request, *, kind: str) -> Response:
        resolved = self._scoped_repo_for(request)
        if isinstance(resolved, Response):
            return resolved
        _, repo = resolved
        ref = f"{kind}/{request.path_params['name']}"
        sha = self._refs.get(repo.full_name, {}).get(ref)
        if sha is None:
            return _not_found()
        return JSONResponse(
            {"ref": f"refs/{ref}", "object": {"sha": sha, "type": "commit"}}
        )

    async def _get_head_ref(self, request: Request) -> Response:
        return await self._get_ref(request, kind="heads")

    async def _get_tag_ref(self, request: Request) -> Response:
        return await self._get_ref(request, kind="tags")

    async def _get_commit(self, request: Request) -> Response:
        resolved = self._scoped_repo_for(request)
        if isinstance(resolved, Response):
            return resolved
        _, repo = resolved
        sha = request.path_params["sha"]
        if sha not in self._commits.get(repo.full_name, set()):
            return _not_found()
        return JSONResponse({"sha": sha})

    async def _revoke_token(self, request: Request) -> Response:
        record = self._installation_token(request)
        if record is None:
            return _unauthorized()
        record.revoked = True
        self.revoked_tokens.append(record.token)
        return Response(status_code=204)

    def _build_app(self) -> Starlette:
        return Starlette(
            routes=[
                Route(
                    "/app/installations/{installation_id:int}",
                    self._get_installation,
                    methods=["GET"],
                ),
                Route(
                    "/app/installations/{installation_id:int}/access_tokens",
                    self._mint_token,
                    methods=["POST"],
                ),
                Route(
                    "/installation/repositories",
                    self._list_repositories,
                    methods=["GET"],
                ),
                Route(
                    "/installation/token",
                    self._revoke_token,
                    methods=["DELETE"],
                ),
                Route(
                    "/repos/{owner}/{repo}/git/ref/heads/{name:path}",
                    self._get_head_ref,
                    methods=["GET"],
                ),
                Route(
                    "/repos/{owner}/{repo}/git/ref/tags/{name:path}",
                    self._get_tag_ref,
                    methods=["GET"],
                ),
                Route(
                    "/repos/{owner}/{repo}/git/commits/{sha}",
                    self._get_commit,
                    methods=["GET"],
                ),
                Route(
                    "/repos/{owner}/{repo}",
                    self._get_repo,
                    methods=["GET"],
                ),
            ]
        )
