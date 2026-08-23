"""Add Stage 6 product identity and pilot experience records.

Revision ID: 0007_stage6_product_experience
Revises: 0006_stage5_grounding_performance
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_stage6_product_experience"
down_revision: str | Sequence[str] | None = "0006_stage5_grounding_performance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "role" not in _columns("campus_users"):
        with op.batch_alter_table("campus_users") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "role",
                    sa.String(length=24),
                    nullable=False,
                    server_default="student",
                )
            )

    if "product_subjects" not in tables:
        op.create_table(
            "product_subjects",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("subject_kind", sa.String(length=24), nullable=False),
            sa.Column("campus_user_id", sa.String(length=64), nullable=True),
            sa.Column("merged_into_subject_id", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["campus_user_id"],
                ["campus_users.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["merged_into_subject_id"],
                ["product_subjects.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("campus_user_id"),
        )
        op.create_index(
            "ix_product_subjects_subject_kind",
            "product_subjects",
            ["subject_kind"],
            unique=False,
        )
        op.create_index(
            "ix_product_subjects_campus_user_id",
            "product_subjects",
            ["campus_user_id"],
            unique=True,
        )
        op.create_index(
            "ix_product_subjects_merged_into_subject_id",
            "product_subjects",
            ["merged_into_subject_id"],
            unique=False,
        )

    op.execute(
        sa.text(
            """
            INSERT INTO product_subjects (
                id, subject_kind, campus_user_id, merged_into_subject_id,
                status, created_at, last_seen_at, invalidated_at
            )
            SELECT
                'psub_' || substr(id, 5), 'campus', id, NULL,
                'active', created_at, last_login_at, NULL
            FROM campus_users
            WHERE NOT EXISTS (
                SELECT 1 FROM product_subjects
                WHERE product_subjects.campus_user_id = campus_users.id
            )
            """
        )
    )

    if "visitor_sessions" not in tables:
        op.create_table(
            "visitor_sessions",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("csrf_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["subject_id"],
                ["product_subjects.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_visitor_sessions_subject_id",
            "visitor_sessions",
            ["subject_id"],
            unique=False,
        )
        op.create_index(
            "ix_visitor_sessions_token_hash",
            "visitor_sessions",
            ["token_hash"],
            unique=True,
        )
        op.create_index(
            "ix_visitor_sessions_expires_at",
            "visitor_sessions",
            ["expires_at"],
            unique=False,
        )

    if "owner_subject_id" not in _columns("conversations"):
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.add_column(
                sa.Column("owner_subject_id", sa.String(length=64), nullable=True)
            )
            batch_op.create_foreign_key(
                "fk_conversations_owner_subject_id_product_subjects",
                "product_subjects",
                ["owner_subject_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch_op.create_index(
                "ix_conversations_owner_subject_id",
                ["owner_subject_id"],
                unique=False,
            )
        op.execute(
            sa.text(
                """
                UPDATE conversations
                SET owner_subject_id = (
                    SELECT id FROM product_subjects
                    WHERE product_subjects.campus_user_id = conversations.owner_user_id
                )
                WHERE owner_user_id IS NOT NULL
                """
            )
        )

    if "client_message_id" not in _columns("messages"):
        with op.batch_alter_table("messages") as batch_op:
            batch_op.add_column(
                sa.Column("client_message_id", sa.String(length=120), nullable=True)
            )
            batch_op.create_unique_constraint(
                "uq_message_conversation_client_id",
                ["conversation_id", "client_message_id"],
            )

    task_columns = _columns("agent_tasks")
    if {
        "request_mode",
        "parent_task_id",
        "requested_by_subject_id",
    } - task_columns:
        with op.batch_alter_table("agent_tasks") as batch_op:
            if "request_mode" not in task_columns:
                batch_op.add_column(
                    sa.Column(
                        "request_mode",
                        sa.String(length=32),
                        nullable=False,
                        server_default="normal",
                    )
                )
            if "parent_task_id" not in task_columns:
                batch_op.add_column(
                    sa.Column("parent_task_id", sa.String(length=64), nullable=True)
                )
                batch_op.create_foreign_key(
                    "fk_agent_tasks_parent_task_id",
                    "agent_tasks",
                    ["parent_task_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch_op.create_index(
                    "ix_agent_tasks_parent_task_id",
                    ["parent_task_id"],
                    unique=False,
                )
            if "requested_by_subject_id" not in task_columns:
                batch_op.add_column(
                    sa.Column(
                        "requested_by_subject_id",
                        sa.String(length=64),
                        nullable=True,
                    )
                )
                batch_op.create_foreign_key(
                    "fk_agent_tasks_requested_by_subject_id",
                    "product_subjects",
                    ["requested_by_subject_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch_op.create_index(
                    "ix_agent_tasks_requested_by_subject_id",
                    ["requested_by_subject_id"],
                    unique=False,
                )

    if "student_profiles" not in tables:
        op.create_table(
            "student_profiles",
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column(
                "personalization_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "onboarding_completed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["subject_id"],
                ["product_subjects.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("subject_id"),
        )

    if "profile_attributes" not in tables:
        op.create_table(
            "profile_attributes",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column("attribute_key", sa.String(length=48), nullable=False),
            sa.Column("attribute_value", sa.String(length=240), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("source_kind", sa.String(length=32), nullable=False),
            sa.Column("supporting_user_text", sa.Text(), nullable=False),
            sa.Column("source_answer_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["subject_id"],
                ["product_subjects.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["source_answer_id"],
                ["answers.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_profile_attributes_subject_id",
            "profile_attributes",
            ["subject_id"],
            unique=False,
        )
        op.create_index(
            "ix_profile_attributes_source_answer_id",
            "profile_attributes",
            ["source_answer_id"],
            unique=False,
        )
        op.create_index(
            "ix_profile_attributes_subject_status",
            "profile_attributes",
            ["subject_id", "status"],
            unique=False,
        )

    if "user_todos" not in tables:
        op.create_table(
            "user_todos",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("notes", sa.Text(), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("source_answer_id", sa.String(length=64), nullable=True),
            sa.Column("source_action_index", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["subject_id"],
                ["product_subjects.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["source_answer_id"],
                ["answers.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_user_todos_subject_id",
            "user_todos",
            ["subject_id"],
            unique=False,
        )
        op.create_index(
            "ix_user_todos_source_answer_id",
            "user_todos",
            ["source_answer_id"],
            unique=False,
        )
        op.create_index(
            "ix_user_todos_subject_status",
            "user_todos",
            ["subject_id", "status"],
            unique=False,
        )

    if "answer_feedback" not in tables:
        op.create_table(
            "answer_feedback",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column("answer_id", sa.String(length=64), nullable=False),
            sa.Column("rating", sa.String(length=24), nullable=False),
            sa.Column("categories", sa.JSON(), nullable=False),
            sa.Column("comment", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["subject_id"],
                ["product_subjects.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["answer_id"],
                ["answers.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "subject_id",
                "answer_id",
                name="uq_feedback_subject_answer",
            ),
        )
        op.create_index(
            "ix_answer_feedback_subject_id",
            "answer_feedback",
            ["subject_id"],
            unique=False,
        )
        op.create_index(
            "ix_answer_feedback_answer_id",
            "answer_feedback",
            ["answer_id"],
            unique=False,
        )
        op.create_index(
            "ix_answer_feedback_rating_created",
            "answer_feedback",
            ["rating", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("answer_feedback")
    op.drop_table("user_todos")
    op.drop_table("profile_attributes")
    op.drop_table("student_profiles")

    with op.batch_alter_table("agent_tasks") as batch_op:
        batch_op.drop_index("ix_agent_tasks_requested_by_subject_id")
        batch_op.drop_constraint(
            "fk_agent_tasks_requested_by_subject_id",
            type_="foreignkey",
        )
        batch_op.drop_column("requested_by_subject_id")
        batch_op.drop_index("ix_agent_tasks_parent_task_id")
        batch_op.drop_constraint("fk_agent_tasks_parent_task_id", type_="foreignkey")
        batch_op.drop_column("parent_task_id")
        batch_op.drop_column("request_mode")

    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_constraint(
            "uq_message_conversation_client_id",
            type_="unique",
        )
        batch_op.drop_column("client_message_id")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_index("ix_conversations_owner_subject_id")
        batch_op.drop_constraint(
            "fk_conversations_owner_subject_id_product_subjects",
            type_="foreignkey",
        )
        batch_op.drop_column("owner_subject_id")

    op.drop_table("visitor_sessions")
    op.drop_table("product_subjects")
    with op.batch_alter_table("campus_users") as batch_op:
        batch_op.drop_column("role")
