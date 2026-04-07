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

from zeroth.econ.models import RegulusSettings


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


class MemorySettings(BaseModel):
    """Memory backend configuration."""

    default_connector: str = "ephemeral"
    redis_kv_prefix: str = "zeroth:mem:kv"
    redis_thread_prefix: str = "zeroth:mem:thread"


class PgvectorSettings(BaseModel):
    """Pgvector-based vector memory configuration."""

    enabled: bool = False
    table_name: str = "zeroth_memory_vectors"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536


class ChromaSettings(BaseModel):
    """ChromaDB vector memory configuration."""

    enabled: bool = False
    host: str = "localhost"
    port: int = 8000
    collection_prefix: str = "zeroth_memory"


class ElasticsearchSettings(BaseModel):
    """Elasticsearch memory backend configuration."""

    enabled: bool = False
    hosts: list[str] = Field(default_factory=lambda: ["http://localhost:9200"])
    index_prefix: str = "zeroth_memory"


class SandboxSettings(BaseModel):
    """Sandbox execution backend configuration."""

    backend: str = "local"  # local, docker, auto, sidecar
    sidecar_url: str = "http://sandbox-sidecar:8001"
    docker_container_name: str = "zeroth-sandbox"
    docker_binary: str = "docker"


class WebhookSettings(BaseModel):
    """Webhook delivery system configuration."""

    enabled: bool = True
    delivery_poll_interval: float = 2.0
    delivery_timeout: float = 10.0
    max_delivery_concurrency: int = 16
    default_max_retries: int = 5
    retry_base_delay: float = 1.0
    retry_max_delay: float = 300.0


class ApprovalSLASettings(BaseModel):
    """Approval SLA timeout and escalation configuration."""

    enabled: bool = True
    checker_poll_interval: float = 10.0


class DispatchSettings(BaseModel):
    """Distributed dispatch configuration."""

    arq_enabled: bool = False
    shutdown_timeout: float = 30.0
    poll_interval: float = 0.5


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
    regulus: RegulusSettings = Field(default_factory=RegulusSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    pgvector: PgvectorSettings = Field(default_factory=PgvectorSettings)
    chroma: ChromaSettings = Field(default_factory=ChromaSettings)
    elasticsearch: ElasticsearchSettings = Field(default_factory=ElasticsearchSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    approval_sla: ApprovalSLASettings = Field(default_factory=ApprovalSLASettings)
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)

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
