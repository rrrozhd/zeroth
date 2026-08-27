"""MCP server registry administration and tool discovery.

Operators register the stdio MCP servers a deployment makes available; graph
authors reference one by ``ref`` and never write its ``command``/``args``/
``env``. That split is the point: ``capability_bindings`` are author-declared
(``PolicyGuard.evaluate`` resolves required capabilities from the node and lets
policies -- also bound in the same graph -- decide only whether they are
permitted), so a row an author cannot edit is the only place an operator-side
ceiling can live.

Be precise about what that ceiling bounds, because the split above reads wider
than it is. ``grants`` gates which *graphs may reference* this server: an
``mcp_tool`` node may not declare more than the row grants, checked at publish
and again in ``MCPSessionPool`` before a process exists. It constrains the
spawned process not at all. ``command``, ``args`` and ``env`` are handed to the
transport verbatim -- no allowlist, no digest pin, no working directory, no
rlimits, no uid drop, no sandbox -- so the server runs as the service user with
that user's authority. ``MCP_ADMIN`` is therefore arbitrary code execution on
this host. That is the deliberate shape of an admin-tier role rather than an
oversight, and it is why the permission is separated below; hand it out with
the same care as shell access.

Every route is gated on ``MCP_ADMIN``, which ``OPERATOR`` deliberately does not
hold. Reusing ``CONNECTOR_ADMIN`` would have handed the registry to the same
role that authors graphs (``WORKFLOW_ADMIN``) and collapsed that separation.
"""

from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.graph.models import Capability
from zeroth.platform.primitives.error_vocabulary import safe_error_detail
from zeroth.runtime.agents.mcp import MCPClientManager, MCPServerConfig, tool_schema_hash
from zeroth.service.api.authorization import Permission, require_permission

_REF_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class MCPServerResponse(BaseModel):
    """One registered MCP server, as shown to an operator."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    command: str
    args: list[str]
    #: Keys only, values masked. Which variables are set is useful to an
    #: operator; their values are credentials.
    env: dict[str, str]
    grants: list[Capability]
    created_at: str
    updated_at: str


class MCPServerCreateRequest(BaseModel):
    """Payload for registering an MCP server."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    grants: list[Capability] = Field(default_factory=list)


class MCPServerUpdateRequest(BaseModel):
    """Payload for reconfiguring a registered MCP server."""

    model_config = ConfigDict(extra="forbid")

    command: str
    args: list[str] = Field(default_factory=list)
    #: Omit to keep the stored environment. It cannot default to ``{}`` like the
    #: other fields: responses mask every value, so the documented operation
    #: "PUT command/args/grants to narrow a server" would round-trip a payload
    #: with no real env and silently delete every credential -- or, if the
    #: client posted the masked body back, persist the literal "***" and leave
    #: the registry rendering identical to a healthy row.
    env: dict[str, str] | None = None
    grants: list[Capability] = Field(default_factory=list)


class DiscoveredToolResponse(BaseModel):
    """One tool a live server advertised, with the digest an import would pin."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any] | None = None
    schema_hash: str


class MCPDiscoverResponse(BaseModel):
    """Result of listing tools on a live server."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    tools: list[DiscoveredToolResponse]
    latency_ms: float


def _mask_env(env: dict[str, str]) -> dict[str, str]:
    """Show which variables are set, never their values.

    Unlike a connector's ``params`` -- where a DSN's host and database are
    useful and only the userinfo is sensitive -- an MCP server's environment is
    credentials by convention. There is no non-secret half worth guessing at,
    so every value is masked and only the key names survive.
    """
    return dict.fromkeys(env, "***")


def _repo(request: Request) -> Any:
    repo = getattr(request.app.state.bootstrap, "mcp_server_config_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MCP server registry is not available",
        )
    return repo


def _tenant(request: Request) -> str:
    """Authoritative tenant for this deployment (WS-B)."""
    deployment = getattr(request.app.state.bootstrap, "deployment", None)
    return getattr(deployment, "tenant_id", None) or "default"


def _validate_ref(ref: str) -> None:
    if not _REF_RE.match(ref):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ref must match ^[a-z0-9_-]{1,64}$",
        )


