"""Create the Stage 1 conversation, task, answer and evidence schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("profile_context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_messages_conversation_id",
        "messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("user_message_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("answer_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_tasks_conversation_id",
        "agent_tasks",
        ["conversation_id"],
        unique=False,
    )
    op.create_table(
        "answers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("headline", sa.String(length=240), nullable=False),
        sa.Column("answer_markdown", sa.Text(), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("next_actions", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.String(length=24), nullable=False),
        sa.Column("verification_mode", sa.String(length=32), nullable=False),
        sa.Column("model_provider", sa.String(length=40), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_answers_task_id", "answers", ["task_id"], unique=True)
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("answer_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("publisher", sa.String(length=200), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fresh_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("resource_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_answer_id",
        "evidence",
        ["answer_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_answer_id", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_answers_task_id", table_name="answers")
    op.drop_table("answers")
    op.drop_index("ix_agent_tasks_conversation_id", table_name="agent_tasks")
    op.drop_table("agent_tasks")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("conversations")
