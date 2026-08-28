"""Add response styles, questions, contributors and curated knowledge."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_community_questions_knowledge"
down_revision: str | Sequence[str] | None = "0012_agent_admission_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "response_style", sa.String(length=24), nullable=False, server_default="neutral"
            )
        )
    with op.batch_alter_table("answers") as batch_op:
        batch_op.add_column(sa.Column("question_offer_reason", sa.String(length=64), nullable=True))

    op.create_table(
        "local_contributor_credentials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=160), nullable=False),
        sa.Column("public_name", sa.String(length=120), nullable=False),
        sa.Column("unit", sa.String(length=200), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["campus_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index(
        "ix_local_contributor_credentials_user_id", "local_contributor_credentials", ["user_id"]
    )
    op.create_index(
        "ix_local_contributor_credentials_username", "local_contributor_credentials", ["username"]
    )

    op.create_table(
        "community_questions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("answer_id", sa.String(length=64), nullable=True),
        sa.Column("owner_subject_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("evidence_gap", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending_review"),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_subject_id"], ["product_subjects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["campus_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("answer_id", name="uq_community_question_answer"),
    )
    op.create_index("ix_community_questions_answer_id", "community_questions", ["answer_id"])
    op.create_index(
        "ix_community_questions_owner_subject_id", "community_questions", ["owner_subject_id"]
    )
    op.create_index(
        "ix_community_questions_status_created", "community_questions", ["status", "created_at"]
    )

    op.create_table(
        "community_answers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("question_id", sa.String(length=64), nullable=False),
        sa.Column("contributor_user_id", sa.String(length=64), nullable=False),
        sa.Column("answer_markdown", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="visible"),
        sa.Column(
            "knowledge_review_state",
            sa.String(length=32),
            nullable=False,
            server_default="not_reviewed",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["community_questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contributor_user_id"], ["campus_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "question_id", "contributor_user_id", name="uq_community_answer_question_contributor"
        ),
    )
    op.create_index("ix_community_answers_question_id", "community_answers", ["question_id"])
    op.create_index(
        "ix_community_answers_contributor_user_id", "community_answers", ["contributor_user_id"]
    )
    op.create_index(
        "ix_community_answers_question_status", "community_answers", ["question_id", "status"]
    )

    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("question_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("canonical_question", sa.Text(), nullable=False),
        sa.Column("answer_markdown", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False, server_default="校园综合"),
        sa.Column("alternative_phrasings", sa.JSON(), nullable=False),
        sa.Column("applicable_scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("maintainer_unit", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("basis_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("validity", sa.String(length=24), nullable=False, server_default="stable"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("visibility", sa.String(length=24), nullable=False, server_default="public"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("published_source_id", sa.String(length=120), nullable=True),
        sa.Column("published_resource_id", sa.String(length=64), nullable=True),
        sa.Column("published_version_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["question_id"], ["community_questions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["campus_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("question_id", name="uq_knowledge_entry_question"),
    )
    op.create_index("ix_knowledge_entries_question_id", "knowledge_entries", ["question_id"])
    op.create_index(
        "ix_knowledge_entries_created_by_user_id", "knowledge_entries", ["created_by_user_id"]
    )
    op.create_index(
        "ix_knowledge_entries_status_visibility", "knowledge_entries", ["status", "visibility"]
    )

    op.create_table(
        "knowledge_entry_origins",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_entry_id", sa.String(length=64), nullable=False),
        sa.Column("community_answer_id", sa.String(length=64), nullable=True),
        sa.Column("origin_kind", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["knowledge_entry_id"], ["knowledge_entries.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["community_answer_id"], ["community_answers.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_entry_id", "community_answer_id", name="uq_knowledge_origin"
        ),
    )
    op.create_index(
        "ix_knowledge_entry_origins_knowledge_entry_id",
        "knowledge_entry_origins",
        ["knowledge_entry_id"],
    )
    op.create_index(
        "ix_knowledge_entry_origins_community_answer_id",
        "knowledge_entry_origins",
        ["community_answer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_entry_origins_community_answer_id", table_name="knowledge_entry_origins"
    )
    op.drop_index(
        "ix_knowledge_entry_origins_knowledge_entry_id", table_name="knowledge_entry_origins"
    )
    op.drop_table("knowledge_entry_origins")
    op.drop_index("ix_knowledge_entries_status_visibility", table_name="knowledge_entries")
    op.drop_index("ix_knowledge_entries_created_by_user_id", table_name="knowledge_entries")
    op.drop_index("ix_knowledge_entries_question_id", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
    op.drop_index("ix_community_answers_question_status", table_name="community_answers")
    op.drop_index("ix_community_answers_contributor_user_id", table_name="community_answers")
    op.drop_index("ix_community_answers_question_id", table_name="community_answers")
    op.drop_table("community_answers")
    op.drop_index("ix_community_questions_status_created", table_name="community_questions")
    op.drop_index("ix_community_questions_owner_subject_id", table_name="community_questions")
    op.drop_index("ix_community_questions_answer_id", table_name="community_questions")
    op.drop_table("community_questions")
    op.drop_index(
        "ix_local_contributor_credentials_username", table_name="local_contributor_credentials"
    )
    op.drop_index(
        "ix_local_contributor_credentials_user_id", table_name="local_contributor_credentials"
    )
    op.drop_table("local_contributor_credentials")
    with op.batch_alter_table("answers") as batch_op:
        batch_op.drop_column("question_offer_reason")
    with op.batch_alter_table("agent_tasks") as batch_op:
        batch_op.drop_column("response_style")
