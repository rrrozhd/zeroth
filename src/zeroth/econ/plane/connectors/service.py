from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

from sqlalchemy import and_, func, select, update

from zeroth.econ.plane.config import settings
from zeroth.econ.plane.connectors.models import (
    ConnectorConfig,
    ConnectorDeliveryLog,
    ConnectorOutbox,
)
from zeroth.econ.plane.connectors.registry import build_adapter_registry
from zeroth.econ.plane.connectors.schemas import (
    ConnectorEventEnvelope,
    ConnectorHealthResult,
    ConnectorSendResult,
)
from zeroth.econ.plane.scoped_session import ScopedSession

logger = logging.getLogger(__name__)
_OTEL_COUNTERS: dict[str, Any] = {}
_OTEL_ENABLED = False
# Statuses an outbox row can be claimed from.  Named once so the candidate
# SELECT and the conditional claim UPDATE cannot drift apart -- if they did,
# the claim would stop being a guard.
_CLAIMABLE_STATUSES = ("PENDING", "FAILED")


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("connector persistence requires an exact ScopedSession")
    return db


def _bound_tenant(db: ScopedSession) -> str:
    if db.scope is None:
        raise ValueError("connector persistence requires a tenant-bound scope")
    return db.scope.tenant_id


def _require_requested_tenant(db: ScopedSession, tenant_id: str) -> str:
    bound_tenant = _bound_tenant(db)
    normalized = "default" if tenant_id == "tenant_default" else tenant_id
    if normalized != bound_tenant:
        raise ValueError("tenant ownership does not match the bound scope")
    return bound_tenant


def _utcnow() -> datetime:
    return datetime.now(UTC)


def init_otel_metrics() -> None:
    global _OTEL_ENABLED
    if not settings.otel_metrics_enabled:
        return
    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        endpoint = settings.otel_metrics_otlp_endpoint or None
        exporter = OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10000)
        provider = MeterProvider(metric_readers=[reader])
        metrics.set_meter_provider(provider)
        meter = metrics.get_meter("econ_plane.connectors")
        _OTEL_COUNTERS["execution"] = meter.create_counter("ecp_execution_events_total")
        _OTEL_COUNTERS["outcome"] = meter.create_counter("ecp_outcomes_total")
        _OTEL_COUNTERS["evaluation"] = meter.create_counter("ecp_evaluations_completed_total")
        _OTEL_COUNTERS["policy"] = meter.create_counter("ecp_policy_actions_total")
        _OTEL_ENABLED = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("otel metrics init failed: %s", exc)
        _OTEL_ENABLED = False


def _otel_add(counter_key: str, amount: int = 1, attrs: dict[str, str] | None = None) -> None:
    if not _OTEL_ENABLED:
        return
    counter = _OTEL_COUNTERS.get(counter_key)
    if counter is None:
        return
    counter.add(amount, attributes=attrs or {})


def _max_attempts() -> int:
    return int(getattr(settings, "connector_max_attempts", 8))


def _backoff_base() -> int:
    return int(getattr(settings, "connector_backoff_base_s", 2))


def _next_attempt(attempts: int) -> datetime:
    delay = _backoff_base() ** max(1, attempts)
    return _utcnow() + timedelta(seconds=min(delay, 3600))


def _adapter_registry() -> dict[str, Any]:
    return build_adapter_registry()


def _event_payload(envelope: ConnectorEventEnvelope) -> dict[str, Any]:
    return envelope.model_dump(mode="json")


def _normalize_outbox_status(status: str | None) -> str | None:
    if not status:
        return None
    return status.upper()


def list_connector_configs(db: ScopedSession, tenant_id: str) -> list[ConnectorConfig]:
    db = _require_exact_scoped_session(db)
    tenant_id = _require_requested_tenant(db, tenant_id)
    return list(db.execute(select(ConnectorConfig).where(ConnectorConfig.tenant_id == tenant_id).order_by(ConnectorConfig.connector_type)).scalars())  # noqa: E501


