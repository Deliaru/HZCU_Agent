"""Repair optional identity references created before SQLite FK enforcement.

Revision ID: 0005_repair_identity_foreign_keys
Revises: 0004_campus_identity
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_repair_identity_foreign_keys"
down_revision: str | Sequence[str] | None = "0004_campus_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE conversations
            SET owner_user_id = NULL
            WHERE owner_user_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM campus_users
                WHERE campus_users.id = conversations.owner_user_id
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE security_audit_events
            SET actor_user_id = NULL
            WHERE actor_user_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM campus_users
                WHERE campus_users.id = security_audit_events.actor_user_id
              )
            """
        )
    )


def downgrade() -> None:
    # Dangling identity references cannot be reconstructed safely.
    pass
