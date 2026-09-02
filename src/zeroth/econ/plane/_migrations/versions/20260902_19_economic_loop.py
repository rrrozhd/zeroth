"""Add fresh probabilistic schedules, randomized verification, and calibration.

Revision ID: 20260902_19
Revises: 20260901_18
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_19"
down_revision = "20260901_18"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _table_names()
    decision_columns = {
        row["name"]
        for row in sa.inspect(op.get_bind()).get_columns(
            "probabilistic_migration_decisions"
        )
    }
    if "evidence_lineage_json" not in decision_columns:
        with op.batch_alter_table("probabilistic_migration_decisions") as batch:
            batch.add_column(
                sa.Column(
                    "evidence_lineage_json",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
    if "probabilistic_decision_schedules" not in tables:
        op.create_table(
            "probabilistic_decision_schedules",
            sa.Column("schedule_id", sa.String(40), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("evidence_source_json", sa.JSON(), nullable=False),
            sa.Column("policy_json", sa.JSON(), nullable=False),
            sa.Column("simulations", sa.Integer(), nullable=False),
            sa.Column("seed", sa.Integer(), nullable=False),
            sa.Column("interval_minutes", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("next_run_at", sa.DateTime(), nullable=False),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("last_decision_id", sa.String(40), nullable=True),
            sa.Column("last_error", sa.String(512), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=False),
        )
        op.create_index(
            "ix_probabilistic_decision_schedules_tenant_due",
            "probabilistic_decision_schedules",
            ["tenant_id", "active", "next_run_at"],
        )
        for column in ("tenant_id", "next_run_at"):
            op.create_index(
                f"ix_probabilistic_decision_schedules_{column}",
                "probabilistic_decision_schedules",
                [column],
            )
    if "randomized_rollouts" not in tables:
        op.create_table(
            "randomized_rollouts",
            sa.Column("rollout_id", sa.String(40), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("decision_id", sa.String(40), nullable=False),
            sa.Column("workload", sa.String(128), nullable=False),
            sa.Column("incumbent_model", sa.String(255), nullable=False),
            sa.Column("candidate_model", sa.String(255), nullable=False),
            sa.Column("candidate_probability", sa.Float(), nullable=False),
            sa.Column("cohort_probabilities_json", sa.JSON(), nullable=False),
            sa.Column("assignment_salt", sa.String(128), nullable=False),
            sa.Column("minimum_per_arm", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=False),
        )
        for column in ("tenant_id", "decision_id", "workload"):
            op.create_index(
                f"ix_randomized_rollouts_{column}", "randomized_rollouts", [column]
            )
    if "randomized_rollout_assignments" not in tables:
        op.create_table(
            "randomized_rollout_assignments",
            sa.Column("assignment_id", sa.String(40), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("rollout_id", sa.String(40), nullable=False),
            sa.Column("subject_id", sa.String(192), nullable=False),
            sa.Column("cohort", sa.String(128), nullable=False),
            sa.Column("arm", sa.String(16), nullable=False),
            sa.Column("assigned_model", sa.String(255), nullable=False),
            sa.Column("assigned_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "uq_randomized_rollout_assignment_subject",
            "randomized_rollout_assignments",
            ["tenant_id", "rollout_id", "subject_id"],
            unique=True,
        )
        for column in ("tenant_id", "rollout_id"):
            op.create_index(
                f"ix_randomized_rollout_assignments_{column}",
                "randomized_rollout_assignments",
                [column],
            )
    if "randomized_rollout_verifications" not in tables:
        op.create_table(
            "randomized_rollout_verifications",
            sa.Column("verification_id", sa.String(40), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("rollout_id", sa.String(40), nullable=False),
            sa.Column("request_digest", sa.String(64), nullable=False),
            sa.Column("report_json", sa.JSON(), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=False),
            sa.Column("verified_by", sa.String(128), nullable=False),
        )
        for column in ("tenant_id", "rollout_id", "verified_at"):
            op.create_index(
                f"ix_randomized_rollout_verifications_{column}",
                "randomized_rollout_verifications",
                [column],
            )
    if "forecast_calibration_observations" not in tables:
        op.create_table(
            "forecast_calibration_observations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("forecast_id", sa.String(128), nullable=False),
            sa.Column("verification_id", sa.String(40), nullable=False),
            sa.Column("workload", sa.String(128), nullable=False),
            sa.Column("incumbent_model", sa.String(255), nullable=False),
            sa.Column("candidate_model", sa.String(255), nullable=False),
            sa.Column("metric", sa.String(128), nullable=False),
            sa.Column("predicted_mean", sa.Float(), nullable=False),
            sa.Column("predicted_low", sa.Float(), nullable=False),
            sa.Column("predicted_high", sa.Float(), nullable=False),
            sa.Column("observed", sa.Float(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "uq_forecast_calibration_verification_metric",
            "forecast_calibration_observations",
            ["tenant_id", "verification_id", "metric"],
            unique=True,
        )
        for column in ("tenant_id", "verification_id", "workload", "observed_at"):
            op.create_index(
                f"ix_forecast_calibration_observations_{column}",
                "forecast_calibration_observations",
                [column],
            )


def downgrade() -> None:
    tables = _table_names()
    for table in (
        "forecast_calibration_observations",
        "randomized_rollout_verifications",
        "randomized_rollout_assignments",
        "randomized_rollouts",
        "probabilistic_decision_schedules",
    ):
        if table in tables:
            op.drop_table(table)
    if "probabilistic_migration_decisions" in tables:
        columns = {
            row["name"]
            for row in sa.inspect(op.get_bind()).get_columns(
                "probabilistic_migration_decisions"
            )
        }
        if "evidence_lineage_json" in columns:
            with op.batch_alter_table("probabilistic_migration_decisions") as batch:
                batch.drop_column("evidence_lineage_json")
