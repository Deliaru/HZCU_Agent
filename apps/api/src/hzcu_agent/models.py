from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hzcu_agent.db import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(UTC)


class CampusUser(Base):
    __tablename__ = "campus_users"
    __table_args__ = (
        UniqueConstraint(
            "identity_provider",
            "subject_hash",
            name="uq_campus_user_provider_subject",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_provider: Mapped[str] = mapped_column(String(48), default="hzcu_cas")
    subject_hash: Mapped[str] = mapped_column(String(64))
    subject_hint: Mapped[str | None] = mapped_column(String(24), nullable=True)
    access_scopes: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["public", "campus"])
    role: Mapped[str] = mapped_column(String(24), default="student")
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("campus_users.id", ondelete="CASCADE"),
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LocalAdminCredential(Base):
    """Single server administrator credential; passwords are never persisted."""

    __tablename__ = "local_admin_credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="primary")
    username: Mapped[str] = mapped_column(String(160), unique=True)
    subject_hint: Mapped[str] = mapped_column(String(24))
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_events"
    __table_args__ = (Index("ix_security_audit_event_time", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("campus_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RuntimeModelConfiguration(Base):
    """Singleton server-wide model endpoint selected from the admin console."""

    __tablename__ = "runtime_model_configurations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="primary")
    protocol: Mapped[str] = mapped_column(String(40))
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    api_key_hint: Mapped[str] = mapped_column(String(16))
    agent_model: Mapped[str] = mapped_column(String(160))
    utility_model: Mapped[str] = mapped_column(String(160))
    reasoning_effort: Mapped[str] = mapped_column(String(16), default="medium")
    utility_reasoning_effort: Mapped[str] = mapped_column(String(16), default="low")
    timeout_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    updated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("campus_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProductSubject(Base):
    __tablename__ = "product_subjects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(24), index=True)
    campus_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("campus_users.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    merged_into_subject_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VisitorSession(Base):
    __tablename__ = "visitor_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="CASCADE"),
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("campus_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_subject_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    profile_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["AgentTask"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_message_conversation_client_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    client_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    user_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    answer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    access_scopes: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["public"])
    request_mode: Mapped[str] = mapped_column(String(32), default="normal")
    parent_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    requested_by_subject_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    conversation: Mapped[Conversation] = relationship(back_populates="tasks")


class AnswerRecord(Base):
    __tablename__ = "answers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), unique=True, index=True)
    headline: Mapped[str] = mapped_column(String(240))
    answer_markdown: Mapped[str] = mapped_column(Text)
    assumptions: Mapped[list[str]] = mapped_column(JSON, default=list)
    next_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String(24), default="medium")
    verification_mode: Mapped[str] = mapped_column(String(32), default="unknown")
    model_provider: Mapped[str] = mapped_column(String(40))
    model_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    subject_id: Mapped[str] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    personalization_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProfileAttribute(Base):
    __tablename__ = "profile_attributes"
    __table_args__ = (Index("ix_profile_attributes_subject_status", "subject_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="CASCADE"),
        index=True,
    )
    attribute_key: Mapped[str] = mapped_column(String(48))
    attribute_value: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(24), default="suggested")
    source_kind: Mapped[str] = mapped_column(String(32), default="user")
    supporting_user_text: Mapped[str] = mapped_column(Text, default="")
    source_answer_id: Mapped[str | None] = mapped_column(
        ForeignKey("answers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class UserTodo(Base):
    __tablename__ = "user_todos"
    __table_args__ = (Index("ix_user_todos_subject_status", "subject_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(240))
    notes: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="open")
    source_answer_id: Mapped[str | None] = mapped_column(
        ForeignKey("answers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_action_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AnswerFeedback(Base):
    __tablename__ = "answer_feedback"
    __table_args__ = (
        UniqueConstraint("subject_id", "answer_id", name="uq_feedback_subject_answer"),
        Index("ix_answer_feedback_rating_created", "rating", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("product_subjects.id", ondelete="CASCADE"),
        index=True,
    )
    answer_id: Mapped[str] = mapped_column(
        ForeignKey("answers.id", ondelete="CASCADE"),
        index=True,
    )
    rating: Mapped[str] = mapped_column(String(24))
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class EvidenceRecord(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    publisher: Mapped[str] = mapped_column(String(200))
    canonical_url: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fresh_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_id: Mapped[str] = mapped_column(String(120))
    resource_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Keep the provenance fields that are available on the in-memory Evidence
    # contract when an answer is reloaded after a browser refresh.  Earlier
    # Stage 6 rows predated these fields and are backfilled with conservative
    # defaults by migration 0008.
    authority_level: Mapped[str] = mapped_column(String(32), default="unknown")
    audience_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    retrieval_mode: Mapped[str] = mapped_column(String(32), default="unknown")
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "document_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_evidence_document_version_id_document_versions",
        ),
        nullable=True,
        index=True,
    )


class AnswerGroundingRecord(Base):
    __tablename__ = "answer_grounding"

    answer_id: Mapped[str] = mapped_column(
        ForeignKey("answers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    verifier_verdict: Mapped[str] = mapped_column(String(32))
    verifier_summary: Mapped[str] = mapped_column(Text)
    citation_coverage: Mapped[float] = mapped_column(Float, default=0.0)
    fully_supported_rate: Mapped[float] = mapped_column(Float, default=0.0)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AnswerClaimRecord(Base):
    __tablename__ = "answer_claims"
    __table_args__ = (
        UniqueConstraint("answer_id", "claim_key", name="uq_answer_claim_key"),
        Index("ix_answer_claims_answer_ordinal", "answer_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    answer_id: Mapped[str] = mapped_column(
        ForeignKey("answers.id", ondelete="CASCADE"),
        index=True,
    )
    claim_key: Mapped[str] = mapped_column(String(120))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    statement_type: Mapped[str] = mapped_column(String(32))
    importance: Mapped[str] = mapped_column(String(24))
    scope: Mapped[str] = mapped_column(Text, default="")
    valid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    support_status: Mapped[str] = mapped_column(String(32))
    uncertainty: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClaimEvidenceRecord(Base):
    __tablename__ = "claim_evidence"
    __table_args__ = (
        UniqueConstraint(
            "claim_id",
            "evidence_id",
            name="uq_claim_evidence_pair",
        ),
        Index("ix_claim_evidence_claim_id", "claim_id"),
        Index("ix_claim_evidence_evidence_id", "evidence_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_id: Mapped[str] = mapped_column(
        ForeignKey("answer_claims.id", ondelete="CASCADE"),
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"),
    )
    relation: Mapped[str] = mapped_column(String(24))
    support_status: Mapped[str] = mapped_column(String(32))
    rationale: Mapped[str] = mapped_column(Text, default="")
    supporting_excerpt: Mapped[str] = mapped_column(Text, default="")


class TaskPerformanceRecord(Base):
    __tablename__ = "task_performance"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    scenario: Mapped[str] = mapped_column(String(40))
    total_duration_ms: Mapped[float] = mapped_column(Float)
    excluded_model_ttft_ms: Mapped[float] = mapped_column(Float)
    controllable_duration_ms: Mapped[float] = mapped_column(Float)
    first_progress_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_call_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    model_ttft_measurable: Mapped[bool] = mapped_column(Boolean, default=True)
    spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourceDefinitionRecord(Base):
    __tablename__ = "source_definitions"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_department: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(Text)
    allowed_hosts: Mapped[list[str]] = mapped_column(JSON, default=list)
    visibility: Mapped[str] = mapped_column(String(32), default="public")
    authority_level: Mapped[str] = mapped_column(String(32), default="official")
    acquisition_methods: Mapped[list[str]] = mapped_column(JSON, default=list)
    connector_kind: Mapped[str] = mapped_column(String(64))
    poll_interval_seconds: Mapped[int] = mapped_column(Integer)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer)
    default_ttl_seconds: Mapped[int] = mapped_column(Integer)
    live_required_for: Mapped[list[str]] = mapped_column(JSON, default=list)
    parser_profile: Mapped[str] = mapped_column(String(80))
    snapshot_policy: Mapped[str] = mapped_column(String(32), default="raw")
    config_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SourceResource(Base):
    __tablename__ = "source_resources"
    __table_args__ = (
        UniqueConstraint("source_id", "canonical_uri", name="uq_source_resource_uri"),
        Index("ix_source_resources_source_last_seen", "source_id", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("source_definitions.id", ondelete="CASCADE"), index=True
    )
    canonical_uri: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(48))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "document_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_source_resources_current_version_id_document_versions",
        ),
        nullable=True,
        index=True,
    )
    etag: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(200), nullable=True)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("resource_id", "content_hash", name="uq_document_version_hash"),
        Index("ix_document_versions_observed_at", "observed_at"),
        Index("ix_document_versions_published_at", "published_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("source_resources.id", ondelete="CASCADE"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    raw_snapshot_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(160))
    normalized_text: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500))
    publisher: Mapped[str] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    parser_version: Mapped[str] = mapped_column(String(80))
    quality_status: Mapped[str] = mapped_column(String(32), default="accepted")
    document_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "ordinal",
            name="uq_document_chunk_ordinal",
        ),
        Index(
            "ix_document_chunks_version_ordinal",
            "document_version_id",
            "ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    chunk_kind: Mapped[str] = mapped_column(String(48), default="section")
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str] = mapped_column(String(120))
    embedding_dimensions: Mapped[int] = mapped_column(Integer)
    index_version: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CampusEntityRecord(Base):
    __tablename__ = "campus_entities"
    __table_args__ = (
        Index(
            "ix_campus_entities_type_deadline",
            "entity_type",
            "deadline_at",
        ),
        Index(
            "ix_campus_entities_version_type",
            "document_version_id",
            "entity_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(48))
    canonical_name: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    department: Mapped[str | None] = mapped_column(String(200), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    audience_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    action_items: Mapped[list[str]] = mapped_column(JSON, default=list)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    document_number: Mapped[str | None] = mapped_column(String(160), nullable=True)
    relation_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    related_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    extractor_version: Mapped[str] = mapped_column(String(80))
    evidence_spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_source_started", "source_id", "started_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("source_definitions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
