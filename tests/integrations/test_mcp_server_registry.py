"""Proofs for the operator-owned MCP server registry (migration 027).

The registry only earns its place if it is the side of the capability check a
graph author cannot edit, so these tests exercise the properties that carry
that weight: rows survive a restart, one tenant's row (whose ``env`` holds API
keys) is invisible to another, and every publish path actually consults it.

Two of them are about what the registry does *not* do. ``grants`` gates which
graphs may reference a server; it says nothing about the process that server
becomes -- ``command``/``args``/``env`` are used verbatim, so what keeps the
service's own secrets out of that child is the MCP SDK's environment allowlist,
which is inherited behaviour nothing here owns. It is pinned below because
nothing else pins it.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import textwrap

import pytest

from zeroth.contracts.graph.models import (
    AgentToolBinding,
    Edge,
    Graph,
    MCPToolNode,
    MCPToolNodeData,
)
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.contracts.graph.validation_errors import GraphValidationError
from zeroth.governance.policy.models import Capability
from zeroth.integrations.mcp.config_repository import MCPServerConfigRepository
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.runtime.agents.mcp import MCP_REQUIRED_CAPABILITIES, MCPClientManager, MCPServerConfig, RegisteredMCPServerConfig
from zeroth.service.bootstrap.migrations import run_migrations
from zeroth.service.demo import DEMO_GRAPH_ID, build_hello_graph, seed_demo


def _database(tmp_path: Path) -> AsyncSQLiteDatabase:
    database_path = tmp_path / "registry.db"
    run_migrations(f"sqlite:///{database_path}")
    return AsyncSQLiteDatabase(str(database_path))


@pytest.mark.asyncio
async def test_migration_027_creates_a_usable_table(tmp_path: Path) -> None:
    """A round trip proves the migration applied, not merely that it ran."""
    repository = MCPServerConfigRepository(_database(tmp_path))

    stored = await repository.upsert(
        "filesystem",
        "npx",
        ["-y", "@mcp/server-filesystem", "/srv/data"],
        {"API_KEY": "secret"},
        [Capability.FILESYSTEM_READ],
    )

    assert stored.ref == "filesystem"
    assert stored.command == "npx"
    assert stored.args == ["-y", "@mcp/server-filesystem", "/srv/data"]
    assert stored.env == {"API_KEY": "secret"}
    assert stored.grants == [Capability.FILESYSTEM_READ]
    assert stored.tenant_id == "default"


@pytest.mark.asyncio
async def test_registration_survives_a_restart(tmp_path: Path) -> None:
    """The ceiling is worthless if it evaporates when the process dies."""
    database_path = tmp_path / "registry.db"
    run_migrations(f"sqlite:///{database_path}")

    await MCPServerConfigRepository(AsyncSQLiteDatabase(str(database_path))).upsert(
        "git", "npx", ["-y", "@mcp/server-git"], {}, [Capability.EXTERNAL_API_CALL]
    )

    reopened = await MCPServerConfigRepository(AsyncSQLiteDatabase(str(database_path))).get("git")

    assert reopened is not None
    assert reopened.grants == [Capability.EXTERNAL_API_CALL]


@pytest.mark.asyncio
async def test_a_foreign_tenant_cannot_read_or_delete_a_registration(tmp_path: Path) -> None:
    """``env`` carries API keys, so cross-tenant visibility is a credential leak."""
    repository = MCPServerConfigRepository(_database(tmp_path))
    await repository.upsert(
        "filesystem", "npx", [], {"API_KEY": "owner-secret"}, [], tenant_id="owner"
    )

    assert await repository.get("filesystem", tenant_id="foreign") is None
    assert await repository.list(tenant_id="foreign") == []
    assert not await repository.delete("filesystem", tenant_id="foreign")

    owner_row = await repository.get("filesystem", tenant_id="owner")
    assert owner_row is not None
    assert owner_row.env == {"API_KEY": "owner-secret"}


@pytest.mark.asyncio
async def test_an_unknown_grant_narrows_the_ceiling_rather_than_raising(tmp_path: Path) -> None:
    """A vocabulary change must not make every referencing graph unloadable.

    Dropping an unrecognised grant fails closed -- the ceiling gets smaller, so
    a node that relied on it is denied at publish. Raising here would instead
    take out every graph referencing the server, which is a worse failure for a
    row an operator may not be able to edit quickly.
    """
    repository = MCPServerConfigRepository(_database(tmp_path))
    await repository.upsert("srv", "echo", [], {}, [Capability.FILESYSTEM_READ])

    async with repository._configs("default").transaction(write_lock=True) as table:
        await table.update(
            {"grants": '["filesystem_read","not_a_real_capability"]'}, where={"ref": "srv"}
        )

    loaded = await repository.get("srv")
    assert loaded is not None
    assert loaded.grants == [Capability.FILESYSTEM_READ]


def _demo_draft_with_mcp_tool(server_ref: str) -> Graph:
    """The demo graph carrying one ``mcp_tool`` node, shaped as an import leaves it.

    Node, tool edge and agent binding together -- ``seed_demo`` publishes an
    existing draft under ``demo-hello`` as it finds it, so the graph reaching
    that publish has to be the graph an operator would really have.
    """
    required_refs = sorted(capability.value for capability in MCP_REQUIRED_CAPABILITIES)
    graph = build_hello_graph()
    agent = graph.nodes[0]
    agent.agent.tool_bindings = [
        AgentToolBinding(
            target_node_id="mcp_echo", name="echo", description="Echo the text back"
        )
    ]
    # The agent floor: an agent must itself hold what its attached tool needs,
    # so a graph without this is refused before the registry is ever consulted.
    agent.capability_bindings = list(required_refs)
    node = MCPToolNode(
        node_id="mcp_echo",
        graph_version_ref=agent.graph_version_ref,
        capability_bindings=list(required_refs),
        mcp_tool=MCPToolNodeData(
            server_ref=server_ref,
            tool_name="echo",
            description="Echo the text back",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            schema_hash="pinned-at-import",
        ),
    )
    return graph.model_copy(
        update={
            "nodes": [*graph.nodes, node],
            "edges": [
                Edge(
                    edge_id="agent->mcp_echo",
                    source_node_id="agent",
                    target_node_id="mcp_echo",
                    kind="tool",
                )
            ],
        }
    )


@pytest.mark.asyncio
async def test_seed_demo_refuses_a_graph_referencing_an_unregistered_server(tmp_path: Path) -> None:
    """``seed-demo`` is a publish path, so it owes the registry the same check.

    The seeded graph has no ``mcp_tool`` node today, which is exactly what makes
    this worth pinning: the gap is invisible until someone leaves a draft under
    ``demo-hello``, and then an unreviewed publish is the *only* way that graph
    ever goes live. The draft is planted through a repository with no validator
    -- the state any tooling that predates the check would leave behind.
    """
    database = _database(tmp_path)
    await GraphRepository(database).save(_demo_draft_with_mcp_tool("never-registered"))

    with pytest.raises(GraphValidationError) as excinfo:
        await seed_demo(database)

    # The exception's own text is only a count, so assert on the issue: the
    # point is *which* check refused, not that something did.
    (error,) = excinfo.value.report.errors
    assert error.node_id == "mcp_echo"
    assert "unknown MCP server 'never-registered'" in error.message


@pytest.mark.asyncio
async def test_seed_demo_still_publishes_what_the_operator_did_register(tmp_path: Path) -> None:
    """The other half: a wired resolver must resolve, not merely refuse.

    A resolver aimed at the wrong tenant -- or one that answers ``None`` for
    everything -- would satisfy the test above while making ``seed-demo``
    unusable for anyone who registered a server properly.
    """
    database = _database(tmp_path)
    await MCPServerConfigRepository(database).upsert(
        "echo", "npx", ["-y", "@mcp/echo"], {}, sorted(MCP_REQUIRED_CAPABILITIES, key=str)
    )
    await GraphRepository(database).save(_demo_draft_with_mcp_tool("echo"))

    deployment = await seed_demo(database)

    assert deployment.graph_id == DEMO_GRAPH_ID
    assert deployment.graph_version_ref == f"{DEMO_GRAPH_ID}@1"


#: A one-tool MCP server that reports its own environment. The repository's
#: fixtures deliberately do not answer this question -- ``echo_server`` cannot
#: see out of itself -- and nothing else in the tree spawns a server just to
#: look at what it inherited.
_ENV_PROBE_SERVER = textwrap.dedent(
    '''
    """Report one environment variable as the spawned child actually sees it."""

    import os

    from mcp.server.fastmcp import FastMCP

    server = FastMCP("zeroth-env-probe")


    @server.tool(description="Return one environment variable, or <absent>")
    def read_env(name: str) -> str:
        return os.environ.get(name, "<absent>")


    if __name__ == "__main__":
        server.run()
    '''
)


@pytest.mark.asyncio
async def test_a_spawned_server_inherits_the_registry_row_and_not_the_services_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mitigation that makes plaintext ``env`` tolerable is not ours -- pin it.

    ``env`` is stored unencrypted (parity with ``memory_connector_configs``),
    and ``grants`` bounds only which graphs may *reference* a server -- never
    what the spawned process may do, since ``command``/``args``/``env`` are
    passed verbatim with no allowlist, digest pin, cwd, rlimits or uid drop.
    So the one thing standing between the service's own credentials and an
    operator-supplied binary is the MCP SDK's environment allowlist
    (``get_default_environment``: HOME, LOGNAME, PATH, SHELL, TERM, USER), which
    this repository inherits, does not own, and never checked.

    Both halves matter. If the allowlist regressed to a full ``os.environ``
    copy, every registered server would silently gain the service's API keys.
    If it stopped merging the configured ``env``, every registration's own
    credentials would stop arriving and the registry would be inert.

    THIRD-PARTY LOCK, NOT A WITNESS. No code in this repository makes this
    property true, so it cannot fail against the pre-fix tree by construction
    and it is evidence for no finding. Do not count it as coverage; it exists
    because nothing pinned an inherited guarantee the plaintext-``env``
    decision leans on, and a silent upstream change would otherwise be the
    first anyone heard of it.
    """
    script = tmp_path / "env_probe_server.py"
    script.write_text(_ENV_PROBE_SERVER)
    monkeypatch.setenv("ZEROTH_TEST_PARENT_SECRET", "belongs-to-the-service")

    manager = MCPClientManager(
        [
            RegisteredMCPServerConfig(
                name="probe",
                command=sys.executable,
                args=[str(script)],
                env={"ZEROTH_TEST_CHILD_TOKEN": "from-the-registry-row"},
                grants=list(MCP_REQUIRED_CAPABILITIES),
            )
        ]
    )
    try:
        await manager.start()
        # The parent really does hold it, so "<absent>" below is the child's
        # environment talking rather than a variable that was never set.
        assert os.environ["ZEROTH_TEST_PARENT_SECRET"] == "belongs-to-the-service"
        inherited = await manager.call_tool("read_env", {"name": "ZEROTH_TEST_PARENT_SECRET"})
        configured = await manager.call_tool("read_env", {"name": "ZEROTH_TEST_CHILD_TOKEN"})
    finally:
        await manager.stop()

    assert inherited == "<absent>"
    assert configured == "from-the-registry-row"
