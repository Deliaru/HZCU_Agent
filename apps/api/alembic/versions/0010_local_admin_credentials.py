"""Add the independent local administrator credential.

Revision ID: 0010_local_admin_credentials
Revises: 0009_runtime_model_configuration
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_local_admin_credentials"
down_revision: str | Sequence[str] | None = "0009_runtime_model_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_admin_credentials",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=160), nullable=False),
        sa.Column("subject_hint", sa.String(length=24), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_local_admin_credentials_username"),
    )


def downgrade() -> None:
    op.drop_table("local_admin_credentials")