def _response(record: Any) -> MCPServerResponse:
    return MCPServerResponse(
        ref=record.ref,
        command=record.command,
        args=list(record.args),
        env=_mask_env(record.env),
        grants=list(record.grants),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def register_mcp_server_routes(app: FastAPI | APIRouter) -> None:
    """Register MCP server registry administration routes."""

    @app.get("/mcp/servers", response_model=list[MCPServerResponse])
    async def list_mcp_servers(request: Request) -> list[MCPServerResponse]:
        """Every MCP server registered for this deployment, by ref."""
        await require_permission(request, Permission.MCP_ADMIN)
        records = await _repo(request).list(tenant_id=_tenant(request))
        return [_response(record) for record in records]

    @app.post(
        "/mcp/servers",
        response_model=MCPServerResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_mcp_server(
        request: Request, payload: MCPServerCreateRequest
    ) -> MCPServerResponse:
        """Register a server. Does not spawn it -- use the discover route for that."""
        await require_permission(request, Permission.MCP_ADMIN)
        repo = _repo(request)
        tenant = _tenant(request)
        _validate_ref(payload.ref)
        if await repo.get(payload.ref, tenant_id=tenant) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"MCP server {payload.ref!r} already exists; use PUT to update",
            )
        try:
            record = await repo.upsert(
                payload.ref,
                payload.command,
                payload.args,
                payload.env,
                payload.grants,
                tenant_id=tenant,
            )
        except KeyError as exc:
            # ``ref`` is the table PRIMARY KEY, so another tenant already owning
            # it makes the tenant-scoped upsert affect no row. Answering with
            # the same 409 an owned collision gets is deliberate: a distinct
            # status here would let any caller probe another tenant's refs.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"MCP server {payload.ref!r} already exists; use PUT to update",
            ) from exc
        return _response(record)

    @app.put("/mcp/servers/{ref}", response_model=MCPServerResponse)
    async def update_mcp_server(
        request: Request, ref: str, payload: MCPServerUpdateRequest
    ) -> MCPServerResponse:
        """Reconfigure a registered server.

        Narrowing ``grants`` here can strand an already-published graph whose
        node was validated against the wider ceiling. That is deliberate:
        ``MCPSessionPool`` re-reads this row before it will hand out a session,
        so the graph fails closed rather than keeping a capability the operator
        has since withdrawn. Published versions are immutable, so the
        publish-time check alone could not achieve that.
        """
        await require_permission(request, Permission.MCP_ADMIN)
        repo = _repo(request)
        tenant = _tenant(request)
        _validate_ref(ref)
        existing = await repo.get(ref, tenant_id=tenant)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server {ref!r} not found",
            )
        # None means "leave the environment alone"; {} is an explicit clear.
        env = existing.env if payload.env is None else payload.env
        record = await repo.upsert(
            ref, payload.command, payload.args, env, payload.grants, tenant_id=tenant
        )
        return _response(record)

    @app.delete("/mcp/servers/{ref}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_mcp_server(request: Request, ref: str) -> Response:
        """Deregister a server."""
        await require_permission(request, Permission.MCP_ADMIN)
        _validate_ref(ref)
        if not await _repo(request).delete(ref, tenant_id=_tenant(request)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server {ref!r} not found",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/mcp/servers/{ref}/tools", response_model=MCPDiscoverResponse)
    async def discover_mcp_tools(request: Request, ref: str) -> MCPDiscoverResponse:
        """Spawn the server, list its tools with the digests an import would pin, stop.

        This is the only route that runs the operator's command, and it is
        why the route set is admin-tier. The returned ``schema_hash`` values
        are what ``mcp-import`` freezes into the graph and what the runtime
        drift check later compares against.
        """
        await require_permission(request, Permission.MCP_ADMIN)
        _validate_ref(ref)
        record = await _repo(request).get(ref, tenant_id=_tenant(request))
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server {ref!r} not found",
            )
        manager = MCPClientManager(
            [
                MCPServerConfig(
                    name=record.ref,
                    command=record.command,
                    args=list(record.args),
                    env=dict(record.env) or None,
                )
            ]
        )
        started = time.perf_counter()
        try:
            manifests = await manager.start()
        except Exception as exc:  # noqa: BLE001 - surfaced as a probe failure
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=safe_error_detail(exc, context="MCP server discovery"),
            ) from exc
        finally:
            await manager.stop()
        latency_ms = (time.perf_counter() - started) * 1000.0
        return MCPDiscoverResponse(
            ref=ref,
            tools=[
                DiscoveredToolResponse(
                    name=manifest.alias,
                    description=manifest.description,
                    input_schema=manifest.parameters_schema,
                    schema_hash=tool_schema_hash(
                        manifest.alias, manifest.description, manifest.parameters_schema
                    ),
                )
                for manifest in manifests
            ],
            latency_ms=latency_ms,
        )
