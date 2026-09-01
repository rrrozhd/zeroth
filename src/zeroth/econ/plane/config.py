from urllib.parse import urlsplit

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
    # Hosted deployments enable plan/usage enforcement. Self-hosted installs
    # keep the same open-source APIs without requiring subscription records.
    cloud_entitlements_enabled: bool = False
    cloud_scheduler_enabled: bool = False
    cloud_scheduler_interval_seconds: float = 60.0
    # Hosted identity is optional so the open-source plane and SDK keep their
    # current dependency and startup surface. These are required only when the
    # AuthKit routes are enabled by a hosted deployment.
    workos_authkit_enabled: bool = False
    workos_client_id: str = ""
    workos_api_key: str = ""
    workos_redirect_uri: str = ""
    workos_cookie_password: str = ""
    cloud_browser_origin: str = ""
    paddle_billing_enabled: bool = False
    paddle_api_key: str = ""
    paddle_webhook_secret: str = ""
    paddle_sandbox: bool = True
    paddle_solo_price_id: str = ""
    paddle_team_price_id: str = ""

    model_config = SettingsConfigDict(env_prefix="ECP_", case_sensitive=False)


settings = Settings()


class EconConfigError(RuntimeError):
    pass


def validate_startup_settings() -> None:
    if not settings.jwt_secret.strip() or settings.jwt_secret == "change-me":
        raise EconConfigError("ECP_JWT_SECRET must be configured before standalone startup")
    if settings.cloud_scheduler_enabled and not settings.cloud_entitlements_enabled:
        raise EconConfigError("cloud scheduler requires cloud entitlements")
    if settings.cloud_scheduler_interval_seconds <= 0:
        raise EconConfigError("ECP_CLOUD_SCHEDULER_INTERVAL_SECONDS must be positive")
    if settings.workos_authkit_enabled:
        missing = [
            env_name
            for env_name, value in (
                ("ECP_WORKOS_CLIENT_ID", settings.workos_client_id),
                ("ECP_WORKOS_API_KEY", settings.workos_api_key),
                ("ECP_WORKOS_REDIRECT_URI", settings.workos_redirect_uri),
                ("ECP_WORKOS_COOKIE_PASSWORD", settings.workos_cookie_password),
                ("ECP_CLOUD_BROWSER_ORIGIN", settings.cloud_browser_origin),
            )
            if not value.strip()
        ]
        if missing:
            raise EconConfigError(
                "WorkOS AuthKit is enabled but required settings are missing: "
                + ", ".join(missing)
            )
        if len(settings.workos_cookie_password) < 32:
            raise EconConfigError("ECP_WORKOS_COOKIE_PASSWORD must be at least 32 characters")
        redirect = urlsplit(settings.workos_redirect_uri)
        browser = urlsplit(settings.cloud_browser_origin)
        redirect_origin = f"{redirect.scheme}://{redirect.netloc}"
        clean_browser_origin = (
            browser.scheme == "https"
            and bool(browser.netloc)
            and browser.username is None
            and browser.password is None
            and browser.path in {"", "/"}
            and not browser.query
            and not browser.fragment
        )
        if (
            redirect.scheme != "https"
            or not redirect.netloc
            or not clean_browser_origin
            or settings.cloud_browser_origin.rstrip("/") != redirect_origin
        ):
            raise EconConfigError(
                "ECP_CLOUD_BROWSER_ORIGIN must be the same HTTPS origin as "
                "ECP_WORKOS_REDIRECT_URI"
            )
    if settings.paddle_billing_enabled:
        missing = [
            env_name
            for env_name, value in (
                ("ECP_PADDLE_API_KEY", settings.paddle_api_key),
                ("ECP_PADDLE_WEBHOOK_SECRET", settings.paddle_webhook_secret),
                ("ECP_PADDLE_SOLO_PRICE_ID", settings.paddle_solo_price_id),
            )
            if not value.strip()
        ]
        if missing:
            raise EconConfigError(
                "Paddle billing is enabled but required settings are missing: "
                + ", ".join(missing)
            )
