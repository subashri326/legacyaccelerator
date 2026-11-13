"""catalog attributes: column and table details that change behaviour

Adds ``attributes`` (JSON) to db_object and db_column, e.g. a Teradata column that compares
case-insensitively, Unicode text, or a table whose row key is a unique primary index.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    for table in ("db_object", "db_column"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("attributes", _JSON, nullable=False, server_default="{}"))


def downgrade() -> None:
    for table in ("db_column", "db_object"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("attributes")
