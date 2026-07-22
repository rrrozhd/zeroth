from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base


class DashboardView(Base):
    __tablename__ = "dashboard_views"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    viewer_sub: Mapped[str] = mapped_column(String(128), index=True)
    view_name: Mapped[str] = mapped_column(String(64))
