"""Add optional CAS identity, opaque sessions and task access scopes.

Revision ID: 0004_campus_identity
Revises: 0003_semantic_index
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_campus_identity"
down_revision: str | Sequence[str] | None = "0003_semantic_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Development uses metadata.create_all so a developer may have the new tables
    # before Alembic advances from 0003. Keep the upgrade safe for that supported
    # path while retaining the normal create path for production databases.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "campus_users" not in tables:
        op.create_table(
            "campus_users",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("identity_provider", sa.String(length=48), nullable=False),
            sa.Column("subject_hash", sa.String(length=64), nullable=False),
            sa.Column("subject_hint", sa.String(length=24), nullable=True),
            sa.Column("access_scopes", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "identity_provider",
                "subject_hash",
                name="uq_campus_user_provider_subject",
            ),
        )
    if "auth_sessions" not in tables:
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("csrf_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["campus_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_auth_sessions_user_id",
            "auth_sessions",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_auth_sessions_token_hash",
            "auth_sessions",
            ["token_hash"],
            unique=True,
        )
        op.create_index(
            "ix_auth_sessions_expires_at",
            "auth_sessions",
            ["expires_at"],
            unique=False,
        )
    if "security_audit_events" not in tables:
        op.create_table(
            "security_audit_events",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("actor_user_id", sa.String(length=64), nullable=True),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("outcome", sa.String(length=24), nullable=False),
            sa.Column("request_id", sa.String(length=128), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["actor_user_id"],
                ["campus_users.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_security_audit_events_actor_user_id",
            "security_audit_events",
            ["actor_user_id"],
            unique=False,
        )
        op.create_index(
            "ix_security_audit_event_time",
            "security_audit_events",
            ["occurred_at"],
            unique=False,
        )

    conversation_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("conversations")
    }
    if "owner_user_id" not in conversation_columns:
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.add_column(sa.Column("owner_user_id", sa.String(length=64), nullable=True))
            batch_op.create_foreign_key(
                "fk_conversations_owner_user_id_campus_users",
                "campus_users",
                ["owner_user_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_index(
                "ix_conversations_owner_user_id",
                ["owner_user_id"],
                unique=False,
            )

    task_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_tasks")
    }
    if "access_scopes" not in task_columns:
        with op.batch_alter_table("agent_tasks") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "access_scopes",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[\"public\"]'"),
                )
            )
        with op.batch_alter_table("agent_tasks") as batch_op:
            batch_op.alter_column("access_scopes", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("agent_tasks") as batch_op:
        batch_op.drop_column("access_scopes")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_index("ix_conversations_owner_user_id")
        batch_op.drop_constraint(
            "fk_conversations_owner_user_id_campus_users",
            type_="foreignkey",
        )
        batch_op.drop_column("owner_user_id")

    op.drop_index("ix_security_audit_event_time", table_name="security_audit_events")
    op.drop_index(
        "ix_security_audit_events_actor_user_id",
        table_name="security_audit_events",
    )
    op.drop_table("security_audit_events")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("campus_users")
