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

# The gateway transport is an injected client; its close gets a bound so the
# audit drain behind it cannot be postponed indefinitely.
TRANSPORT_CLOSE_TIMEOUT_SECONDS = 5.0

# Tasks cancelled at a shutdown bound and deliberately not awaited. Held only
# so the loop does not report them as destroyed while pending.
_ABANDONED_TASKS: set[asyncio.Task] = set()

# The fixed code an abandoned task's eventual failure is logged under. The task
# is an injected client's, so its exception message is outside every check this
# process makes over what may reach the log stream.
_ABANDONED_TASK_FAILED = "abandoned_task_failed"


def _release_abandoned_task(task: asyncio.Task) -> None:
    """Retire an abandoned shutdown task, consuming the result nobody awaited.

    Releasing a task without ever retrieving its exception hands the reporting
    to asyncio, whose default handler prints "Task exception was never
    retrieved" together with the exception's full message and traceback --
    an injected transport's message, on a path this process otherwise keeps
    free of foreign text. Consuming it here makes the failure a fixed code and
    an exception type instead.

    Args:
        task: The finished task that was abandoned at a shutdown bound.
    """
    _ABANDONED_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "abandoned shutdown task failed code=%s task=%s exception_type=%s",
            _ABANDONED_TASK_FAILED,
            task.get_name(),
            type(exc).__name__,
        )


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


async def _drain_audit_delivery(app: FastAPI, *, timeout: float = 5.0) -> None:
    """Drain the bounded audit-delivery stage and log whatever it could not persist.

    Runs after the gateway transport has stopped -- so no further terminal
    event can be submitted -- and while the audit repository is still open,
    because the delivery worker writes through it. Skipping the drain would
    abandon every event still queued when the process stopped; discarding the
    report would lose the one list naming them.

    Args:
        app: The application whose ``state.bootstrap`` owns the delivery stage.
        timeout: Seconds the bounded shutdown may take, in total.
    """
    queue = getattr(app.state.bootstrap, "audit_delivery_queue", None)
    aclose = getattr(queue, "aclose", None)
    if not callable(aclose):
        return
    try:
        report = await aclose(timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the rest of teardown must still run
        # Type only, never the message: the delivery stage deliberately logs
        # failure codes rather than exception text, which can carry a payload.
        logger.error("audit delivery drain failed exception_type=%s", type(exc).__name__)
        return
    undelivered = getattr(report, "undelivered_audit_ids", ())
    if undelivered:
        logger.error(
            "audit delivery shutdown left %d event(s) undelivered: %s",
            len(undelivered),
            ", ".join(undelivered),
        )


async def _close_gateway_transport(app: FastAPI, *, timeout: float) -> None:
    """Stop the gateway transport within a bound, whatever it does about it.

    The close is an injected client's, so it can hang; awaiting it unbounded put
    the audit drain behind something with no deadline. An overrunning close is
    cancelled and released rather than waited on, exactly as the delivery stage
    treats a writer that ignores cancellation.

    Args:
        app: The application whose ``state.bootstrap`` owns the transport.
        timeout: Seconds the close may take before it is abandoned.

    Raises:
        BaseException: Whatever the transport's own close raised, so the caller
            can preserve it -- after the drain has run.
    """
    transport = getattr(app.state.bootstrap, "langgraph_gateway_transport", None)
    aclose = getattr(transport, "aclose", None)
    if not callable(aclose):
        return
    close_result = aclose()
    if not inspect.isawaitable(close_result):
        return
    task = asyncio.ensure_future(close_result)
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        _ABANDONED_TASKS.add(task)
        task.add_done_callback(_release_abandoned_task)
        logger.error("gateway transport close exceeded %.1fs; abandoning it", timeout)
        return
    exc = task.exception()
    if exc is not None:
        raise exc


@asynccontextmanager
async def service_lifespan(app: FastAPI):
    """Own the gateway transport and the audit drain around the service lifecycle.

    Both run *inside* the runtime lifespan, in an unconditional ``finally``, so
    they are the first thing that happens when serving stops and the whole of
    the runtime teardown is still ahead of them. Sitting outside it was a bound
    on paper only: the drain then queued behind every post-yield await that
    teardown performs -- a run worker's graceful shutdown, the ARQ consumer and
    pool, the webhook client, the secret provider -- none of which has a
    deadline. Any one of them hanging postponed the drain indefinitely, which is
    the same lost backlog R10 exists to prevent, reached by a slower route.

    The two keep their order relative to each other: the transport stops first,
    so a still-serving gateway is not submitting into a queue that is already
    draining; the drain then runs whatever the close did, because a transport
    that failed to stop is not a reason to abandon evidence already accepted.
    A transport error is preserved and re-raised, but only after the drain has
    had its bounded turn *and* the runtime has torn down -- raising it any
    earlier would skip that teardown entirely.

    A startup that fails never reaches the body, so the shutdown inside it never
    runs; the outer guard is what still closes a transport the bootstrap had
    already built, exactly once.
    """
    transport_error: BaseException | None = None
    stopped = False
    try:
        async with _service_runtime_lifespan(app):
            try:
                yield
            finally:
                stopped = True
                transport_error = await _stop_gateway_and_drain_audit(app)
    finally:
        if not stopped:
            failure = await _stop_gateway_and_drain_audit(app)
            if failure is not None:
                # Never raised: the startup failure already propagating here is
                # the one an operator needs, and it is the more informative of
                # the two.
                logger.error(
                    "gateway transport close failed during a failed startup exception_type=%s",
                    type(failure).__name__,
                )
    if transport_error is not None:
        raise transport_error


async def _stop_gateway_and_drain_audit(app: FastAPI) -> BaseException | None:
    """Stop the gateway transport, then drain the audit stage, both bounded.

    Args:
        app: The application whose ``state.bootstrap`` owns both.

    Returns:
        Whatever the transport's close raised, for the caller to re-raise once
        the rest of teardown has run. The drain runs either way: a transport
        that failed to stop is not a reason to abandon evidence already
        accepted, and is exactly when the backlog is least likely to be empty.
    """
    transport_error: BaseException | None = None
    try:
        await _close_gateway_transport(app, timeout=TRANSPORT_CLOSE_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the drain must run either way, and
        # the error is handed back to the caller rather than dropped.
        transport_error = exc
    finally:
        await _drain_audit_delivery(app)
    return transport_error
