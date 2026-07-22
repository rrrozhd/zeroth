"""Zeroth unified settings model.

Configuration is loaded with the following priority (highest wins):
1. Environment variables with ZEROTH_ prefix
2. .env file values
3. zeroth.yaml defaults
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from zeroth.platform.artifacts.models import ArtifactStoreSettings
from zeroth.platform.config.models import HttpClientSettings, RegulusSettings

# Default embedding model used across memory connectors. Centralized here so a
# single upgrade flows into pgvector, chroma, and bootstrap wiring.
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 1536


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

    # The service reads these through ServiceAuthConfig.from_env (service/auth.py),
    # which uses flat ZEROTH_SERVICE_* names rather than the nested ZEROTH_AUTH__*
    # scheme — the `env` override keeps the generated configuration reference
    # pointing at the names the service actually honors.
    api_keys_json: SecretStr | None = Field(
        default=None,
        description=(
            'JSON **list** of credential objects: `[{"credential_id", "secret", '
            '"subject", "roles", "tenant_id"?, "workspace_id"?}]`'
        ),
        json_schema_extra={"env": "ZEROTH_SERVICE_API_KEYS_JSON"},
    )
    bearer_json: SecretStr | None = Field(
        default=None,
        description="JSON bearer/JWT verification config (issuer, audience, jwks)",
        json_schema_extra={"env": "ZEROTH_SERVICE_BEARER_JSON"},
    )


class MemorySettings(BaseModel):
    """Memory backend configuration."""

    default_connector: str = "ephemeral"
    redis_kv_prefix: str = "zeroth:mem:kv"
    redis_thread_prefix: str = "zeroth:mem:thread"


class PgvectorSettings(BaseModel):
    """Pgvector-based vector memory configuration."""

    enabled: bool = False
    table_name: str = "zeroth_memory_vectors"
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS


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


class SlackNotificationSettings(BaseModel):
    webhook_url: str | None = None
    timeout: float = 10.0


class EmailNotificationSettings(BaseModel):
    smtp_host: str | None = None
    smtp_port: int = 587
    from_address: str | None = None
    to_addresses: list[str] = Field(default_factory=list)
    username: str | None = None
    password: SecretStr | None = None
    use_tls: bool = True
    timeout: float = 10.0


class ApprovalNotificationSettings(BaseModel):
    enabled: bool = False
    slack: SlackNotificationSettings = Field(default_factory=SlackNotificationSettings)
    email: EmailNotificationSettings = Field(default_factory=EmailNotificationSettings)


class DispatchSettings(BaseModel):
    """Dispatch and horizontal scaling configuration."""

    arq_enabled: bool = False
    shutdown_timeout: float = 30.0
    poll_interval: float = 0.5


class TLSSettings(BaseModel):
    """TLS configuration for direct uvicorn SSL."""

    certfile: str | None = None
    keyfile: str | None = None


class TracingSettings(BaseModel):
    """OpenTelemetry tracing configuration (requires the ``otel`` extra)."""

    enabled: bool = False
    service_name: str = "zeroth-core"
    # OTLP/HTTP collector endpoint; when unset the exporter falls back to its own
    # OTEL_EXPORTER_OTLP_ENDPOINT environment variable.
    otlp_endpoint: str | None = None


class SecretsSettings(BaseModel):
    """Secret-resolution backend configuration (WS-F).

    Selects how the process resolves LLM keys, the WS-D signing key, HTTP auth
    secrets, and execution-unit env vars. ``backend='env'`` (the dev default)
    reads process/injected environment variables; ``backend='vault'`` reads a
    HashiCorp Vault KV-v2 mount.

    When ``allow_env_fallback`` is False, a secret that the provider cannot
    resolve FAILS the call (fail-closed) instead of letting the underlying
    library silently read a process-global environment variable.
    """

    backend: str = "env"  # env | vault
    allow_env_fallback: bool = True

    # Vault connection / auth (only consulted when backend == 'vault').
    vault_addr: str | None = None
    vault_mount: str = "secret"
    vault_role_id: SecretStr | None = None
    vault_secret_id: SecretStr | None = None
    vault_token: SecretStr | None = None
    # Seconds a resolved value stays cached before a re-fetch.
    vault_cache_ttl: float = 300.0

    # Optional provider-prefix -> logical-secret-name overrides, e.g.
    # ``{"openai": "llm.openai_prod"}``. When a provider prefix is absent the
    # adapter derives the logical name as ``llm.<provider>``.
    llm_key_map: dict[str, str] = Field(default_factory=dict)


class ProvenanceSigningSettings(BaseModel):
    """Keyed provenance-signing configuration (WS-D).

    Controls how deployment attestations and audit-chain records are signed on
    top of their unkeyed SHA-256 digests. The signing key resolves through the
    SAME shared :class:`SecretProvider` as every other secret surface (WS-F),
    never a second env reader.

    * ``mode='env'`` (dev default) — HMAC-SHA256 keyed from the secret provider.
      Key-custody-bounded, NOT non-repudiation. When no key is configured the
      process runs unsigned-legacy (records verify as neither signed nor
      tampered) after a startup warning.
    * ``mode='kms'`` — Ed25519; ``signing_key_ref`` resolves the private key and
      ``public_keys_json`` carries the verify keys. Backs the strong claim.
      Missing key material fails closed at startup.
    * ``mode='off'`` — signing disabled; records stay unsigned-legacy.
    """

    mode: str = "env"  # env | kms | off
    # Advisory only: the EFFECTIVE algorithm is derived from ``mode`` — env ->
    # HS256, kms -> Ed25519. It is surfaced for documentation/telemetry; setting
    # it inconsistently with ``mode`` (e.g. mode='env', algorithm='Ed25519') does
    # NOT change the algorithm used. See docs/provenance-trust-model.md.
    algorithm: str = "HS256"
    # Logical secret name resolved via the shared SecretProvider (defaults to
    # 'signing.deployment' when unset).
    signing_key_ref: str | None = None
    signing_key_id: str = "dev-local"
    # JSON object of ``{key_id: public-key-hex}`` for the Ed25519 verify path.
    public_keys_json: SecretStr | None = None


class RetentionSettings(BaseModel):
    """WS-E retention / right-to-erasure configuration.

    ``enabled`` gates the background :class:`RetentionPurgeWorker`; when False the
    worker is not started and no TTL purge runs (explicit erasure via the API
    still works). ``worker_poll_interval`` is the seconds between purge sweeps.
    ``default_audit_ttl_seconds`` / ``default_run_ttl_seconds`` seed the
    system-default retention policy when a tenant has none; ``None`` means "keep
    forever" (no automatic purge for that surface).
    """

    enabled: bool = False
    # Interval, not a persisted TTL — stays a positive float.
    worker_poll_interval: float = Field(default=3600.0, gt=0)
    # Whole positive seconds, like the per-tenant policy TTLs.
    default_audit_ttl_seconds: int | None = Field(default=None, ge=1)
    default_run_ttl_seconds: int | None = Field(default=None, ge=1)


class PolicySettings(BaseModel):
    """Behavioral capability-enforcement configuration (WS-C).

    When ``enforce_capabilities`` is True (the default), the service wires a
    default :class:`PolicyGuard` into the orchestrator so a node's declared
    ``capability_bindings`` become a behavioral, fail-closed gate: an agent that
    invokes a tool or touches memory without the matching capability granted is
    DENIED, and the sandbox network/secret gate on executable units is revived.

    Setting it False leaves ``policy_guard`` unset — capabilities stay purely
    advisory and no gate runs. This is the deliberate escape hatch for operators
    who have not yet backfilled ``capability_bindings`` on their graphs; it is
    NOT how the gate is bypassed on a per-run basis (there is no per-run bypass).

    ``local_network_strict`` reserves the STRICT-mode local-sandbox refusal knob;
    STANDARD already refuses network-bearing local nodes (see sandbox.py), so the
    default carries the strict posture without extra wiring.
    """

    enforce_capabilities: bool = True
    local_network_strict: bool = True


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
    approval_notifications: ApprovalNotificationSettings = Field(
        default_factory=ApprovalNotificationSettings
    )
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)
    tls: TLSSettings = Field(default_factory=TLSSettings)
    artifact_store: ArtifactStoreSettings = Field(default_factory=ArtifactStoreSettings)
    http_client: HttpClientSettings = Field(default_factory=HttpClientSettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    provenance: ProvenanceSigningSettings = Field(default_factory=ProvenanceSigningSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

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
        """Return sources in priority order: init kwargs > env > .env > YAML."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


_settings_parameters = inspect.signature(ZerothSettings).parameters
ZerothSettings.__signature__ = inspect.signature(ZerothSettings).replace(
    parameters=[
        parameter
        for name, parameter in _settings_parameters.items()
        if name != "approval_notifications"
    ]
)


_settings_singleton: ZerothSettings | None = None


def get_settings() -> ZerothSettings:
    """Return the cached ZerothSettings singleton, creating it on first call."""
    global _settings_singleton  # noqa: PLW0603
    if _settings_singleton is None:
        _settings_singleton = ZerothSettings()
    return _settings_singleton
