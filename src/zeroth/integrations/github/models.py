"""Domain models and the typed failure vocabulary for the GitHub App integration.

Everything a checkout produces or refuses is described here: installation and
repository projections of the GitHub App surface, the checkout request/result
pair, and :class:`CheckoutError` -- the single exception family whose ``code``
is machine-readable and whose message is a fixed template over already-redacted
detail. Response bodies, credentials, and attacker-chosen entry names never
appear in these messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class InstallationState(StrEnum):
    """Lifecycle of a GitHub App installation as the platform tracks it."""

    PENDING_CLAIM = "pending_claim"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class RepositoryState(StrEnum):
    """Whether a granted repository is still reachable through the installation."""

    ACTIVE = "active"
    REMOVED = "removed"


@dataclass(frozen=True)
class Installation:
    """One GitHub App installation, projected onto the fields the platform needs."""

    installation_id: int
    account_login: str
    account_type: str
    repository_selection: str
    state: InstallationState
    last_verified_at: datetime | None = None
    tenant_id: str | None = None


@dataclass(frozen=True)
class RepositoryGrant:
    """One repository an installation grants access to."""

    repo_id: int
    owner: str
    name: str
    full_name: str
    private: bool
    default_branch: str
    state: RepositoryState = RepositoryState.ACTIVE


@dataclass(frozen=True)
class CheckoutRequest:
    """What to stage: a repository plus either a symbolic ref or a pinned commit.

    ``installation_id`` addresses the GitHub App installation the checkout
    authenticates through; nothing else in the request identifies it.
    """

    installation_id: int
    owner: str
    name: str
    ref: str
    expected_commit_sha: str | None = None


@dataclass(frozen=True)
class StagedCheckout:
    """A checkout that passed every gate, with the identities the audit trail pins."""

    checkout_id: str
    commit_sha: str
    git_tree_id: str
    tree_digest: str
    file_count: int
    size_bytes: int
    has_lfs_pointers: bool
    verified_at: datetime


class CheckoutFailureCode(StrEnum):
    """Why a checkout was refused; the machine-readable half of every failure."""

    INSTALLATION_REVOKED = "installation_revoked"
    INSTALLATION_SUSPENDED = "installation_suspended"
    REPO_OUT_OF_SCOPE = "repo_out_of_scope"
    REF_NOT_FOUND = "ref_not_found"
    COMMIT_UNREACHABLE = "commit_unreachable"
    TREE_SYMLINK = "tree_symlink"
    TREE_SUBMODULE = "tree_submodule"
    TREE_DOTGIT = "tree_dotgit"
    TREE_TRAVERSAL = "tree_traversal"
    CAPS_FILE_SIZE = "caps_file_size"
    CAPS_TOTAL_SIZE = "caps_total_size"
    CAPS_FILE_COUNT = "caps_file_count"
    LFS_REQUIRED = "lfs_required"
    FETCH_TIMEOUT = "fetch_timeout"
    CANCELLED = "cancelled"
    GIT_ERROR = "git_error"
    API_ERROR = "api_error"
    CONFIG_MISSING = "config_missing"


class CheckoutError(Exception):
    """A refused checkout step: a failure code plus already-redacted detail.

    ``detail`` must be safe to log verbatim -- the caller redacts credentials
    before constructing the error, and fixed templates are used everywhere a
    response body or attacker-chosen name could otherwise leak.
    """

    def __init__(self, code: CheckoutFailureCode, detail: str) -> None:
        super().__init__(f"checkout failed ({code.value}): {detail}")
        self.code = code
        self.detail = detail


class InstallationRevokedError(CheckoutError):
    """The installation no longer exists: uninstalled, or access was revoked."""

    def __init__(
        self, detail: str = "installation is revoked or the app is uninstalled"
    ) -> None:
        super().__init__(CheckoutFailureCode.INSTALLATION_REVOKED, detail)


class InstallationSuspendedError(CheckoutError):
    """The installation exists but is suspended by the account."""

    def __init__(self, detail: str = "installation is suspended") -> None:
        super().__init__(CheckoutFailureCode.INSTALLATION_SUSPENDED, detail)


class RepoOutOfScopeError(CheckoutError):
    """The repository is not reachable through this installation's grants."""

    def __init__(
        self, detail: str = "repository is not accessible through this installation"
    ) -> None:
        super().__init__(CheckoutFailureCode.REPO_OUT_OF_SCOPE, detail)


class RefNotFoundError(CheckoutError):
    """Neither a branch, a tag, nor a known commit matched the requested ref."""

    def __init__(self, detail: str = "ref does not resolve in this repository") -> None:
        super().__init__(CheckoutFailureCode.REF_NOT_FOUND, detail)


class GitHubApiError(CheckoutError):
    """The GitHub API answered with a status the client has no better mapping for.

    Carries only the status code and the operation label -- never the response
    body or the request URL.
    """

    def __init__(self, status_code: int, operation: str) -> None:
        super().__init__(
            CheckoutFailureCode.API_ERROR,
            f"github api returned status {status_code} during {operation}",
        )
        self.status_code = status_code
        self.operation = operation


class InstallationTokenRejectedError(GitHubApiError):
    """An installation token was rejected in use (401): expired, revoked, or stale."""

    def __init__(self, operation: str) -> None:
        super().__init__(401, operation)


__all__ = [
    "CheckoutError",
    "CheckoutFailureCode",
    "CheckoutRequest",
    "GitHubApiError",
    "Installation",
    "InstallationRevokedError",
    "InstallationState",
    "InstallationSuspendedError",
    "InstallationTokenRejectedError",
    "RefNotFoundError",
    "RepoOutOfScopeError",
    "RepositoryGrant",
    "RepositoryState",
    "StagedCheckout",
]
