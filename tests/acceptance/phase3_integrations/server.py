"""The real economic routes on SQLite behind a loopback HTTP listener.

Only database construction is overridden; authentication, roles, tenant
claims, validation, ingestion, retained reports and serialization are the
production code paths. Subprocess recipes (TypeScript, restarted workers) need
a real socket, so every test uses the same listener instead of an ASGI shim.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.cloud.api import router as cloud_router
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.decisioning.api import router as decision_router
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def build_app(engine) -> FastAPI:
    app = FastAPI()
    for router in (cloud_router, decision_router, instrumentation_router):
        app.include_router(router, prefix="/v1")

    def database():
        with Session(engine) as raw:
            yield raw

    def scoped_database(user=Depends(get_current_user)):  # noqa: B008
        with Session(engine) as raw:
            yield ScopedSession(raw, TenantWideScopeContext(tenant_id=user.tenant_id))

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_scoped_db] = scoped_database
    return app


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "economic routes did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "economic HTTP listener did not stop"


def token(tenant: str = "tenant-a") -> str:
    minted = mint_econ_service_token(tenant)
    assert minted is not None
    return minted


def sdk(origin: str, tenant: str = "tenant-a", **kwargs):
    from zeroth.sdk import ZerothClient

    return ZerothClient(api_key=token(tenant), base_url=origin, **kwargs)
