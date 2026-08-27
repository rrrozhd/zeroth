"""Static configuration for the GitHub App integration.

A frozen value object the service layer constructs and hands down; nothing in
this package reads process settings or the environment on its own. The private
key itself never lives here -- only the logical secret name the
:class:`~zeroth.platform.secrets.provider.SecretProvider` resolves at signing
time.
"""

from __future__ import annotations

from dataclasses import dataclass

_MIB = 1024 * 1024


@dataclass(frozen=True)
class GitHubAppConfig:
    """Everything the GitHub App integration needs to know at construction time.

    Attributes:
        app_id: The GitHub App's numeric identifier, as a string (JWT issuer).
        api_base_url: REST API origin; overridden in tests and for GHES.
        git_base_url: Origin git remotes are built from
            (``{git_base_url}/{owner}/{name}.git``).
        private_key_secret_name: Logical secret name the App's PEM private key
            resolves under.
        max_file_bytes: Cap on any single file in a staged checkout.
        max_total_bytes: Cap on the byte total of a staged checkout.
        max_file_count: Cap on the number of files in a staged checkout.
        allow_http_git: Tests only -- when True, plain ``http://`` loopback git
            remotes are permitted via ``GIT_ALLOW_PROTOCOL``.
    """

    app_id: str
    api_base_url: str = "https://api.github.com"
    git_base_url: str = "https://github.com"
    private_key_secret_name: str = "github.app_private_key"
    max_file_bytes: int = 50 * _MIB
    max_total_bytes: int = 500 * _MIB
    max_file_count: int = 50_000
    allow_http_git: bool = False


__all__ = ["GitHubAppConfig"]
