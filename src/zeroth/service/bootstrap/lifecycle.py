"""Application lifespan for the deployment-scoped service surface.

Extracted verbatim from the app factory: the lifespan reads every component
off ``app.state.bootstrap`` and captures nothing from ``create_app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _service_runtime_lifespan(app: FastAPI):
    """Start and stop the deployment-scoped background surface.

    Everything resolves off ``app.state.bootstrap``: the bundled Regulus
    control plane, the durable run worker and its shutdown watcher, the
    queue gauge, webhook delivery, SLA checker, retention purge worker,
    the ARQ wakeup consumer, and the single-owner close of the shared
    secret provider.
    """
    # When the bundled Regulus control plane is mounted in-process, initialize
    # its own schema + seed data here: Starlette does not run a mounted
    # sub-app's startup events, so econ_plane.main's on_startup never fires.
    if getattr(app.state.bootstrap, "regulus_client", None) is not None:
        try:
            from zeroth.econ.plane.common.bootstrap import bootstrap as econ_plane_bootstrap
            from zeroth.econ.plane.config import settings as ecp_settings
            from zeroth.econ.plane.connectors.service import init_otel_metrics

            # Default-safe JWT secret for the bundled control plane. The
            # mounted plane signs its Admin tokens with ECP_JWT_SECRET, so
            # booting on the shipped placeholder 'change-me' would make those
            # tokens forgeable. The plane is now enabled by default (G1), so
            # rather than CRASH a fresh deploy we auto-generate a
            # cryptographically-strong per-process secret and use it: this is
            # STRONGER than the placeholder (tokens stay unforgeable — the
            # whole point of the v0.4 guard) and needs no operator action.
            #
            # The secret is assigned onto the module-level econ_plane settings
            # singleton BEFORE any token is minted or verified, so BOTH the
            # self-auth mint (econ.service_auth.mint_econ_service_token) AND the
            # mount's token verify (econ_plane.auth.service.decode_token) — each
            # of which reads ``settings.jwt_secret`` at call time off this same
            # singleton — observe the ephemeral value.
            #
            # Escapes (unchanged):
            #  - explicit ECP_JWT_SECRET (any non-placeholder value) -> used as-is;
            #  - ECP_ALLOW_INSECURE_JWT_SECRET=1 -> keep the literal 'change-me'
            #    placeholder (tests / deliberately-insecure local dev).
            #
            # Per-process is safe here: the ONLY client of /regulus is Zeroth's
            # own in-process self-auth in THIS process (the open token issuer is
            # blocked at the gate), so a cross-worker secret mismatch is not a
            # reachable path. Set an explicit ECP_JWT_SECRET for multi-worker or
            # persistent deployments. See SECURITY.md.
            if ecp_settings.jwt_secret == "change-me" and os.environ.get(
                "ECP_ALLOW_INSECURE_JWT_SECRET"
            ) not in ("1", "true", "yes"):
                ecp_settings.jwt_secret = secrets.token_urlsafe(32)
                logger.warning(
                    "Using an ephemeral per-process Regulus signing secret; set "
                    "ECP_JWT_SECRET for multi-worker or persistent deployments."
                )

            econ_plane_bootstrap()
            init_otel_metrics()  # no-op unless ECP_OTEL_METRICS_ENABLED
            logger.info("Initialized bundled Regulus control plane")
        except ImportError:
            pass

    worker = getattr(app.state.bootstrap, "worker", None)
    poll_task: asyncio.Task | None = None
    queue_gauge_task: asyncio.Task | None = None
    delivery_poll_task: asyncio.Task | None = None
    sla_checker_task: asyncio.Task | None = None

    if worker is not None:
        await worker.start()
        poll_task = asyncio.create_task(worker.poll_loop(), name="worker-poll")

    # Start queue depth gauge if observability is wired.
    queue_gauge = getattr(app.state.bootstrap, "queue_gauge", None)
    if queue_gauge is not None:
        queue_gauge_task = asyncio.create_task(queue_gauge.run(), name="queue-gauge")

    # Start webhook delivery worker if configured.
    delivery_worker = getattr(app.state.bootstrap, "delivery_worker", None)
    if delivery_worker is not None:
        delivery_poll_task = asyncio.create_task(
            delivery_worker.poll_loop(), name="webhook-delivery"
        )

    # Start approval SLA checker if configured.
    sla_checker = getattr(app.state.bootstrap, "sla_checker", None)
    if sla_checker is not None:
        sla_checker_task = asyncio.create_task(sla_checker.poll_loop(), name="sla-checker")

    # WS-E: start the retention purge worker when enabled (mirrors the SLA
    # checker exactly). None unless ZEROTH_RETENTION__ENABLED is true.
    retention_worker_task: asyncio.Task | None = None
    retention_worker = getattr(app.state.bootstrap, "retention_worker", None)
    if retention_worker is not None:
        retention_worker_task = asyncio.create_task(
            retention_worker.poll_loop(), name="retention-purge"
        )

    # Phase 16: ARQ wakeup consumer task.
    arq_consumer_task: asyncio.Task | None = None
    arq_pool = getattr(app.state.bootstrap, "arq_pool", None)
    if worker is not None and arq_pool is not None:
        try:
            from zeroth.platform.config.settings import get_settings
            from zeroth.platform.dispatch.arq_wakeup import run_arq_consumer

            redis_settings = get_settings().redis
            arq_consumer_task = asyncio.create_task(
                run_arq_consumer(redis_settings, worker.handle_wakeup),
                name="arq-consumer",
            )
        except ImportError:
            pass

    # Signal handling is left to uvicorn (audit B13). uvicorn installs its own
    # SIGTERM/SIGINT handlers via signal.signal() in capture_signals() BEFORE
    # the lifespan runs; a loop.add_signal_handler() here would OVERRIDE them,
    # so uvicorn's should_exit is never set, main_loop() spins forever, and
    # this post-yield teardown never runs (process hangs until SIGKILL). On
    # SIGTERM uvicorn sets should_exit -> main_loop exits -> Server.shutdown()
    # drives the lifespan past this yield, where worker.graceful_shutdown()
    # below (and all other teardown) runs.
    yield

    # Graceful shutdown -- wait for in-flight runs then release leases.
    if worker is not None:
        await worker.graceful_shutdown()

    # Cancel ARQ consumer.
    if arq_consumer_task is not None:
        arq_consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await arq_consumer_task

    # Close ARQ pool.
    if arq_pool is not None:
        with contextlib.suppress(Exception):
            await arq_pool.close()

    # Graceful shutdown: cancel the poll loop (not the executing runs).
    if poll_task is not None:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
    if queue_gauge_task is not None:
        queue_gauge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queue_gauge_task

    # Shutdown webhook delivery worker.
    if delivery_poll_task is not None:
        delivery_poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await delivery_poll_task

    # Shutdown SLA checker.
    if sla_checker_task is not None:
        sla_checker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sla_checker_task

    # Shutdown retention purge worker.
    if retention_worker_task is not None:
        retention_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_worker_task

    # Close webhook HTTP client.
    webhook_http_client = getattr(app.state.bootstrap, "webhook_http_client", None)
    if webhook_http_client is not None:
        await webhook_http_client.aclose()

    # Flush and stop Regulus telemetry transport (Pitfall 2).
    regulus_client = getattr(app.state.bootstrap, "regulus_client", None)
    if regulus_client is not None:
        regulus_client.stop()

    # Close the shared secret provider's pooled HTTP client (Vault). The
    # lifespan is the single owner of this shutdown: entrypoints and
    # bootstrap never close it, so it happens exactly once.
    secret_provider = getattr(app.state.bootstrap, "secret_provider", None)
    provider_aclose = getattr(secret_provider, "aclose", None)
    if callable(provider_aclose):
        close_result = provider_aclose()
        if inspect.isawaitable(close_result):
            await close_result


@asynccontextmanager
async def service_lifespan(app: FastAPI):
    """Own the gateway transport around the existing service lifecycle."""
    try:
        async with _service_runtime_lifespan(app):
            yield
    finally:
        gateway_transport = getattr(app.state.bootstrap, "langgraph_gateway_transport", None)
        gateway_aclose = getattr(gateway_transport, "aclose", None)
        if callable(gateway_aclose):
            close_result = gateway_aclose()
            if inspect.isawaitable(close_result):
                await close_result
