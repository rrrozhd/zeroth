"""Service-layer surface for the GitHub App integration (ZER-37).

Persistence for installations, repository grants, and webhook delivery
dedup; the tenant-facing integration service; the HMAC-authenticated webhook
receiver; and the maintenance janitor. All construction happens in the
service bootstrap, gated on ``settings.github.enabled``.
"""

from zeroth.service.github.janitor import GitHubMaintenanceWorker
from zeroth.service.github.repository import (
    GitHubInstallationRecord,
    GitHubRepositoryRecord,
    SQLiteGitHubRepository,
)
from zeroth.service.github.service import GitHubIntegrationService
from zeroth.service.github.webhook_receiver import register_github_webhook_route

__all__ = [
    "GitHubInstallationRecord",
    "GitHubIntegrationService",
    "GitHubMaintenanceWorker",
    "GitHubRepositoryRecord",
    "SQLiteGitHubRepository",
    "register_github_webhook_route",
]
