from sqlalchemy import select

from zeroth.econ.plane.auth.models import Role
from zeroth.econ.plane.billing import models as _billing_models  # noqa: F401
from zeroth.econ.plane.database import Base, SessionLocal, _ensure_sqlite_compat, engine
from zeroth.econ.plane.instrumentation.models import ChargeCostRevisionRecord
from zeroth.econ.plane.migrations import ECON_VERSION_TABLE
from zeroth.platform.storage.schema_revision import (
    SchemaRevision,
    read_schema_revision,
    unknown_schema_revision,
)

_MIGRATIONS_PACKAGE = "zeroth.econ.plane._migrations"
_schema_revision = unknown_schema_revision(_MIGRATIONS_PACKAGE)


def bootstrap() -> None:
    """Initialize the econ store and capture its bounded revision evidence."""
    global _schema_revision
    # Preserve legacy parent-table creation: some offline constraints depend on
    # those tables. The new revision FK must wait for its parent's migrated
    # charge columns, or it masks the actionable schema-convergence refusal.
    revision_table = ChargeCostRevisionRecord.__table__
    Base.metadata.create_all(
        bind=engine, tables=[table for table in Base.metadata.sorted_tables if table is not revision_table],
    )
    _ensure_sqlite_compat()
    revision_table.create(bind=engine, checkfirst=True)
    with SessionLocal() as db:
        for role_name in ("Admin", "Analyst", "Approver", "Viewer"):
            existing = db.execute(select(Role).where(Role.name == role_name)).scalar_one_or_none()
            if existing is None:
                db.add(Role(name=role_name))
        db.commit()
    _schema_revision = read_schema_revision(
        engine, _MIGRATIONS_PACKAGE, version_table=ECON_VERSION_TABLE
    )
    if _schema_revision.applied is None:
        # Backward compatibility for existing standalone econ databases. A
        # foreign service revision remains ``unknown`` under classification.
        legacy = read_schema_revision(engine, _MIGRATIONS_PACKAGE)
        if legacy.state != "unknown":
            _schema_revision = legacy


def schema_revision() -> SchemaRevision:
    """Return startup-captured evidence without querying on the health path."""
    return _schema_revision
