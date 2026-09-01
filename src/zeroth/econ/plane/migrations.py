"""Packaged Alembic runner for the economic-plane schema."""

from __future__ import annotations

ECON_VERSION_TABLE = "alembic_version_econ"


def run_econ_migrations(database_url: str) -> None:
    """Upgrade an economic database using migrations bundled in the wheel."""
    import importlib.resources

    from alembic import command
    from alembic.config import Config
    from zeroth.platform.storage.sqlalchemy_url import sqlalchemy_database_url

    alembic_cfg = Config()
    database_url = sqlalchemy_database_url(database_url)
    migrations_dir = str(importlib.resources.files("zeroth.econ.plane._migrations"))
    alembic_cfg.set_main_option("script_location", migrations_dir)
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    alembic_cfg.attributes["database_url_override"] = database_url
    alembic_cfg.attributes["version_table"] = ECON_VERSION_TABLE
    command.upgrade(alembic_cfg, "head")


__all__ = ["ECON_VERSION_TABLE", "run_econ_migrations"]
