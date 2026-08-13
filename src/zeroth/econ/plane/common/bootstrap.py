from sqlalchemy import select

from zeroth.econ.plane.auth.models import Role
from zeroth.econ.plane.database import Base, SessionLocal, _ensure_sqlite_compat, engine


def bootstrap() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_compat()
    with SessionLocal() as db:
        for role_name in ("Admin", "Analyst", "Approver", "Viewer"):
            existing = db.execute(select(Role).where(Role.name == role_name)).scalar_one_or_none()
            if existing is None:
                db.add(Role(name=role_name))
        db.commit()
