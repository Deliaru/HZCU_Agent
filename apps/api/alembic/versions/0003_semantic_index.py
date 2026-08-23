"""Add semantic chunks and structured campus entities.

Revision ID: 0003_semantic_index
Revises: 0002_source_registry
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_semantic_index"
down_revision: str | Sequence[str] | None = "0002_source_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_version_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("chunk_kind", sa.String(length=48), nullable=False),
        sa.Column("heading", sa.String(length=500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("embedding_model", sa.String(length=120), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("index_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "ordinal",
            name="uq_document_chunk_ordinal",
        ),
    )
    op.create_index(
        "ix_document_chunks_document_version_id",
        "document_chunks",
        ["document_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_chunks_version_ordinal",
        "document_chunks",
        ["document_version_id", "ordinal"],
        unique=False,
    )

    op.create_table(
        "campus_entities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_version_id", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=48), nullable=False),
        sa.Column("canonical_name", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("department", sa.String(length=200), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audience_scopes", sa.JSON(), nullable=False),
        sa.Column("action_items", sa.JSON(), nullable=False),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.Column("document_number", sa.String(length=160), nullable=True),
        sa.Column("relation_kind", sa.String(length=48), nullable=True),
        sa.Column("related_title", sa.String(length=500), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("extractor_version", sa.String(length=80), nullable=False),
        sa.Column("evidence_spans", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_campus_entities_document_version_id",
        "campus_entities",
        ["document_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_campus_entities_type_deadline",
        "campus_entities",
        ["entity_type", "deadline_at"],
        unique=False,
    )
    op.create_index(
        "ix_campus_entities_version_type",
        "campus_entities",
        ["document_version_id", "entity_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_campus_entities_version_type",
        table_name="campus_entities",
    )
    op.drop_index(
        "ix_campus_entities_type_deadline",
        table_name="campus_entities",
    )
    op.drop_index(
        "ix_campus_entities_document_version_id",
        table_name="campus_entities",
    )
    op.drop_table("campus_entities")

    op.drop_index(
        "ix_document_chunks_version_ordinal",
        table_name="document_chunks",
    )
    op.drop_index(
        "ix_document_chunks_document_version_id",
        table_name="document_chunks",
    )
    op.drop_table("document_chunks")
