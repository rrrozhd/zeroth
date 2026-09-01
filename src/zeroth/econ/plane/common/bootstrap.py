from sqlalchemy import select

from zeroth.econ.plane.auth.models import Role
from zeroth.econ.plane.billing import models as _billing_models  # noqa: F401
from zeroth.econ.plane.database import Base, SessionLocal, _ensure_sqlite_compat, engine
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
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_compat()
    with SessionLocal() as db:
        for role_name in ("Admin", "Analyst", "Approver", "Viewer"):
            existing = db.execute(select(Role).where(Role.name == role_name)).scalar_one_or_none()
            if existing is None:
                db.add(Role(name=role_name))
        db.commit()
    _schema_revision = read_schema_revision(engine, _MIGRATIONS_PACKAGE)


def schema_revision() -> SchemaRevision:
    """Return startup-captured evidence without querying on the health path."""
    return _schema_revision
