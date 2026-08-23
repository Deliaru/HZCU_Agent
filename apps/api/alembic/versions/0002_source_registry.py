"""Add source registry, immutable resources, document versions and sync runs.

Revision ID: 0002_source_registry
Revises: 0001_initial
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_source_registry"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_definitions",
        sa.Column("id", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("owner_department", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("allowed_hosts", sa.JSON(), nullable=False),
        sa.Column("visibility", sa.String(length=32), nullable=False),
        sa.Column("authority_level", sa.String(length=32), nullable=False),
        sa.Column("acquisition_methods", sa.JSON(), nullable=False),
        sa.Column("connector_kind", sa.String(length=64), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("default_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("live_required_for", sa.JSON(), nullable=False),
        sa.Column("parser_profile", sa.String(length=80), nullable=False),
        sa.Column("snapshot_policy", sa.String(length=32), nullable=False),
        sa.Column("config_payload", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "source_resources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("canonical_uri", sa.Text(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("resource_type", sa.String(length=48), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_version_id", sa.String(length=64), nullable=True),
        sa.Column("etag", sa.String(length=500), nullable=True),
        sa.Column("last_modified", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["source_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "canonical_uri", name="uq_source_resource_uri"),
    )
    op.create_index(
        "ix_source_resources_source_id",
        "source_resources",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        "ix_source_resources_current_version_id",
        "source_resources",
        ["current_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_source_resources_source_last_seen",
        "source_resources",
        ["source_id", "last_seen_at"],
        unique=False,
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_snapshot_uri", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("publisher", sa.String(length=200), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parser_version", sa.String(length=80), nullable=False),
        sa.Column("quality_status", sa.String(length=32), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["source_resources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_id", "content_hash", name="uq_document_version_hash"),
    )
    op.create_index(
        "ix_document_versions_resource_id",
        "document_versions",
        ["resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_versions_observed_at",
        "document_versions",
        ["observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_document_versions_published_at",
        "document_versions",
        ["published_at"],
        unique=False,
    )
    with op.batch_alter_table("source_resources") as batch_op:
        batch_op.create_foreign_key(
            "fk_source_resources_current_version_id_document_versions",
            "document_versions",
            ["current_version_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_runs_source_id", "sync_runs", ["source_id"], unique=False)
    op.create_index(
        "ix_sync_runs_source_started",
        "sync_runs",
        ["source_id", "started_at"],
        unique=False,
    )

    with op.batch_alter_table("evidence") as batch_op:
        batch_op.add_column(sa.Column("document_version_id", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_evidence_document_version_id",
            ["document_version_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_evidence_document_version_id_document_versions",
            "document_versions",
            ["document_version_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_constraint(
            "fk_evidence_document_version_id_document_versions",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_evidence_document_version_id")
        batch_op.drop_column("document_version_id")

    op.drop_index("ix_sync_runs_source_started", table_name="sync_runs")
    op.drop_index("ix_sync_runs_source_id", table_name="sync_runs")
    op.drop_table("sync_runs")

    with op.batch_alter_table("source_resources") as batch_op:
        batch_op.drop_constraint(
            "fk_source_resources_current_version_id_document_versions",
            type_="foreignkey",
        )
    op.drop_index("ix_document_versions_published_at", table_name="document_versions")
    op.drop_index("ix_document_versions_observed_at", table_name="document_versions")
    op.drop_index("ix_document_versions_resource_id", table_name="document_versions")
    op.drop_table("document_versions")

    op.drop_index("ix_source_resources_source_last_seen", table_name="source_resources")
    op.drop_index("ix_source_resources_current_version_id", table_name="source_resources")
    op.drop_index("ix_source_resources_source_id", table_name="source_resources")
    op.drop_table("source_resources")
    op.drop_table("source_definitions")
