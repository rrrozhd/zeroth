"""The checkout pipeline: resolve, verify, fetch, scan, materialize, re-verify.

``CheckoutService.stage`` turns a :class:`CheckoutRequest` into a verified
working tree. Installation health is proven before any git process runs; the
fetch is by pinned SHA into a per-tenant bare cache with the credential
injected only through the environment; Phase A judges the tree at the object
level before anything touches the destination; Phase B re-walks the
materialized tree with :func:`zeroth.platform.primitives.treeguard.guard_tree`
and records the digest. Any failure or cancellation leaves the destination
empty and the scratch directory gone.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from zeroth.integrations.github import git_cli
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.git_cli import GitInvocation, build_scrubbed_env
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    CheckoutRequest,
    RepositoryGrant,
    StagedCheckout,
)
from zeroth.integrations.github.token_broker import CredentialLease, InstallationTokenBroker
from zeroth.integrations.github.tree_policy import (
    enforce_tree_policy,
    is_lfs_pointer,
    lfs_candidate_blobs,
    parse_cat_file_batch,
    parse_ls_tree,
)
from zeroth.platform.primitives.treeguard import TreeGuardCode, TreeGuardViolation, guard_tree

_UNREACHABLE_MARKERS = (
    "not our ref",
    "unadvertised object",
    "couldn't find remote ref",
    "bad object",
    "unable to read sha1 file",
)
_DOTGIT_FSCK_MARKER = "hasdotgit"

_TREEGUARD_TO_FAILURE = {
    TreeGuardCode.SYMLINK_FOUND: CheckoutFailureCode.TREE_SYMLINK,
    TreeGuardCode.SPECIAL_FILE_FOUND: CheckoutFailureCode.TREE_TRAVERSAL,
    TreeGuardCode.DOT_GIT_FOUND: CheckoutFailureCode.TREE_DOTGIT,
    TreeGuardCode.FILE_TOO_LARGE: CheckoutFailureCode.CAPS_FILE_SIZE,
    TreeGuardCode.TREE_TOO_LARGE: CheckoutFailureCode.CAPS_TOTAL_SIZE,
    TreeGuardCode.TOO_MANY_FILES: CheckoutFailureCode.CAPS_FILE_COUNT,
    TreeGuardCode.TYPE_CHANGED: CheckoutFailureCode.TREE_TRAVERSAL,
    TreeGuardCode.TRAVERSAL_COMPONENT: CheckoutFailureCode.TREE_TRAVERSAL,
}

_DEFAULT_FETCH_TIMEOUT_SECONDS = 600.0
_DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0


class CheckoutStateStore(Protocol):
    """Where checkout lifecycle transitions are recorded, when anyone cares."""

    def record_state(
        self, checkout_id: str, state: str, **fields: object
    ) -> None:  # pragma: no cover - protocol
        """Record that ``checkout_id`` entered ``state`` with optional detail."""


class CheckoutService:
    """Stage pinned repository checkouts through the full verification pipeline."""

    def __init__(
        self,
        config: GitHubAppConfig,
        client: GitHubAppClient,
        broker: InstallationTokenBroker,
        git: GitInvocation,
        cache_dir: Path,
        store: CheckoutStateStore | None = None,
        *,
        fetch_timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT_SECONDS,
        command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._client = client
        self._broker = broker
        self._git = git
        self._cache_dir = Path(cache_dir)
        self._store = store
        self._fetch_timeout = fetch_timeout_seconds
        self._command_timeout = command_timeout_seconds

    async def stage(
        self, request: CheckoutRequest, *, destination: Path, tenant_id: str
    ) -> StagedCheckout:
        """Stage the requested commit into ``destination``, fully verified.

        On ANY failure or cancellation the destination is left empty and the
        scratch directory is removed; the failure code is recorded when a state
        store is attached.

        Raises:
            CheckoutError: With the failure code naming the first gate refused.
            asyncio.CancelledError: When the surrounding task is cancelled.
        """
        checkout_id = uuid.uuid4().hex
        destination = Path(destination)
        scratch = destination.parent / f".zeroth-stage-{checkout_id}"
        try:
            staged = await self._stage_inner(
                checkout_id, request, destination=destination, tenant_id=tenant_id, scratch=scratch
            )
        except CheckoutError as exc:
            self._reset_destination(destination)
            self._record(checkout_id, "failed", code=exc.code.value)
            raise
        except asyncio.CancelledError:
            self._reset_destination(destination)
            self._record(checkout_id, "failed", code=CheckoutFailureCode.CANCELLED.value)
            raise
        except BaseException:
            self._reset_destination(destination)
            self._record(checkout_id, "failed", code=CheckoutFailureCode.GIT_ERROR.value)
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self._record(
            checkout_id,
            "ready",
            commit_sha=staged.commit_sha,
            tree_digest=staged.tree_digest,
        )
        return staged

    async def _stage_inner(
        self,
        checkout_id: str,
        request: CheckoutRequest,
        *,
        destination: Path,
        tenant_id: str,
        scratch: Path,
    ) -> StagedCheckout:
        """Run the pipeline; every refusal surfaces as a typed CheckoutError."""
        self._record(checkout_id, "resolving")
        repo, commit_sha = await self._resolve_via_api(request)
        await self._broker.verify_installation(request.installation_id)

        scratch.mkdir(parents=True, exist_ok=True)
        base_env = build_scrubbed_env(self._config, None, scratch)
        cache_repo = self._cache_dir / tenant_id / f"{repo.repo_id}.git"
        await self._ensure_cache_repo(cache_repo, request, base_env)

        probe = await self._run(git_cli.cat_file_exists(commit_sha), cache_repo, base_env)
        if probe.returncode != 0:
            self._record(checkout_id, "fetching")
            await self._fetch_pinned(request, commit_sha, cache_repo, scratch)

        self._record(checkout_id, "scanning")
        has_lfs_pointers = await self._phase_a(commit_sha, cache_repo, base_env)

        self._record(checkout_id, "materializing")
        destination.mkdir(parents=True, exist_ok=True)
        checkout_env = dict(base_env)
        checkout_env["GIT_INDEX_FILE"] = str(scratch / "index")
        materialized = await self._run(
            git_cli.checkout_into(cache_repo, destination, commit_sha),
            destination,
            checkout_env,
        )
        if materialized.returncode != 0:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR,
                f"git checkout exited {materialized.returncode}",
            )

        self._record(checkout_id, "verifying")
        report = await self._phase_b(destination, checkout_id)
        tree_id = await self._run(git_cli.rev_parse_tree(commit_sha), cache_repo, base_env)
        if tree_id.returncode != 0:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR,
                f"git rev-parse exited {tree_id.returncode}",
            )
        return StagedCheckout(
            checkout_id=checkout_id,
            commit_sha=commit_sha,
            git_tree_id=tree_id.stdout.decode("ascii").strip(),
            tree_digest=report.tree_digest,
            file_count=report.file_count,
            size_bytes=report.total_bytes,
            has_lfs_pointers=has_lfs_pointers,
            verified_at=datetime.now(UTC),
        )

    # -- pipeline steps ---------------------------------------------------------

    async def _resolve_via_api(
        self, request: CheckoutRequest
    ) -> tuple[RepositoryGrant, str]:
        """Learn repo identity and pin the commit SHA through the REST API."""
        key = (request.installation_id, request.name)
        repo = await self._broker.run_with_lease(
            *key,
            lambda token: self._client.get_repository(token, request.owner, request.name),
        )
        wanted = request.expected_commit_sha or request.ref
        commit_sha = await self._broker.run_with_lease(
            *key,
            lambda token: self._client.resolve_ref(token, request.owner, request.name, wanted),
        )
        return repo, commit_sha

    async def _ensure_cache_repo(
        self, cache_repo: Path, request: CheckoutRequest, env: dict[str, str]
    ) -> None:
        """Initialize the tenant's bare cache repo for this repository once."""
        if (cache_repo / "HEAD").exists():
            return
        cache_repo.mkdir(parents=True, exist_ok=True)
        initialized = await self._run(git_cli.init_bare_cache(), cache_repo, env)
        if initialized.returncode != 0:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR,
                f"git init exited {initialized.returncode}",
            )
        origin_url = f"{self._config.git_base_url}/{request.owner}/{request.name}.git"
        remote = await self._run(git_cli.add_origin(origin_url), cache_repo, env)
        if remote.returncode != 0:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR,
                f"git remote add exited {remote.returncode}",
            )

    async def _fetch_pinned(
        self,
        request: CheckoutRequest,
        commit_sha: str,
        cache_repo: Path,
        scratch: Path,
    ) -> None:
        """Fetch the pinned SHA with the credential in the environment only."""
        lease = await self._broker.lease(request.installation_id, request.name)
        fetch_env = build_scrubbed_env(self._config, lease, scratch)
        try:
            fetched = await self._git.run_git(
                git_cli.fetch_by_sha(commit_sha),
                cwd=cache_repo,
                env=fetch_env,
                timeout_seconds=self._fetch_timeout,
                lease=lease,
            )
            if fetched.returncode != 0:
                code = self._classify_fetch_failure(fetched.stderr_redacted)
                if code is CheckoutFailureCode.GIT_ERROR and await self._remote_answers(
                    cache_repo, fetch_env, lease
                ):
                    # The remote serves us fine but refused to hand over this
                    # commit: the pinned SHA is unreachable (GC'd or unknown),
                    # not a transport failure. Classified on evidence, not on
                    # server-specific stderr phrasing.
                    code = CheckoutFailureCode.COMMIT_UNREACHABLE
                raise CheckoutError(
                    code,
                    f"git fetch exited {fetched.returncode}: "
                    f"{fetched.stderr_redacted[-500:].strip()}",
                )
        finally:
            await self._client.revoke_installation_token(lease.reveal())
            self._broker.invalidate(request.installation_id, request.name, lease.reveal())

    @staticmethod
    def _classify_fetch_failure(stderr_redacted: str) -> CheckoutFailureCode:
        """Map redacted fetch stderr onto the failure vocabulary."""
        lowered = stderr_redacted.lower()
        if _DOTGIT_FSCK_MARKER in lowered:
            # transfer.fsckObjects refused the pack because a tree carries a
            # ``.git`` entry -- the same hostile shape Phase A would refuse.
            return CheckoutFailureCode.TREE_DOTGIT
        if any(marker in lowered for marker in _UNREACHABLE_MARKERS):
            return CheckoutFailureCode.COMMIT_UNREACHABLE
        return CheckoutFailureCode.GIT_ERROR

    async def _remote_answers(
        self, cache_repo: Path, fetch_env: dict[str, str], lease: CredentialLease
    ) -> bool:
        """Probe origin health after a failed fetch; failures mean unhealthy."""
        try:
            probe = await self._git.run_git(
                git_cli.ls_remote_heads(),
                cwd=cache_repo,
                env=fetch_env,
                timeout_seconds=min(self._command_timeout, 30.0),
                lease=lease,
            )
        except CheckoutError:
            return False
        return probe.returncode == 0

    async def _phase_a(
        self, commit_sha: str, cache_repo: Path, env: dict[str, str]
    ) -> bool:
        """Object-level policy over the fetched tree; returns the LFS flag."""
        listing = await self._run(git_cli.ls_tree(commit_sha), cache_repo, env)
        if listing.returncode != 0:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR,
                f"git ls-tree exited {listing.returncode}",
            )
        entries = parse_ls_tree(listing.stdout)
        enforce_tree_policy(entries, self._config)
        candidates = lfs_candidate_blobs(entries)
        if not candidates:
            return False
        batch_input = b"".join(sha.encode("ascii") + b"\n" for sha, _path in candidates)
        batch = await self._run(
            git_cli.cat_file_batch(), cache_repo, env, input_bytes=batch_input
        )
        if batch.returncode != 0:
            raise CheckoutError(
                CheckoutFailureCode.GIT_ERROR,
                f"git cat-file exited {batch.returncode}",
            )
        contents = parse_cat_file_batch(batch.stdout)
        return any(is_lfs_pointer(content) for content in contents.values())

    async def _phase_b(self, destination: Path, checkout_id: str) -> Any:
        """Hardened re-walk of the materialized tree; returns the guard report."""
        try:
            return await asyncio.to_thread(
                guard_tree,
                destination,
                max_file_bytes=self._config.max_file_bytes,
                max_total_bytes=self._config.max_total_bytes,
                max_file_count=self._config.max_file_count,
                context=f"github checkout {checkout_id}",
            )
        except TreeGuardViolation as violation:
            raise CheckoutError(
                _TREEGUARD_TO_FAILURE[violation.code],
                f"materialized tree refused ({violation.code.value})",
            ) from violation
        except OSError as exc:
            # A root that cannot be opened O_NOFOLLOW|O_DIRECTORY is hostile
            # (e.g. a symlinked checkout root), not a broken disk -- refuse.
            raise CheckoutError(
                CheckoutFailureCode.TREE_TRAVERSAL,
                "materialized tree root could not be walked",
            ) from exc

    # -- plumbing ---------------------------------------------------------------

    async def _run(
        self,
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        input_bytes: bytes | None = None,
    ) -> Any:
        """Run a credential-free local git command with the command timeout."""
        return await self._git.run_git(
            args,
            cwd=cwd,
            env=env,
            timeout_seconds=self._command_timeout,
            input_bytes=input_bytes,
        )

    def _record(self, checkout_id: str, state: str, **fields: object) -> None:
        """Record a lifecycle transition when a store is attached."""
        if self._store is not None:
            self._store.record_state(checkout_id, state, **fields)

    @staticmethod
    def _reset_destination(destination: Path) -> None:
        """Leave the destination as an empty directory after a failure."""
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True, exist_ok=True)


__all__ = ["CheckoutService", "CheckoutStateStore"]
