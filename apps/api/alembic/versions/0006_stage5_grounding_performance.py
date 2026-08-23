"""Add Stage 5 claim grounding and performance records.

Revision ID: 0006_stage5_grounding_performance
Revises: 0005_repair_identity_foreign_keys
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_stage5_grounding_performance"
down_revision: str | Sequence[str] | None = "0005_repair_identity_foreign_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "answer_grounding",
        sa.Column("answer_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("verifier_verdict", sa.String(length=32), nullable=False),
        sa.Column("verifier_summary", sa.Text(), nullable=False),
        sa.Column("citation_coverage", sa.Float(), nullable=False),
        sa.Column("fully_supported_rate", sa.Float(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("answer_id"),
    )
    op.create_table(
        "answer_claims",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("answer_id", sa.String(length=64), nullable=False),
        sa.Column("claim_key", sa.String(length=120), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("statement_type", sa.String(length=32), nullable=False),
        sa.Column("importance", sa.String(length=24), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("valid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("support_status", sa.String(length=32), nullable=False),
        sa.Column("uncertainty", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("answer_id", "claim_key", name="uq_answer_claim_key"),
    )
    op.create_index(
        "ix_answer_claims_answer_id",
        "answer_claims",
        ["answer_id"],
        unique=False,
    )
    op.create_index(
        "ix_answer_claims_answer_ordinal",
        "answer_claims",
        ["answer_id", "ordinal"],
        unique=False,
    )
    op.create_table(
        "claim_evidence",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("relation", sa.String(length=24), nullable=False),
        sa.Column("support_status", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("supporting_excerpt", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["answer_claims.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "claim_id",
            "evidence_id",
            name="uq_claim_evidence_pair",
        ),
    )
    op.create_index(
        "ix_claim_evidence_claim_id",
        "claim_evidence",
        ["claim_id"],
        unique=False,
    )
    op.create_index(
        "ix_claim_evidence_evidence_id",
        "claim_evidence",
        ["evidence_id"],
        unique=False,
    )
    op.create_table(
        "task_performance",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("scenario", sa.String(length=40), nullable=False),
        sa.Column("total_duration_ms", sa.Float(), nullable=False),
        sa.Column("excluded_model_ttft_ms", sa.Float(), nullable=False),
        sa.Column("controllable_duration_ms", sa.Float(), nullable=False),
        sa.Column("first_progress_ms", sa.Float(), nullable=True),
        sa.Column("model_call_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("model_ttft_measurable", sa.Boolean(), nullable=False),
        sa.Column("spans", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("task_performance")
    op.drop_index("ix_claim_evidence_evidence_id", table_name="claim_evidence")
    op.drop_index("ix_claim_evidence_claim_id", table_name="claim_evidence")
    op.drop_table("claim_evidence")
    op.drop_index("ix_answer_claims_answer_ordinal", table_name="answer_claims")
    op.drop_index("ix_answer_claims_answer_id", table_name="answer_claims")
    op.drop_table("answer_claims")
    op.drop_table("answer_grounding")