def get_or_create_connector_config(db: ScopedSession, tenant_id: str, connector_type: str) -> ConnectorConfig:  # noqa: E501
    db = _require_exact_scoped_session(db)
    tenant_id = _require_requested_tenant(db, tenant_id)
    row = db.execute(
        select(ConnectorConfig).where(
            and_(ConnectorConfig.tenant_id == tenant_id, ConnectorConfig.connector_type == connector_type)  # noqa: E501
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    now = _utcnow()
    row = ConnectorConfig(
        tenant_id=tenant_id,
        connector_type=connector_type,
        enabled=False,
        config_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def configure_connector(db: ScopedSession, tenant_id: str, connector_type: str, config_json: dict[str, Any]) -> ConnectorConfig:  # noqa: E501
    db = _require_exact_scoped_session(db)
    tenant_id = _require_requested_tenant(db, tenant_id)
    adapter = _adapter_registry().get(connector_type)
    if adapter is None:
        raise ValueError(f"unsupported connector_type '{connector_type}'")
    adapter.validate_config(config_json)
    row = get_or_create_connector_config(db, tenant_id, connector_type)
    row.config_json = config_json
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row


def set_connector_enabled(db: ScopedSession, tenant_id: str, connector_type: str, enabled: bool) -> ConnectorConfig:  # noqa: E501
    db = _require_exact_scoped_session(db)
    tenant_id = _require_requested_tenant(db, tenant_id)
    adapter = _adapter_registry().get(connector_type)
    if adapter is None:
        raise ValueError(f"unsupported connector_type '{connector_type}'")
    row = get_or_create_connector_config(db, tenant_id, connector_type)
    if enabled:
        adapter.validate_config(row.config_json)
    row.enabled = enabled
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row


def connector_status(db: ScopedSession, tenant_id: str) -> list[dict[str, Any]]:
    db = _require_exact_scoped_session(db)
    tenant_id = _require_requested_tenant(db, tenant_id)
    adapters = _adapter_registry()
    rows = {r.connector_type: r for r in list_connector_configs(db, tenant_id)}
    out: list[dict[str, Any]] = []
    for connector_type, adapter in adapters.items():
        cfg = rows.get(connector_type)
        enabled = bool(cfg.enabled) if cfg is not None else False
        config_json = cfg.config_json if cfg is not None else {}
        health: ConnectorHealthResult
        if not enabled:
            health = ConnectorHealthResult(healthy=True, message="disabled")
        else:
            try:
                health = adapter.healthcheck(config_json)
            except Exception as exc:  # noqa: BLE001
                health = ConnectorHealthResult(healthy=False, message=str(exc))
        out.append(
            {
                "connector_type": connector_type,
                "enabled": enabled,
                "healthy": bool(health.healthy),
                "message": health.message,
            }
        )
    return out


def enqueue_connector_event(
    db: ScopedSession,
    *,
    tenant_id: str,
    event_type: str,
    event_key: str,
    payload: dict[str, Any],
    join_key: str | None = None,
    capability_id: str | None = None,
    implementation_id: str | None = None,
) -> ConnectorOutbox:
    db = _require_exact_scoped_session(db)
    tenant_id = _require_requested_tenant(db, tenant_id)
    if not settings.connectors_enabled:
        raise RuntimeError("connectors are disabled")

    existing = db.execute(
        select(ConnectorOutbox).where(
            and_(
                ConnectorOutbox.tenant_id == tenant_id,
                ConnectorOutbox.event_type == event_type,
                ConnectorOutbox.event_key == event_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = _utcnow()
    envelope = ConnectorEventEnvelope(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        event_key=event_key,
        occurred_at=now,
        tenant_id=tenant_id,
        join_key=join_key,
        capability_id=capability_id,
        implementation_id=implementation_id,
        payload=payload,
    )
    row = ConnectorOutbox(
        tenant_id=tenant_id,
        event_type=event_type,
        event_key=event_key,
        payload_json=_event_payload(envelope),
        status="PENDING",
        attempts=0,
        next_attempt_at=now,
        last_error=None,
        created_at=now,
        processed_at=None,
    )
    db.add(row)
    db.flush()
    if event_type == "execution.event":
        _otel_add("execution")
    elif event_type == "outcome.event":
        _otel_add("outcome")
    elif event_type == "evaluation.completed":
        _otel_add("evaluation")
    elif event_type == "policy_action.lifecycle":
        _otel_add("policy")
    return row


def list_outbox(db: ScopedSession, status: str | None = None) -> list[ConnectorOutbox]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    stmt = select(ConnectorOutbox).where(ConnectorOutbox.tenant_id == tenant_id)
    normalized = _normalize_outbox_status(status)
    if normalized:
        stmt = stmt.where(ConnectorOutbox.status == normalized)
    return list(db.execute(stmt.order_by(ConnectorOutbox.id.desc())).scalars())


def retry_outbox_item(db: ScopedSession, outbox_id: int) -> ConnectorOutbox | None:
    db = _require_exact_scoped_session(db)
    row = db.get(ConnectorOutbox, outbox_id)
    if row is None:
        return None
    row.status = "PENDING"
    row.next_attempt_at = _utcnow()
    row.last_error = None
    db.commit()
    db.refresh(row)
    return row


def _enabled_connectors(db: ScopedSession, tenant_id: str) -> list[ConnectorConfig]:
    db = _require_exact_scoped_session(db)
    tenant_id = _require_requested_tenant(db, tenant_id)
    return list(
        db.execute(
            select(ConnectorConfig).where(
                and_(
                    ConnectorConfig.tenant_id == tenant_id,
                    ConnectorConfig.enabled.is_(True),
                )
            )
        ).scalars()
    )


def _record_delivery(
    db: ScopedSession,
    *,
    outbox_id: int,
    connector_type: str,
    attempt: int,
    result: ConnectorSendResult,
    duration_ms: int,
) -> None:
    db = _require_exact_scoped_session(db)
    db.add(
        ConnectorDeliveryLog(
            outbox_id=outbox_id,
            connector_type=connector_type,
            attempt=attempt,
            status_code=result.status_code,
            response_excerpt=(result.response_excerpt or "")[:512],
            duration_ms=duration_ms,
            created_at=_utcnow(),
        )
    )


def _attempt_send(db: ScopedSession, outbox_row: ConnectorOutbox) -> None:
    db = _require_exact_scoped_session(db)
    _require_requested_tenant(db, outbox_row.tenant_id)
    adapters = _adapter_registry()
    connectors = _enabled_connectors(db, outbox_row.tenant_id)
    if not connectors:
        outbox_row.status = "SENT"
        outbox_row.processed_at = _utcnow()
        return

    payload = outbox_row.payload_json
    all_ok = True
    errors: list[str] = []
    for cfg in connectors:
        adapter = adapters.get(cfg.connector_type)
        if adapter is None:
            continue

        started = perf_counter()
        try:
            result = adapter.send(outbox_row.event_type, payload, cfg.config_json)
        except Exception as exc:  # noqa: BLE001
            result = ConnectorSendResult(success=False, status_code=None, response_excerpt=str(exc))

        duration_ms = int((perf_counter() - started) * 1000)
        _record_delivery(
            db,
            outbox_id=outbox_row.id,
            connector_type=cfg.connector_type,
            attempt=outbox_row.attempts + 1,
            result=result,
            duration_ms=duration_ms,
        )
        if not result.success:
            all_ok = False
            errors.append(f"{cfg.connector_type}:{result.response_excerpt or 'send_failed'}")

    outbox_row.attempts += 1
    if all_ok:
        outbox_row.status = "SENT"
        outbox_row.processed_at = _utcnow()
        outbox_row.last_error = None
        return

    outbox_row.last_error = "; ".join(errors)[:1024]
    if outbox_row.attempts >= _max_attempts():
        outbox_row.status = "DEAD_LETTER"
        outbox_row.processed_at = _utcnow()
    else:
        outbox_row.status = "FAILED"
        outbox_row.next_attempt_at = _next_attempt(outbox_row.attempts)


def process_outbox_batch(db: ScopedSession, batch_size: int | None = None) -> int:
    db = _require_exact_scoped_session(db)
    if not settings.connectors_enabled:
        return 0

    effective_batch_size = batch_size or int(settings.connector_worker_batch_size)
    now = _utcnow()
    # ZER-49 A01-12.  The batch runs inside one transaction that commits only
    # after every row has been sent, so assigning ``status = "PROCESSING"`` in
    # the session was never a claim: a worker starting mid-batch still saw the
    # whole batch as PENDING and delivered every event a second time.  Two
    # mechanisms make the claim exclusive, and both are issued unconditionally
    # because ``ScopedSession`` deliberately exposes no bind or connection to
    # branch a dialect check on:
    #
    #   * ``FOR UPDATE SKIP LOCKED`` locks the candidate rows for the life of
    #     the transaction and lets a concurrent worker step over them instead
    #     of stalling on the batch.  SQLAlchemy compiles the locking clause
    #     silently away on SQLite, so it cannot be the only mechanism.
    #   * the conditional ``UPDATE ... RETURNING`` re-checks the claimable
    #     predicate at write time and reports back only the rows this worker
    #     actually won.  That is the guard on SQLite, and a cheap redundant
    #     check on Postgres.
    #
    # Failure semantics are unchanged: the claim shares the batch transaction,
    # so an exception still rolls the rows back to their previous status rather
    # than stranding them in PROCESSING.
    candidate_ids = list(
        db.execute(
            select(ConnectorOutbox.id)
            .where(
                and_(
                    ConnectorOutbox.status.in_(_CLAIMABLE_STATUSES),
                    ConnectorOutbox.next_attempt_at <= now,
                )
            )
            .order_by(ConnectorOutbox.id.asc())
            .limit(effective_batch_size)
            .with_for_update(skip_locked=True)
        ).scalars()
    )

    claimed_ids: list[int] = []
    if candidate_ids:
        claimed_ids = list(
            db.execute(
                update(ConnectorOutbox)
                .where(
                    and_(
                        ConnectorOutbox.id.in_(candidate_ids),
                        ConnectorOutbox.status.in_(_CLAIMABLE_STATUSES),
                        ConnectorOutbox.next_attempt_at <= now,
                    )
                )
                .values(status="PROCESSING")
                .returning(ConnectorOutbox.id)
                .execution_options(synchronize_session=False)
            ).scalars()
        )

    rows: list[ConnectorOutbox] = []
    if claimed_ids:
        rows = list(
            db.execute(
                select(ConnectorOutbox)
                .where(ConnectorOutbox.id.in_(claimed_ids))
                .order_by(ConnectorOutbox.id.asc())
            ).scalars()
        )

    processed = 0
    for row in rows:
        _attempt_send(db, row)
        db.flush()
        processed += 1
    db.commit()
    return processed


def outbox_counts(db: ScopedSession) -> dict[str, int]:
    db = _require_exact_scoped_session(db)
    return {
        status: int(
            db.execute(
                select(func.count(ConnectorOutbox.id)).where(
                    ConnectorOutbox.status == status
                )
            ).scalar_one()
            or 0
        )
        for status in ("PENDING", "FAILED", "DEAD_LETTER")
    }


def render_prometheus_metrics(db: ScopedSession) -> str:
    db = _require_exact_scoped_session(db)
    from zeroth.econ.plane.counterfactual.models import ValueEstimate
    from zeroth.econ.plane.enforcement.models import PolicyAction
    from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent

    execution_total = int(
        db.execute(select(func.count(ExecutionEvent.id))).scalar_one() or 0
    )
    outcomes_total = int(
        db.execute(select(func.count(OutcomeEvent.id))).scalar_one() or 0
    )
    value_sum = float(db.execute(select(func.coalesce(func.sum(ValueEstimate.estimated_value_usd), 0))).scalar_one() or 0.0)  # noqa: E501
    cost_sum = float(db.execute(select(func.coalesce(func.sum(ValueEstimate.estimated_cost_usd), 0))).scalar_one() or 0.0)  # noqa: E501
    margin_sum = float(db.execute(select(func.coalesce(func.sum(ValueEstimate.net_margin_usd), 0))).scalar_one() or 0.0)  # noqa: E501
    gate_pass = int(
        db.execute(
            select(func.count(ValueEstimate.id)).where(
                ValueEstimate.confidence_gate_passed.is_(True)
            )
        ).scalar_one()
        or 0
    )
    gate_block = int(
        db.execute(
            select(func.count(ValueEstimate.id)).where(
                ValueEstimate.confidence_gate_passed.is_(False)
            )
        ).scalar_one()
        or 0
    )
    drift_critical = int(
        db.execute(
            select(func.count(ValueEstimate.id)).where(
                ValueEstimate.drift_state == "critical"
            )
        ).scalar_one()
        or 0
    )
    outbox = outbox_counts(db)
    policy_counts = {
        status: int(
            db.execute(
                select(func.count(PolicyAction.id)).where(PolicyAction.status == status)
            ).scalar_one()
            or 0
        )
        for status in ("PROPOSED", "APPROVED", "APPLIED", "REJECTED", "FAILED")
    }

    lines = [
        "# TYPE ecp_execution_events_total counter",
        f"ecp_execution_events_total {execution_total}",
        "# TYPE ecp_outcomes_total counter",
        f"ecp_outcomes_total {outcomes_total}",
        "# TYPE ecp_estimated_value_usd_total counter",
        f"ecp_estimated_value_usd_total {value_sum}",
        "# TYPE ecp_estimated_cost_usd_total counter",
        f"ecp_estimated_cost_usd_total {cost_sum}",
        "# TYPE ecp_net_margin_usd_total counter",
        f"ecp_net_margin_usd_total {margin_sum}",
        "# TYPE ecp_confidence_gate_pass_total counter",
        f"ecp_confidence_gate_pass_total {gate_pass}",
        "# TYPE ecp_confidence_gate_block_total counter",
        f"ecp_confidence_gate_block_total {gate_block}",
        "# TYPE ecp_drift_critical_total counter",
        f"ecp_drift_critical_total {drift_critical}",
        "# TYPE ecp_connector_outbox_pending gauge",
        f"ecp_connector_outbox_pending {outbox.get('PENDING', 0)}",
        "# TYPE ecp_connector_outbox_failed gauge",
        f"ecp_connector_outbox_failed {outbox.get('FAILED', 0)}",
        "# TYPE ecp_connector_outbox_dead_letter gauge",
        f"ecp_connector_outbox_dead_letter {outbox.get('DEAD_LETTER', 0)}",
    ]
    for status, count in policy_counts.items():
        lines.append(f'ecp_policy_actions_total{{status="{status}",type="all"}} {count}')
    return "\n".join(lines) + "\n"
