"""Migration 035 refuses to destroy MCP server registrations on the way down.

``downgrade`` here is the only irreversible step in the recent chain: it drops
the sole copy of every registered server's ``command``/``args``/``env``, and
``env`` is credentials. These tests pin that a rollback which would lose them
stops instead, that the escape hatch really is an escape hatch, and that the
table it recreates afterwards is usable -- the mechanical round trip nothing
else exercises.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

#: Spelled out rather than imported from the migration module, which is not
#: importable by name (``035_add_mcp_server_configs`` starts with a digit) and
#: whose symbol would have to be loaded off disk by path. The duplication is
#: self-checking in the direction that matters: this is the string an operator
#: types from a runbook, and if the migration ever renames it, the forced
#: downgrade below sets a variable nothing reads, the guard refuses, and the
#: test goes red -- which is the correct answer to a renamed operator interface.
FORCE_DROP_ENV_VAR = "ZEROTH_MIGRATION_035_FORCE_DROP"

_INSERT = text(
    "INSERT INTO mcp_server_configs "
    "(ref, command, args, env, grants, tenant_id, created_at, updated_at) "
    "VALUES ('filesystem', 'npx', '[\"-y\",\"@mcp/server-filesystem\"]', "
    "'{\"API_KEY\":\"live-secret\"}', '[\"filesystem_read\"]', 'default', 'then', 'then')"
)


def _config(path: Path) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def test_035_downgrade_refuses_while_registrations_exist(tmp_path: Path) -> None:
    """The rollback stops rather than taking the credentials with it.

    Asserting on the surviving row, not just on the raise: the whole failure
    this guards against is a ``DROP`` that already happened by the time anyone
    reads the traceback.
    """
    path = tmp_path / "populated.db"
    config = _config(path)
    command.upgrade(config, "035")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(_INSERT)

    with pytest.raises(RuntimeError, match="still holds MCP server registrations"):
        command.downgrade(config, "034")

    with engine.connect() as connection:
        assert "mcp_server_configs" in inspect(engine).get_table_names()
        row = connection.execute(text("SELECT ref, env FROM mcp_server_configs")).mappings().one()
        assert row == {"ref": "filesystem", "env": '{"API_KEY":"live-secret"}'}
        # The refusal has to beat the DDL, so the chain must not have moved
        # either -- a half-applied downgrade is its own outage.
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "035"
        )


def test_035_downgrade_of_an_empty_registry_is_a_plain_rollback(tmp_path: Path) -> None:
    """The case a release rollback actually hits stays a no-op, not an obstacle."""
    path = tmp_path / "empty.db"
    config = _config(path)
    command.upgrade(config, "035")

    command.downgrade(config, "034")

    engine = create_engine(f"sqlite:///{path}")
    assert "mcp_server_configs" not in inspect(engine).get_table_names()


def test_035_forced_downgrade_roundtrips_back_to_a_usable_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is what makes the difference, and what it leaves behind still works.

    Both halves are load-bearing. The escape hatch is only worth having if the
    table it drops can be upgraded into again -- ``CREATE TABLE IF NOT EXISTS``
    would happily no-op over a stale table and leave the registry silently
    unwritable -- and it is only an escape hatch at all if the same downgrade
    is refused without it.

    That refusal is why the run happens twice against one populated database.
    Setting the variable and asserting the drop succeeded cannot observe the
    flag being read: an unguarded ``DROP TABLE IF EXISTS``, which is exactly
    what this migration did before the guard, succeeds identically. Only the
    contrast distinguishes "the hatch opened" from "there was never a door".
    """
    path = tmp_path / "forced.db"
    config = _config(path)
    command.upgrade(config, "035")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(_INSERT)

    with pytest.raises(RuntimeError, match="still holds MCP server registrations"):
        command.downgrade(config, "034")
    assert "mcp_server_configs" in inspect(engine).get_table_names()

    monkeypatch.setenv(FORCE_DROP_ENV_VAR, "1")
    command.downgrade(config, "034")
    assert "mcp_server_configs" not in inspect(engine).get_table_names()

    monkeypatch.delenv(FORCE_DROP_ENV_VAR)
    command.upgrade(config, "035")

    with engine.begin() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM mcp_server_configs")).scalar() == 0
        connection.execute(_INSERT)
        assert connection.execute(text("SELECT command FROM mcp_server_configs")).scalar() == "npx"
