"""Tests for the unified configuration system."""

from __future__ import annotations

import pytest


def _make_settings(**env_overrides: str):
    """Create a fresh ZerothSettings with optional env var overrides applied."""
    # Import here to avoid module-level caching issues
    from zeroth.config.settings import ZerothSettings

    # Temporarily patch env if needed
    return ZerothSettings(**{}) if not env_overrides else ZerothSettings()


class TestDefaultSettings:
    """Verify default settings load correctly from zeroth.yaml."""

    def test_default_settings_loads(self):
        from zeroth.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.database.backend == "sqlite"
        assert settings.redis.host == "127.0.0.1"

    def test_database_backend_default_is_sqlite(self):
        from zeroth.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.database.backend == "sqlite"

    def test_redis_settings_absorbs_existing_fields(self):
        """All fields from the original RedisConfig should be present in RedisSettings."""
        from zeroth.config.settings import RedisSettings

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
        from zeroth.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.database.backend == "postgres"

    def test_nested_env_delimiter(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ZEROTH_REDIS__PORT", "6380")
        from zeroth.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert settings.redis.port == 6380
