"""Add configurable task network admission (disabled until explicitly configured)."""

import sqlalchemy as sa

from alembic import op

revision = "0015_agent_network_policy"
down_revision = "0014_announcements"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column(
            "network_restriction_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("network_allowed_cidrs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("network_admin_bypass", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "network_contributor_bypass", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "network_denied_message",
            sa.String(300),
            nullable=False,
            server_default="当前网络暂未开放 Agent 提问，请切换至已开放的网络后重试。",
        ),
    ):
        op.add_column("agent_runtime_policies", column)


def downgrade():
    with op.batch_alter_table("agent_runtime_policies") as batch:
        for name in (
            "network_restriction_enabled",
            "network_allowed_cidrs",
            "network_admin_bypass",
            "network_contributor_bypass",
            "network_denied_message",
        ):
            batch.drop_column(name)
