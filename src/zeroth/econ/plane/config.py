from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Economic Control Plane"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    database_url: str = "sqlite+pysqlite:///./econ_plane.db"
    stat_cost_engine: bool = False
    stat_value_engine: bool = False
    confidence_gating_strict: bool = True
    confidence_gate_level: float = 0.95
    confidence_gate_rel_width: float = 0.30
    default_tenant_id: str = "default"
    insecure_public_token_issuer_enabled: bool = False
    service_principal_subject: str = "zeroth-service"
    service_principal_email: str = "zeroth-service@example.com"
    service_principal_tenant_id: str = "default"
    service_principal_workspace_id: str | None = None
    service_principal_roles: str = "Admin"
    outcome_pipeline_v2: bool = True
    experiment_routing: bool = True
    shadow_eval: bool = True
    policy_action_log_v2: bool = True
    protected_capability_guard_strict: bool = True
    strict_join_key_enforcement: bool = True
    request_log_enabled: bool = False
    request_log_level: str = "DEBUG"
    request_log_sample_rate: float = 0.10
    redis_host: str = "localhost"
    redis_port: int = 6379
    connectors_enabled: bool = False
    # The one directory the warehouse-file adapters may write into. Their
    # ``spool_path`` is operator-supplied and used to be opened verbatim, so it
    # could name any file the process could write. See
    # zeroth.platform.primitives.boundary.confine_path.
    connector_spool_root: str = "./.zeroth/connector-spool"
    connector_worker_batch_size: int = 100
    connector_max_attempts: int = 8
    connector_backoff_base_s: int = 2
    prometheus_enabled: bool = True
    otel_metrics_enabled: bool = False
    otel_metrics_otlp_endpoint: str = ""
    # Platform-emitted execution telemetry names its capability by node_id and its
    # implementation by model_name — rows the bundled deploy never pre-registers.
    # With this on (default), the EXECUTION ingest path auto-upserts those rows so
    # cost events land instead of 422-ing; the OUTCOME path keeps the strict guard.
    auto_register_ingest_capabilities: bool = True

    model_config = SettingsConfigDict(env_prefix="ECP_", case_sensitive=False)


settings = Settings()


class EconConfigError(RuntimeError):
    pass


def validate_startup_settings() -> None:
    if not settings.jwt_secret.strip() or settings.jwt_secret == "change-me":
        raise EconConfigError("ECP_JWT_SECRET must be configured before standalone startup")
