"""Preserve answer evidence provenance metadata.

Revision ID: 0008_answer_evidence_provenance
Revises: 0007_stage6_product_experience
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_answer_evidence_provenance"
down_revision: str | Sequence[str] | None = "0007_stage6_product_experience"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    columns = _columns("evidence")
    with op.batch_alter_table("evidence") as batch_op:
        if "authority_level" not in columns:
            batch_op.add_column(
                sa.Column(
                    "authority_level",
                    sa.String(length=32),
                    nullable=False,
                    server_default="unknown",
                )
            )
        if "audience_scopes" not in columns:
            batch_op.add_column(
                sa.Column(
                    "audience_scopes",
                    sa.JSON(),
                    nullable=False,
                    server_default="[]",
                )
            )
        if "effective_from" not in columns:
            batch_op.add_column(
                sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True)
            )
        if "effective_to" not in columns:
            batch_op.add_column(
                sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True)
            )
        if "retrieval_mode" not in columns:
            batch_op.add_column(
                sa.Column(
                    "retrieval_mode",
                    sa.String(length=32),
                    nullable=False,
                    server_default="unknown",
                )
            )


def downgrade() -> None:
    columns = _columns("evidence")
    with op.batch_alter_table("evidence") as batch_op:
        if "retrieval_mode" in columns:
            batch_op.drop_column("retrieval_mode")
        if "effective_to" in columns:
            batch_op.drop_column("effective_to")
        if "effective_from" in columns:
            batch_op.drop_column("effective_from")
        if "audience_scopes" in columns:
            batch_op.drop_column("audience_scopes")
        if "authority_level" in columns:
            batch_op.drop_column("authority_level")
