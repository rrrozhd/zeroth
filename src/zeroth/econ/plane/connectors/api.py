from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.deps import get_current_scoped_db, require_roles
from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.common.tenant import resolve_tenant_id
from zeroth.econ.plane.connectors.schemas import (
    ConnectorConfigOut,
    ConnectorConfigRequest,
    ConnectorOutboxOut,
    ConnectorStatusOut,
    RetryOutboxResponse,
)
from zeroth.econ.plane.connectors.service import (
    configure_connector,
    connector_status,
    get_or_create_connector_config,
    list_outbox,
    render_prometheus_metrics,
    retry_outbox_item,
    set_connector_enabled,
)
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["connectors", "metrics"])


@router.get("/connectors", response_model=list[ConnectorStatusOut])
def get_connectors(
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[ConnectorStatusOut]:
    rows = connector_status(db, _user.tenant_id)
    return [ConnectorStatusOut(**r) for r in rows]


@router.post("/connectors/{connector_type}/configure", response_model=ConnectorConfigOut)
def configure(
    connector_type: str,
    payload: ConnectorConfigRequest,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> ConnectorConfigOut:
    try:
        row = configure_connector(db, _user.tenant_id, connector_type, payload.config_json)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ConnectorConfigOut.model_validate(row)


@router.post("/connectors/{connector_type}/enable", response_model=ConnectorConfigOut)
def enable(
    connector_type: str,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> ConnectorConfigOut:
    get_or_create_connector_config(db, _user.tenant_id, connector_type)
    try:
        row = set_connector_enabled(db, _user.tenant_id, connector_type, True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ConnectorConfigOut.model_validate(row)


@router.post("/connectors/{connector_type}/disable", response_model=ConnectorConfigOut)
def disable(
    connector_type: str,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> ConnectorConfigOut:
    try:
        row = set_connector_enabled(db, _user.tenant_id, connector_type, False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ConnectorConfigOut.model_validate(row)


@router.get("/connectors/outbox", response_model=list[ConnectorOutboxOut])
def get_outbox(
    status: str | None = Query(default=None),
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin", "Analyst", "Approver", "Viewer")),
) -> list[ConnectorOutboxOut]:
    return [ConnectorOutboxOut.model_validate(r) for r in list_outbox(db, status)]


@router.post("/connectors/outbox/{outbox_id}/retry", response_model=RetryOutboxResponse)
def retry_outbox(
    outbox_id: int,
    db: ScopedSession = Depends(get_current_scoped_db),
    _user: UserClaims = Depends(require_roles("Admin")),
) -> RetryOutboxResponse:
    row = retry_outbox_item(db, outbox_id)
    if row is None:
        raise HTTPException(status_code=404, detail="outbox item not found")
    return RetryOutboxResponse(id=row.id, status=row.status)


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> Response:
    return Response(content=render_prometheus_metrics(db), media_type="text/plain; version=0.0.4")
