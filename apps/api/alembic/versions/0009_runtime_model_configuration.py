"""Add the server-wide runtime model configuration.

Revision ID: 0009_runtime_model_configuration
Revises: 0008_answer_evidence_provenance
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_runtime_model_configuration"
down_revision: str | Sequence[str] | None = "0008_answer_evidence_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_model_configurations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("protocol", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("api_key_hint", sa.String(length=16), nullable=False),
        sa.Column("agent_model", sa.String(length=160), nullable=False),
        sa.Column("utility_model", sa.String(length=160), nullable=False),
        sa.Column(
            "reasoning_effort",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "utility_reasoning_effort",
            sa.String(length=16),
            nullable=False,
            server_default="low",
        ),
        sa.Column(
            "timeout_seconds",
            sa.Float(),
            nullable=False,
            server_default="60",
        ),
        sa.Column("updated_by_user_id", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["campus_users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runtime_model_configurations_updated_by_user_id",
        "runtime_model_configurations",
        ["updated_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_model_configurations_updated_by_user_id",
        table_name="runtime_model_configurations",
    )
    op.drop_table("runtime_model_configurations")
