"""Add Agent-only admission limits, usage counters and queue metadata.

Revision ID: 0012_agent_admission_policy
Revises: 0011_cascade_answer_task_deletion
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0012_agent_admission_policy"
down_revision: str | Sequence[str] | None = "0011_cascade_answer_task_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "visitor_sessions",
        sa.Column("verified_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_tasks",
        sa.Column("queue_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_tasks",
        sa.Column("model_call_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_agent_tasks_queue_deadline_at",
        "agent_tasks",
        ["queue_deadline_at"],
    )

    op.create_table(
        "agent_runtime_policies",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="observe"),
        sa.Column("subject_window_limit", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("subject_window_seconds", sa.Integer(), nullable=False, server_default="1800"),
        sa.Column("subject_daily_limit", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("max_running_per_subject", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_queued_per_subject", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("global_queue_limit", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("queue_timeout_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("agent_concurrency", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("model_concurrency", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("global_daily_task_limit", sa.Integer(), nullable=False, server_default="300"),
        sa.Column(
            "global_daily_model_call_limit",
            sa.Integer(),
            nullable=False,
            server_default="1500",
        ),
        sa.Column("per_task_model_call_limit", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("max_message_length", sa.Integer(), nullable=False, server_default="1500"),
        sa.Column("scope_policy", sa.String(length=24), nullable=False, server_default="balanced"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("turnstile_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("turnstile_site_key", sa.String(length=256), nullable=True),
        sa.Column("encrypted_turnstile_secret", sa.Text(), nullable=True),
        sa.Column("turnstile_secret_hint", sa.String(length=16), nullable=True),
        sa.Column("verification_lease_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("ip_new_subjects_per_hour", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("updated_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["campus_users.id"],
            name="fk_agent_runtime_policies_updated_by_user_id_campus_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "agent_admission_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subject_key", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("request_kind", sa.String(length=32), nullable=False, server_default="normal"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_agent_admission_event_task"),
    )
    op.create_index(
        "ix_agent_admission_events_subject_key",
        "agent_admission_events",
        ["subject_key"],
    )
    op.create_index(
        "ix_agent_admission_events_task_id",
        "agent_admission_events",
        ["task_id"],
    )
    op.create_index(
        "ix_agent_admission_subject_time",
        "agent_admission_events",
        ["subject_key", "occurred_at"],
    )

    op.create_table(
        "agent_usage_counters",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("scope_key", sa.String(length=96), nullable=False),
        sa.Column("bucket_date", sa.String(length=16), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key",
            "bucket_date",
            name="uq_agent_usage_scope_bucket",
        ),
    )
    op.create_index("ix_agent_usage_counters_scope_key", "agent_usage_counters", ["scope_key"])
    op.create_index("ix_agent_usage_counters_bucket_date", "agent_usage_counters", ["bucket_date"])
    op.create_index("ix_agent_usage_bucket", "agent_usage_counters", ["bucket_date"])

    op.create_table(
        "agent_verification_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("ip_hmac", sa.String(length=64), nullable=False),
        sa.Column("subject_key", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_verification_events_ip_hmac", "agent_verification_events", ["ip_hmac"])
    op.create_index(
        "ix_agent_verification_events_subject_key",
        "agent_verification_events",
        ["subject_key"],
    )
    op.create_index(
        "ix_agent_verification_ip_time",
        "agent_verification_events",
        ["ip_hmac", "occurred_at"],
    )
    op.create_index(
        "ix_agent_verification_subject_time",
        "agent_verification_events",
        ["subject_key", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_verification_subject_time", table_name="agent_verification_events")
    op.drop_index("ix_agent_verification_ip_time", table_name="agent_verification_events")
    op.drop_index("ix_agent_verification_events_subject_key", table_name="agent_verification_events")
    op.drop_index("ix_agent_verification_events_ip_hmac", table_name="agent_verification_events")
    op.drop_table("agent_verification_events")

    op.drop_index("ix_agent_usage_bucket", table_name="agent_usage_counters")
    op.drop_index("ix_agent_usage_counters_bucket_date", table_name="agent_usage_counters")
    op.drop_index("ix_agent_usage_counters_scope_key", table_name="agent_usage_counters")
    op.drop_table("agent_usage_counters")

    op.drop_index("ix_agent_admission_subject_time", table_name="agent_admission_events")
    op.drop_index("ix_agent_admission_events_task_id", table_name="agent_admission_events")
    op.drop_index("ix_agent_admission_events_subject_key", table_name="agent_admission_events")
    op.drop_table("agent_admission_events")
    op.drop_table("agent_runtime_policies")

    op.drop_index("ix_agent_tasks_queue_deadline_at", table_name="agent_tasks")
    with op.batch_alter_table("agent_tasks") as batch_op:
        batch_op.drop_column("model_call_count")
        batch_op.drop_column("queue_deadline_at")
    with op.batch_alter_table("visitor_sessions") as batch_op:
        batch_op.drop_column("verified_until")
