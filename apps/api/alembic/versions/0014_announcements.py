"""Add immutable announcements and per-subject read receipts."""

import sqlalchemy as sa

from alembic import op

revision = "0014_announcements"
down_revision = "0013_community_questions_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "announcement_reads",
        sa.Column(
            "announcement_id",
            sa.String(64),
            sa.ForeignKey("announcements.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "subject_id",
            sa.String(64),
            sa.ForeignKey("product_subjects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("announcement_reads")
    op.drop_table("announcements")
