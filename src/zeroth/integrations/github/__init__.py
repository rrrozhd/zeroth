"""GitHub App integration: verified, pinned, credential-scoped repo checkouts.

Public surface of the ZER-37 integrations core: configuration, domain models
and the typed failure vocabulary, App JWT issuance, the REST client, the
installation-token broker with lifetime redaction, the object-level tree
policy, the single git subprocess seam, the checkout pipeline, and the local
materializer.
"""

from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.checkout import CheckoutService, CheckoutStateStore
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.git_cli import (
    CompletedGit,
    GitInvocation,
    build_scrubbed_env,
)
from zeroth.integrations.github.materializer import LocalCheckoutMaterializer
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    CheckoutRequest,
    GitHubApiError,
    Installation,
    InstallationRevokedError,
    InstallationState,
    InstallationSuspendedError,
    InstallationTokenRejectedError,
    RefNotFoundError,
    RepoOutOfScopeError,
    RepositoryGrant,
    RepositoryState,
    StagedCheckout,
)
from zeroth.integrations.github.token_broker import CredentialLease, InstallationTokenBroker
from zeroth.integrations.github.tree_policy import (
    TreeEntry,
    enforce_tree_policy,
    is_lfs_pointer,
    lfs_candidate_blobs,
    parse_cat_file_batch,
    parse_ls_tree,
)

__all__ = [
    "AppJwtIssuer",
    "CheckoutError",
    "CheckoutFailureCode",
    "CheckoutRequest",
    "CheckoutService",
    "CheckoutStateStore",
    "CompletedGit",
    "CredentialLease",
    "GitHubApiError",
    "GitHubAppClient",
    "GitHubAppConfig",
    "GitInvocation",
    "Installation",
    "InstallationRevokedError",
    "InstallationState",
    "InstallationSuspendedError",
    "InstallationTokenBroker",
    "InstallationTokenRejectedError",
    "LocalCheckoutMaterializer",
    "RefNotFoundError",
    "RepoOutOfScopeError",
    "RepositoryGrant",
    "RepositoryState",
    "StagedCheckout",
    "TreeEntry",
    "build_scrubbed_env",
    "enforce_tree_policy",
    "is_lfs_pointer",
    "lfs_candidate_blobs",
    "parse_cat_file_batch",
    "parse_ls_tree",
]
