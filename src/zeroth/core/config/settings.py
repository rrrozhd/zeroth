"""Legacy import path for :mod:`zeroth.platform.config.settings`.

This module defines nothing of its own; it republishes the settings model
namespace from its canonical platform location for compatibility. Import from
``zeroth.platform.config.settings`` instead (see
docs/backend-import-migration.md).
"""

from zeroth.platform.config.settings import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    ApprovalNotificationSettings,
    ApprovalSLASettings,
    AuthSettings,
    ChromaSettings,
    DatabaseSettings,
    DispatchSettings,
    ElasticsearchSettings,
    EmailNotificationSettings,
    MemorySettings,
    PgvectorSettings,
    PolicySettings,
    ProvenanceSigningSettings,
    RedisSettings,
    RetentionSettings,
    SandboxSettings,
    SecretsSettings,
    SlackNotificationSettings,
    TLSSettings,
    TracingSettings,
    WebhookSettings,
    ZerothSettings,
    get_settings,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_EMBEDDING_MODEL",
    "ApprovalNotificationSettings",
    "ApprovalSLASettings",
    "AuthSettings",
    "ChromaSettings",
    "DatabaseSettings",
    "DispatchSettings",
    "EmailNotificationSettings",
    "ElasticsearchSettings",
    "MemorySettings",
    "PgvectorSettings",
    "PolicySettings",
    "ProvenanceSigningSettings",
    "RedisSettings",
    "RetentionSettings",
    "SandboxSettings",
    "SecretsSettings",
    "SlackNotificationSettings",
    "TLSSettings",
    "TracingSettings",
    "WebhookSettings",
    "ZerothSettings",
    "get_settings",
]
