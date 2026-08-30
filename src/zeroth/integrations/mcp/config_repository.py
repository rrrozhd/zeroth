"""Persistence for operator-registered MCP server configurations.

Operators register servers through the admin API (POST /v1/mcp/servers); graphs
reference one by ``ref`` and never see its ``command``/``args``/``env``. That
split is the point of the table: ``capability_bindings`` are author-declared
(``PolicyGuard.evaluate``), so a row here -- which the author of an ``mcp_tool``
node cannot edit -- is where an operator-side ceiling can live.

What ``grants`` bounds, and what it does not
--------------------------------------------
``grants`` gates **which graphs may reference this server**: an ``mcp_tool``
node publishes only if its ``capability_bindings`` stay inside this list. It
says nothing whatever about the process the server becomes. The operator-owned,
digest-pinned Docker profile supplies the real process boundary; secure defaults
refuse discovery and dispatch when that profile is absent. The explicit
development-only escape hatch hands ``command``, ``args`` and ``env`` to the
legacy host transport and makes a principal holding ``MCP_ADMIN`` able to run
arbitrary code as the service user; production rejects that flag. This is why
``OPERATOR`` does not hold ``MCP_ADMIN``. Read any "ceiling" in this module as a
ceiling on *references*, never on the child process.

Two live qualifications on "the author cannot edit it":

* The deprecated inline ``agent.mcp_servers`` path is rejected by default (the
  development-only flag downgrades it to a warning). On that path
  the author picks the binary, argv and env themselves and no row here bounds
  them. Until it is removed, this table is the operator-owned side for
  ``mcp_tool`` nodes only.
* Anything holding ``MCP_ADMIN`` can rewrite these rows. The separation is
  between roles, not between people.

Secret posture
--------------
``env`` is persisted as plaintext JSON: no envelope encryption, no
``SecretProvider`` indirection. This is exact parity with
``memory_connector_configs`` (migration 005) and an accepted posture rather than
a new exposure -- changing it here alone would only move the problem. What makes
it tolerable is that the values never come back out: ``mcp_server_api._mask_env``
replaces every value with ``***`` on every response (keys survive, because which
variables are set is operationally useful and their values are credentials by
convention), and the discover route's 502 goes through ``safe_error_detail``.
The values leave the row only on their way to the spawned process. Migration 027
refuses to drop this table while it holds rows for the same reason.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.governance.policy.models import Capability
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopedTable,
)
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class MCPServerConfigRecord(BaseModel):
    """A persisted MCP server registration row."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    #: The operator's ceiling on *references*: a referencing ``mcp_tool`` node's
    #: ``capability_bindings`` must be a subset of this, and empty denies every
    #: referencing node rather than permitting every one. It does not constrain
    #: the spawned process -- see the module docstring.
    grants: list[Capability] = Field(default_factory=list)
    # WS-B: owning tenant. ``env`` carries API keys, so a server registered by
    # tenant A must be invisible to tenant B when both deployments share the
    # physical control-plane DB. Defaults to the reserved single-tenant sentinel.
    tenant_id: str = "default"
    created_at: str
    updated_at: str


@persistence_surface(
    "service.mcp_server_configs", probe=named_isolation_probe("_drive_mcp_server_configs")
)
class MCPServerConfigRepository:
    """Raw-SQL repository over the ``mcp_server_configs`` table.

    WS-B: reads (``get``/``list``) and ``delete`` are tenant-scoped, and
    ``upsert`` stamps the owning tenant. ``ref`` is the table PRIMARY KEY
    (migration 027 creates it that way), so two tenants cannot register the
    *same* ref on one shared DB -- the same accepted limitation documented on
    ``MemoryConnectorConfigRepository``.

    Every method here returns ``env`` in the clear, because the callers that
    need it -- the discover route and the session pool -- are spawning the
    server. The masking that keeps it off the wire lives at the API boundary
    (``mcp_server_api._response``), not here; a new caller of this repository
    inherits no masking and has to be checked.
    """

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    def _configs(self, tenant_id: str | None) -> ScopedTable:
        context = (
            NullWorkspaceScopeContext.for_default_compatibility()
            if tenant_id in {None, "default"}
            else NullWorkspaceScopeContext(tenant_id=tenant_id)
        )
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.mcp_server_configs",
            context,
        )

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def upsert(
        self,
        ref: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        grants: list[Capability],
        *,
        tenant_id: str = "default",
    ) -> MCPServerConfigRecord:
        """Insert or update a server registration; returns the stored row."""
        now = _utcnow_iso()
        configs = self._configs(tenant_id)
        async with configs.transaction(write_lock=True) as table:
            stored = await table.upsert(
                {
                    "ref": ref,
                    "command": command,
                    "args": _dump(list(args)),
                    "env": _dump(dict(env)),
                    "grants": _dump([capability.value for capability in grants]),
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=("ref",),
                update_columns=("command", "args", "env", "grants", "updated_at"),
                returning="ref",
                update_where={"tenant_id": tenant_id},
            )
        if not stored:
            raise KeyError(ref)
        return await self.get(ref, tenant_id=tenant_id)  # type: ignore[return-value]

    @persistence_operation(ResourceOperation.READ)
    async def get(self, ref: str, *, tenant_id: str | None = None) -> MCPServerConfigRecord | None:
        """Load a single server registration by ref (optionally tenant-scoped)."""
        row = await self._configs(tenant_id).select_one(where={"ref": ref})
        if row is None:
            return None
        return self._row_to_config(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(self, *, tenant_id: str | None = None) -> list[MCPServerConfigRecord]:
        """Persisted server registrations, ordered by ref (optionally tenant-scoped)."""
        async with self._configs(tenant_id).transaction() as table:
            rows = await table.select(order_by=("ref",))
        return [self._row_to_config(row) for row in rows]

    @persistence_operation(ResourceOperation.DELETE)
    async def delete(self, ref: str, *, tenant_id: str | None = None) -> bool:
        """Delete a server registration. Returns True if a row existed (in tenant)."""
        async with self._configs(tenant_id).transaction(write_lock=True) as table:
            existing = await table.select_one(where={"ref": ref}, columns=("ref",))
            if existing is None:
                return False
            await table.delete(where={"ref": ref})
        return True

    def _row_to_config(self, row: Any) -> MCPServerConfigRecord:
        return MCPServerConfigRecord(
            ref=row["ref"],
            command=row["command"],
            args=_load(row["args"], []),
            env=_load(row["env"], {}),
            # An unrecognised capability string is dropped rather than raising:
            # a ceiling row must stay loadable across a vocabulary change, and
            # dropping narrows the ceiling (fails closed) where raising would
            # make every referencing graph unrunnable.
            grants=[
                capability
                for capability in (_as_capability(value) for value in _load(row["grants"], []))
                if capability is not None
            ],
            tenant_id=row["tenant_id"] or "default",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw) if raw else fallback


def _as_capability(value: Any) -> Capability | None:
    try:
        return Capability(value)
    except ValueError:
        return None
