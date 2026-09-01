"""Read-only Alembic revision evidence shared by service health surfaces."""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from zeroth.platform.storage.database import AsyncDatabase

ALEMBIC_VERSION_TABLE = "alembic_version"
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _version_query(version_table: str) -> str:
    if _SQL_IDENTIFIER.fullmatch(version_table) is None:
        raise ValueError(f"invalid Alembic version table: {version_table!r}")
    return f"SELECT version_num FROM {version_table} LIMIT 2"


class SchemaRevision(BaseModel):
    """One schema's applied revision, shipped head, and conservative state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: str | None
    head: str | None
    state: Literal["current", "behind", "unknown"]


def _scripts(migrations_package: str) -> ScriptDirectory:
    locations = tuple(importlib.import_module(migrations_package).__path__)
    if len(locations) != 1:
        raise ValueError("migration package must have one filesystem location")
    config = Config()
    config.set_main_option("script_location", locations[0])
    return ScriptDirectory.from_config(config)


def _unknown(scripts: ScriptDirectory | None, applied: str | None = None) -> SchemaRevision:
    heads = scripts.get_heads() if scripts is not None else ()
    head = heads[0] if len(heads) == 1 else None
    return SchemaRevision(applied=applied, head=head, state="unknown")


def classify_schema_revision(
    applied_heads: Iterable[str], scripts: ScriptDirectory
) -> SchemaRevision:
    """Classify one linear applied head without guessing about foreign branches."""
    applied_values = tuple(applied_heads)
    heads = scripts.get_heads()
    if len(heads) != 1 or len(applied_values) != 1:
        return _unknown(scripts)

    applied = applied_values[0]
    head = heads[0]
    if applied == head:
        return SchemaRevision(applied=applied, head=head, state="current")

    try:
        lineage = {
            revision.revision
            for revision in scripts.iterate_revisions(head, applied, inclusive=True)
        }
    except Exception:  # Alembic uses several resolution/range exception types.
        return _unknown(scripts, applied)
    if applied in lineage:
        return SchemaRevision(applied=applied, head=head, state="behind")
    return _unknown(scripts, applied)


def unknown_schema_revision(migrations_package: str) -> SchemaRevision:
    """Return unknown evidence while retaining the shipped head when readable."""
    try:
        return _unknown(_scripts(migrations_package))
    except Exception:
        return _unknown(None)


async def read_async_schema_revision(
    database: AsyncDatabase, migrations_package: str
) -> SchemaRevision:
    """Read at most two applied heads through the service database seam."""
    try:
        scripts = _scripts(migrations_package)
    except Exception:
        return _unknown(None)
    try:
        async with database.transaction() as connection:
            rows = await connection.fetch_all(_version_query(ALEMBIC_VERSION_TABLE))
        applied = tuple(str(row["version_num"]) for row in rows)
    except Exception:
        return _unknown(scripts)
    return classify_schema_revision(applied, scripts)


def read_schema_revision(
    engine: Engine,
    migrations_package: str,
    *,
    version_table: str = ALEMBIC_VERSION_TABLE,
) -> SchemaRevision:
    """Read at most two applied heads from a synchronous SQLAlchemy engine."""
    try:
        scripts = _scripts(migrations_package)
    except Exception:
        return _unknown(None)
    try:
        with engine.connect() as connection:
            applied = tuple(
                str(row[0])
                for row in connection.execute(text(_version_query(version_table)))
            )
    except Exception:
        return _unknown(scripts)
    return classify_schema_revision(applied, scripts)
