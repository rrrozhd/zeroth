"""Database migration entry point for the service bootstrap."""

from __future__ import annotations


def run_migrations(database_url: str) -> None:
    """Run Alembic migrations against the given database URL."""
    import importlib.resources

    from alembic import command
    from alembic.config import Config

    from zeroth.platform.storage.sqlalchemy_url import sqlalchemy_database_url

    database_url = sqlalchemy_database_url(database_url)
    alembic_cfg = Config()
    migrations_dir = str(importlib.resources.files("zeroth.service._migrations"))
    alembic_cfg.set_main_option("script_location", migrations_dir)
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    _bridge_legacy_campaign_schema(alembic_cfg, database_url)
    command.upgrade(alembic_cfg, "head")


def _bridge_legacy_campaign_schema(alembic_cfg: object, database_url: str) -> None:
    """Repair the one released revision collision without guessing.

    The evaluation campaign originally shipped prompt templates, parent-run
    lineage, and template dependencies as revisions 027-029. When upstream
    migrations were merged those same migrations became 030-032, while 027-029
    were reassigned to app certifications and guardrails. A persistent pilot
    database therefore truthfully says ``029`` for the old history but looks
    stale to the merged chain, which then tries to create its existing tables.

    Only the exact SQLite fingerprint is bridged. Partial schemas and databases
    that already contain either new lineage are left untouched (and normal
    Alembic failure remains fail-closed).
    """
    if not database_url.startswith("sqlite:///"):
        return

    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "alembic_version" not in tables:
            return
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if revision != "029":
            return

        campaign_tables = {"prompt_templates", "template_dependency_references"}
        campaign_columns = (
            "runs" in tables
            and "parent_run_id" in {column["name"] for column in inspector.get_columns("runs")}
        )
        old_campaign_complete = campaign_tables <= tables and campaign_columns
        new_lineage_present = bool(
            {"app_certifications", "app_certification_events", "guardrail_policy_revisions"}
            & tables
        )
        if not old_campaign_complete or new_lineage_present:
            return

        # Replay the missing reassigned revisions, then acknowledge that the
        # already-present campaign tables are today's 030-032 before continuing.
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '026'"))
        command.upgrade(alembic_cfg, "029")
        command.stamp(alembic_cfg, "032")
    finally:
        engine.dispose()
