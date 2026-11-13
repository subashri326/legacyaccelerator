"""saved connections per client, connection test results, pipeline runs

* connection: may belong to a client (reusable by its migrations) instead of one project;
  records the last "Test connection" result.
* pipeline_run: one row per Run with the progress of each stage, for live progress screens.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("connection") as batch:
        batch.add_column(sa.Column("client_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("last_test_status", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("last_test_message", sa.Text(), nullable=True))
        batch.add_column(sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column("project_id", existing_type=sa.String(length=36), nullable=True)
        batch.create_foreign_key(
            "fk_connection_client_id_client", "client", ["client_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_unique_constraint("uq_conn_client_name", ["client_id", "name"])

    op.create_table(
        "pipeline_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_stage", sa.String(length=30), nullable=True),
        sa.Column("stages", _JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pipeline_run_project", "pipeline_run", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_run_project", table_name="pipeline_run")
    op.drop_table("pipeline_run")
    with op.batch_alter_table("connection") as batch:
        batch.drop_constraint("uq_conn_client_name", type_="unique")
        batch.drop_constraint("fk_connection_client_id_client", type_="foreignkey")
        batch.alter_column("project_id", existing_type=sa.String(length=36), nullable=False)
        batch.drop_column("last_tested_at")
        batch.drop_column("last_test_message")
        batch.drop_column("last_test_status")
        batch.drop_column("client_id")
