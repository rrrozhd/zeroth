from __future__ import annotations

import importlib.resources
import sqlite3

from alembic import command
from alembic.config import Config

from zeroth.service.bootstrap.migrations import run_migrations


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(importlib.resources.files("zeroth.service._migrations"))
    )
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_legacy_campaign_revision_029_is_bridged_without_recreating_its_tables(tmp_path) -> None:
    """Old 027-029 were renumbered to 030-032 when two histories merged."""
    path = tmp_path / "legacy-campaign.db"
    url = f"sqlite:///{path}"
    command.upgrade(_config(url), "032")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE app_certification_events;
            DROP TABLE app_certifications;
            DROP TABLE guardrail_admission_state;
            DROP TABLE guardrail_policy_revisions;
            UPDATE alembic_version SET version_num = '029';
            """
        )

    run_migrations(url)

    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version == "035"
    assert {
        "app_certifications",
        "guardrail_policy_revisions",
        "prompt_templates",
        "template_dependency_references",
        "mcp_server_configs",
    } <= tables
