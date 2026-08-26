"""Tests for the unified configuration system."""

from __future__ import annotations

import pytest


def _make_settings(**env_overrides: str):
    """Create a fresh ZerothSettings with optional env var overrides applied."""
    # Import here to avoid module-level caching issues
    from zeroth.platform.config.settings import ZerothSettings

    # Temporarily patch env if needed
    return ZerothSettings(**{}) if not env_overrides else ZerothSettings()


class TestDefaultSettings:
    """Verify default settings load correctly from zeroth.yaml."""

    def test_default_settings_loads(self):
        from zeroth.platform.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.database.backend == "sqlite"
        assert settings.redis.host == "127.0.0.1"

    def test_database_backend_default_is_sqlite(self):
        from zeroth.platform.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.database.backend == "sqlite"

    def test_redis_settings_absorbs_existing_fields(self):
        """All fields from the original RedisConfig should be present in RedisSettings."""
        from zeroth.platform.config.settings import RedisSettings

        rs = RedisSettings()
        assert rs.mode == "local"
        assert rs.host == "127.0.0.1"
        assert rs.port == 6379
        assert rs.password is None
        assert rs.key_prefix == "zeroth"
        assert rs.db == 0
        assert rs.tls is False


class TestEnvVarOverrides:
    """Verify environment variables override YAML defaults."""

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ZEROTH_DATABASE__BACKEND", "postgres")
        monkeypatch.setenv("ZEROTH_DATABASE__POSTGRES_DSN", "postgresql://localhost/zeroth")
        from zeroth.platform.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.database.backend == "postgres"

    def test_nested_env_delimiter(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ZEROTH_REDIS__PORT", "6380")
        from zeroth.platform.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.redis.port == 6380


class TestGitHubAppSettings:
    """ZER-37: the GitHub App integration settings block."""

    def test_defaults_are_disabled_and_mirror_the_integration_config(self):
        from zeroth.integrations.github.config import GitHubAppConfig
        from zeroth.platform.config.settings import ZerothSettings

        github = ZerothSettings().github
        assert github.enabled is False
        assert github.app_id == ""
        assert github.api_base_url == "https://api.github.com"
        assert github.git_base_url == "https://github.com"
        assert github.private_key_secret_name == "github.app_private_key"
        assert github.webhook_secret_name == "github.webhook_secret"
        assert github.cache_dir == ""
        assert github.checkout_ttl_seconds == 900
        # The caps mirror the frozen integration config's defaults exactly.
        config = GitHubAppConfig(app_id="x")
        assert github.max_file_bytes == config.max_file_bytes
        assert github.max_total_bytes == config.max_total_bytes
        assert github.max_file_count == config.max_file_count
        assert github.api_base_url == config.api_base_url
        assert github.git_base_url == config.git_base_url
        assert github.private_key_secret_name == config.private_key_secret_name

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ZEROTH_GITHUB__ENABLED", "true")
        monkeypatch.setenv("ZEROTH_GITHUB__APP_ID", "424242")
        monkeypatch.setenv("ZEROTH_GITHUB__CHECKOUT_TTL_SECONDS", "120")
        from zeroth.platform.config.settings import ZerothSettings

        github = ZerothSettings().github
        assert github.enabled is True
        assert github.app_id == "424242"
        assert github.checkout_ttl_seconds == 120

    def test_github_field_is_hidden_from_the_reported_signature(self):
        import inspect

        from zeroth.platform.config.settings import ZerothSettings

        assert "github" in ZerothSettings.model_fields
        assert "github" not in inspect.signature(ZerothSettings).parameters
