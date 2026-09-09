from __future__ import annotations

import importlib

import pytest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_qualification_registry_migration_round_trip() -> None:
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260904_21_qualification_registry"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        assert "model_migration_qualifications" in inspector.get_table_names()
        assert {
            "qualification_id",
            "tenant_id",
            "record_version",
            "scope_digest",
            "active_scope_key",
            "workload",
            "incumbent_model",
            "candidate_model",
            "policy_digest",
            "algorithm_version",
            "cluster_family_set",
            "mean_probability_contract",
            "icc_upper_bound_exact",
            "cluster_size_vector",
            "cluster_size_vector_sha256",
            "authorization_count_threshold",
            "boundary_error_alpha_exact",
            "certificate_artifact_sha256",
            "family_confidence_set_method",
            "family_qualification_artifact_sha256",
            "icc_upper_confidence_method",
            "icc_qualification_artifact_sha256",
            "independent_unit_definition",
            "grouping_keys",
            "source_window_start",
            "source_window_end",
            "source_evidence_sha256",
            "issuer",
            "issued_at",
            "valid_from",
            "valid_until",
            "supersedes_qualification_id",
            "record_digest",
            "revoked_at",
            "revoked_by",
            "revocation_digest",
            "superseded_at",
            "superseded_by",
            "superseded_by_qualification_id",
        } == {column["name"] for column in inspector.get_columns("model_migration_qualifications")}
        migration.downgrade()
        assert "model_migration_qualifications" not in inspect(connection).get_table_names()
        migration.upgrade()
        assert "model_migration_qualifications" in inspect(connection).get_table_names()


@pytest.mark.parametrize("parent", ["20260907_25", "20260904_21"])
def test_merged_head_upgrades_both_histories_without_losing_rows(tmp_path, parent):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from sqlalchemy import text

    database_url = f"sqlite:///{tmp_path / 'merge.db'}"
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src/zeroth/econ/plane/_migrations"),
    )
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url_override"] = database_url
    command.upgrade(config, parent)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE retained_value (value TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO retained_value VALUES ('preserved')"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260908_26"
        assert connection.scalar(text("SELECT value FROM retained_value")) == "preserved"
        assert "model_migration_qualifications" in inspect(connection).get_table_names()
    engine.dispose()
