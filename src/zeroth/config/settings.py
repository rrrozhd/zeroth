"""Zeroth unified settings model.

Configuration is loaded with the following priority (highest wins):
1. Environment variables with ZEROTH_ prefix
2. .env file values
3. zeroth.yaml defaults
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class DatabaseSettings(BaseModel):
    """Database backend configuration."""

    backend: str = "sqlite"
    sqlite_path: str = "zeroth.db"
    postgres_dsn: SecretStr | None = None
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10
    encryption_key: SecretStr | None = None


class RedisSettings(BaseModel):
    """Redis connection settings, absorbing the existing RedisConfig fields."""

    mode: str = "local"
    host: str = "127.0.0.1"
    port: int = 6379
    password: SecretStr | None = None
    key_prefix: str = "zeroth"
    db: int = 0
    tls: bool = False


class AuthSettings(BaseModel):
    """Service authentication settings."""

    api_keys_json: str | None = None
    bearer_json: str | None = None


class ZerothSettings(BaseSettings):
    """Top-level settings for the Zeroth platform.

    Loads from environment variables (ZEROTH_ prefix), .env file,
    and zeroth.yaml in that priority order.
    """

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="ZEROTH_",
        env_nested_delimiter="__",
        yaml_file="zeroth.yaml",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
        **kwargs: Any,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Return sources in priority order: env vars > .env > YAML defaults."""
        return (
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


_settings_singleton: ZerothSettings | None = None


def get_settings() -> ZerothSettings:
    """Return the cached ZerothSettings singleton, creating it on first call."""
    global _settings_singleton  # noqa: PLW0603
    if _settings_singleton is None:
        _settings_singleton = ZerothSettings()
    return _settings_singleton
