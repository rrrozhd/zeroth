"""Database migration entry point for the service bootstrap."""

from __future__ import annotations


def run_migrations(database_url: str) -> None:
    """Run Alembic migrations against the given database URL."""
    import importlib.resources

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config()
    migrations_dir = str(importlib.resources.files("zeroth.core.migrations"))
    alembic_cfg.set_main_option("script_location", migrations_dir)
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_cfg, "head")
