"""Legacy import path for :mod:`zeroth.platform.config.settings`.

This module defines nothing of its own; it republishes the settings model
namespace from its canonical platform location for compatibility. Import from
``zeroth.platform.config.settings`` instead (see
docs/backend-import-migration.md).
"""

from zeroth.platform.config.settings import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    ApprovalSLASettings,
    AuthSettings,
    ChromaSettings,
    DatabaseSettings,
    DispatchSettings,
    ElasticsearchSettings,
    MemorySettings,
    PgvectorSettings,
    PolicySettings,
    ProvenanceSigningSettings,
    RedisSettings,
    RetentionSettings,
    SandboxSettings,
    SecretsSettings,
    TLSSettings,
    TracingSettings,
    WebhookSettings,
    ZerothSettings,
    get_settings,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "DEFAULT_EMBEDDING_MODEL",
    "ApprovalSLASettings",
    "AuthSettings",
    "ChromaSettings",
    "DatabaseSettings",
    "DispatchSettings",
    "ElasticsearchSettings",
    "MemorySettings",
    "PgvectorSettings",
    "PolicySettings",
    "ProvenanceSigningSettings",
    "RedisSettings",
    "RetentionSettings",
    "SandboxSettings",
    "SecretsSettings",
    "TLSSettings",
    "TracingSettings",
    "WebhookSettings",
    "ZerothSettings",
    "get_settings",
]
