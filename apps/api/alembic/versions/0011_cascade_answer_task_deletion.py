"""Cascade stored answers when their parent task is deleted.

Revision ID: 0011_cascade_answer_task_deletion
Revises: 0010_local_admin_credentials
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_cascade_answer_task_deletion"
down_revision: str | Sequence[str] | None = "0010_local_admin_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}
_CONSTRAINT_NAME = "fk_answers_task_id_agent_tasks"


def upgrade() -> None:
    with op.batch_alter_table(
        "answers",
        naming_convention=_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="foreignkey")
        batch_op.create_foreign_key(
            _CONSTRAINT_NAME,
            "agent_tasks",
            ["task_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "answers",
        naming_convention=_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="foreignkey")
        batch_op.create_foreign_key(
            _CONSTRAINT_NAME,
            "agent_tasks",
            ["task_id"],
            ["id"],
        )
