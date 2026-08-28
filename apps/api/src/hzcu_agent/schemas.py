from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    profile_context: dict[str, Any] = Field(default_factory=dict)


class ConversationResponse(BaseModel):
    conversation_id: str
    created_at: datetime


class ConversationPatchRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    last_message: str | None = None
    last_task_status: str | None = None


class ConversationListResponse(BaseModel):
    items: list[ConversationSummaryResponse]
    next_cursor: str | None = None


class ConversationMessageResponse(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    client_message_id: str | None = None


class ConversationTaskSummary(BaseModel):
    task_id: str
    user_message_id: str
    status: str
    error_code: str | None = None
    answer_id: str | None = None
    request_mode: str = "normal"
    parent_task_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ConversationDetailResponse(BaseModel):
    conversation_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageResponse]
    tasks: list[ConversationTaskSummary]


class AuthSessionResponse(BaseModel):
    authenticated: bool
    auth_mode: Literal["anonymous", "optional_cas", "required_cas"]
    cas_enabled: bool
    subject_hint: str | None = None
    visibility_scopes: list[Literal["public", "campus", "restricted"]]
    mirror_visibility_scopes: list[Literal["public", "campus", "restricted"]]
    login_url: str | None = None
    service_registration_required: bool = False
    query_access: Literal["direct", "vpn", "unavailable"] = "unavailable"
    query_access_expires_at: datetime | None = None
    credential_handoff_available: bool = False
    read_only_capability: Literal["campus_notice.read", "community.answer"] = "campus_notice.read"
    subject_kind: Literal["visitor", "campus", "local_admin", "contributor"] = "visitor"
    role: Literal["visitor", "student", "admin", "contributor"] = "visitor"
    visitor_data_available: bool = False
    local_admin_enabled: bool = False
    local_admin_configured: bool = False
    local_admin_setup_available: bool = False


class LocalAdminChallengeResponse(BaseModel):
    challenge: str
    expires_in_seconds: int


class ContributorLoginChallengeResponse(BaseModel):
    challenge: str
    expires_in_seconds: int


class LocalAdminCredentialRequest(BaseModel):
    username: str = Field(min_length=1, max_length=160, pattern=r"^[^\s\x00-\x1f]+$")
    password: SecretStr
    challenge: str = Field(min_length=16, max_length=256)


class CredentialLoginChallengeResponse(BaseModel):
    challenge: str
    expires_in_seconds: int
    capability: Literal["campus_notice.read"] = "campus_notice.read"


class CredentialLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=160, pattern=r"^[^\s\x00-\x1f]+$")
    password: SecretStr
    challenge: str = Field(min_length=16, max_length=256)


class SendMessageRequest(BaseModel):
    # The runtime Agent policy applies the deployment-specific (1500 by
    # default) limit; keep a higher schema ceiling so an administrator can
    # intentionally raise that limit without being silently capped here.
    message: str = Field(min_length=1, max_length=20_000)
    client_message_id: str | None = Field(default=None, max_length=120)
    profile_overrides: dict[str, Any] = Field(default_factory=dict)
    response_style: Literal["neutral", "congyu"] = "neutral"


class AgentVerificationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class AgentVerificationResponse(BaseModel):
    verified_until: datetime


class AgentAccessResponse(BaseModel):
    mode: Literal["observe", "enforce", "paused"]
    turnstile_enabled: bool
    turnstile_site_key: str | None = None
    verification_required: bool = False
    window_remaining: int | None = None
    daily_remaining: int | None = None
    window_reset_at: datetime | None = None
    daily_reset_at: datetime | None = None
    running: int = 0
    queued: int = 0


class AcceptedTaskResponse(BaseModel):
    task_id: str
    stream_url: str
    queue_position: int = 0


class GoalHypothesis(BaseModel):
    goal: str
    confidence: float = Field(ge=0, le=1)
    support: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)


class EmotionalContext(BaseModel):
    label: str = "neutral"
    confidence: float = Field(default=0.5, ge=0, le=1)


class RiskAssessment(BaseModel):
    level: Literal["low", "normal", "academic_high", "sensitive"] = "normal"
    reason: str = ""


class SemanticSignals(BaseModel):
    """Model-produced classifier signals; they guide investigation but never select an answer."""

    domains: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)
    freshness: Literal["stable", "current", "live_required"] = "current"
    task_shape: Literal["simple", "compound", "contextual"] = "simple"
    domain_scope: Literal["in_scope", "ambiguous", "out_of_scope"] = "ambiguous"
    scope_reason: str = ""
    answer_shape: Literal[
        "fact",
        "enumeration",
        "procedure",
        "comparison",
        "timeline",
        "mixed",
    ] = "fact"


class SemanticEntityGroup(BaseModel):
    kind: str
    values: list[str] = Field(default_factory=list)


class SemanticDossier(BaseModel):
    goal_hypotheses: list[GoalHypothesis] = Field(min_length=1)
    signals: SemanticSignals = Field(default_factory=SemanticSignals)
    latent_needs: list[str] = Field(default_factory=list)
    entities: list[SemanticEntityGroup] = Field(default_factory=list)
    time_references: list[str] = Field(default_factory=list)
    emotional_context: EmotionalContext = Field(default_factory=EmotionalContext)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    risk: RiskAssessment = Field(default_factory=RiskAssessment)
    needed_context: list[str] = Field(default_factory=list)
    candidate_evidence_types: list[str] = Field(default_factory=list)


class InvestigationFilters(BaseModel):
    student_type: Literal["undergraduate", "graduate", "other"] | None = None
    cohort: str | None = None
    college: str | None = None
    major: str | None = None
    entity_types: list[
        Literal["document", "notice", "policy", "course", "competition", "event"]
    ] = Field(default_factory=list)
    valid_at: datetime | None = None
    visibility: list[Literal["public", "campus", "restricted"]] = Field(default_factory=list)


class InvestigationArguments(BaseModel):
    """Typed superset of the registered read-only tool arguments."""

    query: str | None = None
    queries: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    filters: InvestigationFilters = Field(default_factory=InvestigationFilters)
    top_k: int | None = None
    limit: int | None = None
    document_version_id: str | None = None
    locator: int | None = None
    offset: int | None = None
    max_chars: int | None = None
    context_chars: int | None = None

    def tool_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="python",
            exclude_none=True,
            exclude_defaults=True,
        )


class InvestigationStep(BaseModel):
    id: str
    purpose: str
    tool: str
    arguments: InvestigationArguments
    depends_on: list[str] = Field(default_factory=list)
    can_run_in_parallel: bool = False
    success_condition: str


class InvestigationPlan(BaseModel):
    objective: str
    hypotheses_to_test: list[str] = Field(default_factory=list)
    steps: list[InvestigationStep] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list)


class PreparedInvestigation(BaseModel):
    """One model turn that preserves semantic hypotheses and produces a tool plan."""

    dossier: SemanticDossier
    plan: InvestigationPlan


class InvestigationReview(BaseModel):
    status: Literal[
        "sufficient",
        "insufficient",
        "stale",
        "conflicting",
        "unauthorized",
        "irrelevant",
    ]
    can_answer: bool
    summary: str
    missing_evidence: list[str] = Field(default_factory=list)
    follow_up_steps: list[InvestigationStep] = Field(default_factory=list)


class Evidence(BaseModel):
    evidence_id: str
    title: str
    publisher: str
    canonical_url: str
    published_at: datetime | None = None
    observed_at: datetime
    fresh_until: datetime | None = None
    excerpt: str
    source_id: str
    resource_ref: str | None = None
    document_version_id: str | None = None
    authority_level: Literal["official", "official_secondary", "curated", "unknown"] = "unknown"
    audience_scopes: list[str] = Field(default_factory=list)
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    retrieval_mode: Literal[
        "memory",
        "live_public",
        "live_authenticated",
        "unknown",
    ] = "unknown"


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool: str
    version: str = "1.0.0"
    status: Literal["ok", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: ToolError | None = None
    trace_id: str


class AgentAnswer(BaseModel):
    headline: str
    answer_markdown: str
    assumptions: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"
    verification_mode: Literal[
        "live_verified", "cache", "historical", "degraded", "no_campus_evidence"
    ] = "no_campus_evidence"
    claims: list["AnswerClaim"] = Field(default_factory=list)
    profile_suggestions: list["ProfileSuggestionDraft"] = Field(default_factory=list)


class ProfileSuggestionDraft(BaseModel):
    attribute_key: Literal[
        "education_level",
        "cohort",
        "college",
        "major",
        "goal",
        "interest",
    ]
    attribute_value: str = Field(min_length=1, max_length=240)
    supporting_user_text: str = Field(min_length=1, max_length=500)


class ClaimCitation(BaseModel):
    evidence_id: str
    relation: Literal["supports", "contradicts", "context"] = "supports"
    support_status: Literal[
        "full",
        "partial",
        "unsupported",
        "contradicted",
        "stale",
        "out_of_scope",
    ] = "full"
    rationale: str = Field(default="", max_length=160)
    supporting_excerpt: str = Field(default="", max_length=200)


class AnswerClaim(BaseModel):
    claim_id: str
    text: str = Field(min_length=1)
    statement_type: Literal["campus_fact", "analysis", "advice"]
    importance: Literal["key", "supporting"] = "supporting"
    scope: str = ""
    valid_at: datetime | None = None
    support_status: Literal[
        "full",
        "partial",
        "unsupported",
        "contradicted",
        "stale",
        "not_required",
    ] = "not_required"
    citations: list[ClaimCitation] = Field(default_factory=list)
    uncertainty: str = ""


class GroundingAssessment(BaseModel):
    status: Literal[
        "sufficient",
        "conditional",
        "insufficient",
        "stale",
        "conflicting",
        "unauthorized",
    ]
    summary: str
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    needs_more_research: bool = False
    follow_up_steps: list[InvestigationStep] = Field(default_factory=list)


class GroundedAnswerComposition(BaseModel):
    """Claim-first answer plus the evidence sufficiency decision from the same turn."""

    answer: AgentAnswer | None = None
    assessment: GroundingAssessment
    requires_independent_verification: bool = False
    verification_reason: str = ""


class VerificationFinding(BaseModel):
    claim_id: str | None = None
    severity: Literal["info", "warning", "error"]
    code: Literal[
        "orphan_fact",
        "missing_citation",
        "unsupported",
        "partial_support",
        "contradicted",
        "stale",
        "scope_mismatch",
        "invalid_evidence_id",
        "invalid_url",
    ]
    message: str


class ClaimPatch(BaseModel):
    """One claim-level edit; unchanged claims are never re-emitted."""

    action: Literal["remove", "replace", "add"]
    claim_id: str
    claim: AnswerClaim | None = None


class AnswerRevision(BaseModel):
    """Structured diff over the candidate answer. Only changed fields appear."""

    headline: str | None = None
    answer_markdown: str | None = None
    assumptions: list[str] | None = None
    next_actions: list[str] | None = None
    confidence: Literal["low", "medium", "high"] | None = None
    verification_mode: (
        Literal["live_verified", "cache", "historical", "degraded", "no_campus_evidence"] | None
    ) = None
    claim_patches: list[ClaimPatch] = Field(default_factory=list)


class AnswerVerification(BaseModel):
    verdict: Literal["passed", "revised", "research_required"]
    summary: str
    revision: AnswerRevision | None = None
    findings: list[VerificationFinding] = Field(default_factory=list)


class GroundingSummary(BaseModel):
    status: str
    summary: str
    verifier_verdict: str
    verifier_summary: str
    citation_coverage: float = Field(ge=0, le=1)
    fully_supported_rate: float = Field(ge=0, le=1)
    findings: list[VerificationFinding] = Field(default_factory=list)


class AgentPerformance(BaseModel):
    scenario: Literal[
        "no_live_read",
        "public_live",
        "campus_authenticated",
        "multi_source_or_image",
    ]
    total_duration_ms: float = Field(ge=0)
    excluded_model_ttft_ms: float = Field(ge=0)
    controllable_duration_ms: float = Field(ge=0)
    first_progress_ms: float | None = Field(default=None, ge=0)
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    model_ttft_measurable: bool = True
    spans: list[dict[str, Any]] = Field(default_factory=list)


class TaskResponse(BaseModel):
    task_id: str
    status: str
    answer_id: str | None = None
    error_code: str | None = None
    queue_position: int = 0


class AnswerResponse(BaseModel):
    answer_id: str
    task_id: str
    headline: str
    answer_markdown: str
    assumptions: list[str]
    next_actions: list[str]
    confidence: str
    verification_mode: str
    response_style: Literal["neutral", "congyu"] = "neutral"
    evidence: list[Evidence]
    claims: list[AnswerClaim] = Field(default_factory=list)
    grounding: GroundingSummary | None = None
    performance: AgentPerformance | None = None
    profile_suggestions: list["ProfileAttributeResponse"] = Field(default_factory=list)
    question_offer: "QuestionOfferResponse | None" = None
    created_at: datetime


class QuestionOfferResponse(BaseModel):
    reason: Literal[
        "no_evidence",
        "low_confidence",
        "grounding_insufficient",
        "grounding_stale",
        "grounding_conflicting",
        "verification_degraded",
    ]
    title: str
    details: str
    evidence_gap: str
    existing_question_id: str | None = None
    existing_status: str | None = None


class QuestionCreateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=240)
    details: str = Field(min_length=2, max_length=6000)


class QuestionSummaryResponse(BaseModel):
    question_id: str
    title: str
    details: str
    status: Literal["pending_review", "open", "answered", "rejected", "hidden"]
    answer_count: int = 0
    waiting_seconds: int = 0
    created_at: datetime
    updated_at: datetime


class CommunityAnswerResponse(BaseModel):
    answer_id: str
    question_id: str
    answer_markdown: str
    display_name: str
    unit: str | None = None
    status: Literal["visible", "hidden"]
    knowledge_review_state: str
    can_edit: bool = False
    created_at: datetime
    updated_at: datetime


class QuestionDetailResponse(QuestionSummaryResponse):
    evidence_gap: str = ""
    answers: list[CommunityAnswerResponse] = Field(default_factory=list)


class QuestionReviewRequest(BaseModel):
    status: Literal["open", "rejected", "hidden"]
    review_note: str | None = Field(default=None, max_length=1000)


class CommunityAnswerCreateRequest(BaseModel):
    answer_markdown: str = Field(min_length=2, max_length=12000)


class CommunityAnswerUpdateRequest(BaseModel):
    answer_markdown: str = Field(min_length=2, max_length=12000)


class CommunityAnswerModerationRequest(BaseModel):
    status: Literal["visible", "hidden"]


class ContributorCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=160, pattern=r"^[^\s\x00-\x1f]+$")
    password: SecretStr = Field(min_length=6, max_length=256)
    public_name: str = Field(min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=200)


class ContributorUpdateRequest(BaseModel):
    public_name: str | None = Field(default=None, min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=200)
    status: Literal["active", "disabled"] | None = None
    password: SecretStr | None = Field(default=None, min_length=6, max_length=256)


class ContributorResponse(BaseModel):
    contributor_id: str
    username: str
    public_name: str
    unit: str | None
    status: Literal["active", "disabled"]
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None


class KnowledgeEntryRequest(BaseModel):
    question_id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=2, max_length=240)
    canonical_question: str = Field(min_length=2, max_length=2000)
    answer_markdown: str = Field(min_length=2, max_length=20000)
    category: str = Field(default="校园综合", min_length=1, max_length=120)
    alternative_phrasings: list[str] = Field(default_factory=list, max_length=12)
    applicable_scope: str = Field(default="", max_length=2000)
    maintainer_unit: str = Field(default="", max_length=200)
    basis_note: str = Field(default="", max_length=4000)
    validity: Literal["stable", "time_bounded"] = "stable"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    visibility: Literal["public", "campus"] = "public"
    origin_answer_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("alternative_phrasings")
    @classmethod
    def normalize_alternatives(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip()[:240] for value in values if value.strip()))

    @model_validator(mode="after")
    def validate_validity_window(self) -> "KnowledgeEntryRequest":
        if self.validity == "time_bounded" and (
            self.effective_from is None or self.effective_to is None
        ):
            raise ValueError("time_bounded 条目必须填写生效和失效日期")
        if self.effective_from and self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("失效日期必须晚于生效日期")
        return self


class KnowledgeEntryResponse(KnowledgeEntryRequest):
    entry_id: str
    question_id: str | None = None
    status: Literal["draft", "published", "retired"]
    published_source_id: str | None = None
    published_resource_id: str | None = None
    published_version_id: str | None = None
    content_hash: str | None = None
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None = None


class KnowledgeOptimizationResponse(BaseModel):
    entry_id: str
    suggested_title: str
    suggested_category: str
    suggested_phrasings: list[str]
    scope_risk: str


ProfileAttributeKey = Literal[
    "education_level",
    "cohort",
    "college",
    "major",
    "goal",
    "interest",
]


class ProfileAttributeInput(BaseModel):
    attribute_key: ProfileAttributeKey
    attribute_value: str = Field(min_length=1, max_length=240)


class ProfilePatchRequest(BaseModel):
    personalization_enabled: bool | None = None
    onboarding_completed: bool | None = None
    attributes: list[ProfileAttributeInput] = Field(default_factory=list, max_length=6)


class ProfileAttributeResponse(BaseModel):
    attribute_id: str
    attribute_key: ProfileAttributeKey
    attribute_value: str
    status: Literal["confirmed", "suggested", "rejected"]
    source_kind: str
    supporting_user_text: str
    source_answer_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ProfileResponse(BaseModel):
    personalization_enabled: bool
    onboarding_completed: bool
    confirmed: list[ProfileAttributeResponse]
    suggestions: list[ProfileAttributeResponse]


class TodoCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=4000)
    due_at: datetime | None = None
    source_answer_id: str | None = Field(default=None, max_length=64)
    source_action_index: int | None = Field(default=None, ge=0, le=100)


class TodoPatchRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    notes: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    status: Literal["open", "done", "archived"] | None = None


class TodoResponse(BaseModel):
    todo_id: str
    title: str
    notes: str
    due_at: datetime | None
    status: Literal["open", "done", "archived"]
    source_answer_id: str | None
    source_action_index: int | None
    created_at: datetime
    updated_at: datetime


class FeedbackRequest(BaseModel):
    rating: Literal["helpful", "not_helpful", "incorrect", "outdated"]
    categories: list[str] = Field(default_factory=list, max_length=8)
    comment: str = Field(default="", max_length=2000)


class FeedbackCreateRequest(FeedbackRequest):
    answer_id: str = Field(min_length=1, max_length=64)


class FeedbackResponse(BaseModel):
    feedback_id: str
    answer_id: str
    rating: Literal["helpful", "not_helpful", "incorrect", "outdated"]
    categories: list[str]
    comment: str
    created_at: datetime
    updated_at: datetime


class IdentityMergeResponse(BaseModel):
    merged: bool
    conversations_moved: int
    todos_moved: int
    feedback_moved: int
    suggestions_created: int


class AdminOverviewResponse(BaseModel):
    task_count: int
    completed_count: int
    failed_count: int
    success_rate: float
    median_duration_ms: float | None = None
    p95_duration_ms: float | None = None
    feedback_count: int
    source_alert_count: int


class AdminModelConfigurationResponse(BaseModel):
    protocol: Literal["demo", "openai_responses", "anthropic_messages"]
    base_url: str | None = None
    agent_model: str
    utility_model: str
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    utility_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    timeout_seconds: float
    api_key_configured: bool
    api_key_hint: str | None = None
    source: Literal["environment", "database"]
    updated_at: datetime | None = None


class AdminModelConfigurationUpdate(BaseModel):
    protocol: Literal["openai_responses", "anthropic_messages"]
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    agent_model: str = Field(min_length=1, max_length=160)
    utility_model: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    utility_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    timeout_seconds: float = Field(ge=10, le=600)

    @field_validator("base_url")
    @classmethod
    def normalize_optional_base_url(cls, value: str | None) -> str | None:
        normalized = value.strip().rstrip("/") if value else None
        return normalized or None

    @field_validator("agent_model", "utility_model")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("model name cannot contain whitespace")
        return normalized


class AdminAgentPolicyResponse(BaseModel):
    mode: Literal["observe", "enforce", "paused"]
    subject_window_limit: int
    subject_window_seconds: int
    subject_daily_limit: int
    max_running_per_subject: int
    max_queued_per_subject: int
    global_queue_limit: int
    queue_timeout_seconds: int
    agent_concurrency: int
    model_concurrency: int
    global_daily_task_limit: int
    global_daily_model_call_limit: int
    per_task_model_call_limit: int
    max_message_length: int
    scope_policy: Literal["balanced", "strict"]
    timezone: str
    turnstile_enabled: bool
    turnstile_site_key: str | None = None
    turnstile_secret_configured: bool = False
    turnstile_secret_hint: str | None = None
    verification_lease_hours: int
    ip_new_subjects_per_hour: int
    updated_at: datetime | None = None
    today_task_count: int = 0
    today_model_call_count: int = 0
    today_rejection_counts: dict[str, int] = Field(default_factory=dict)
    running_count: int = 0
    queued_count: int = 0
    oldest_queue_wait_seconds: int = 0


class AdminAgentPolicyUpdate(BaseModel):
    mode: Literal["observe", "enforce", "paused"]
    subject_window_limit: int = Field(ge=1, le=100)
    subject_window_seconds: int = Field(ge=1, le=86400)
    subject_daily_limit: int = Field(ge=1, le=1000)
    max_running_per_subject: int = Field(ge=1, le=4)
    max_queued_per_subject: int = Field(ge=0, le=8)
    global_queue_limit: int = Field(ge=1, le=10000)
    queue_timeout_seconds: int = Field(ge=1, le=86400)
    agent_concurrency: int = Field(ge=1, le=64)
    model_concurrency: int = Field(ge=1, le=64)
    global_daily_task_limit: int = Field(ge=1, le=100000)
    global_daily_model_call_limit: int = Field(ge=1, le=1000000)
    per_task_model_call_limit: int = Field(ge=1, le=100)
    max_message_length: int = Field(ge=1, le=20000)
    scope_policy: Literal["balanced", "strict"]
    timezone: str = Field(min_length=1, max_length=64)
    turnstile_enabled: bool
    turnstile_site_key: str | None = Field(default=None, max_length=256)
    turnstile_secret: SecretStr | None = Field(default=None, max_length=4096)
    verification_lease_hours: int = Field(ge=1, le=168)
    ip_new_subjects_per_hour: int = Field(ge=1, le=10000)


class AdminTaskHealthItem(BaseModel):
    task_id: str
    conversation_id: str
    answer_id: str | None
    status: str
    error_code: str | None
    request_mode: str
    model_call_count: int | None
    tool_call_count: int | None
    total_duration_ms: float | None
    created_at: datetime


class AdminTaskHealthResponse(BaseModel):
    items: list[AdminTaskHealthItem]


class AdminConversationTraceResponse(BaseModel):
    matched_trace_id: str
    conversation_id: str
    title: str | None
    subject_kind: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageResponse]
    tasks: list[ConversationTaskSummary]


class SourceStatusResponse(BaseModel):
    source_id: str
    name: str
    owner_department: str
    base_url: str
    allowed_hosts: list[str]
    visibility: str
    authority_level: str
    connector_kind: str
    poll_interval_seconds: int
    default_ttl_seconds: int
    enabled: bool
    resource_count: int
    version_count: int
    last_run_status: str | None = None
    last_run_started_at: datetime | None = None
    last_success_at: datetime | None = None
    latest_version_at: datetime | None = None
    fresh_until: datetime | None = None
    health_state: Literal[
        "healthy",
        "degraded",
        "stale",
        "failing",
        "disabled",
        "waiting",
    ] = "waiting"
    consecutive_failures: int = 0
    chunk_count: int = 0
    entity_count: int = 0


class SourceResourceResponse(BaseModel):
    resource_id: str
    canonical_uri: str
    resource_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    current_version_id: str | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    observed_at: datetime | None = None
    quality_status: str | None = None
    version_count: int = 0
    chunk_count: int = 0
    entity_type: str | None = None
    entity_status: str | None = None
    deadline_at: datetime | None = None
    audience_scopes: list[str] = Field(default_factory=list)


class CampusEntityResponse(BaseModel):
    entity_id: str
    entity_type: str
    canonical_name: str
    status: str
    department: str | None = None
    starts_at: datetime | None = None
    deadline_at: datetime | None = None
    ends_at: datetime | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    audience_scopes: list[str]
    action_items: list[str]
    locations: list[str]
    document_number: str | None = None
    relation_kind: str | None = None
    related_title: str | None = None
    confidence: float
    extractor_version: str


class DocumentVersionSummaryResponse(BaseModel):
    version_id: str
    content_hash: str
    title: str
    publisher: str
    published_at: datetime | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    observed_at: datetime
    parser_version: str
    quality_status: str
    is_current: bool
    chunk_count: int
    entities: list[CampusEntityResponse]


class DocumentVersionDetailResponse(DocumentVersionSummaryResponse):
    resource_id: str
    canonical_uri: str
    media_type: str
    text_excerpt: str
    section_headings: list[str]


class VersionComparisonResponse(BaseModel):
    resource_id: str
    from_version_id: str
    to_version_id: str
    changed: bool
    title_changed: bool
    unified_diff: str
    structured_changes: dict[str, Any]
    truncated: bool


class SourceAlertResponse(BaseModel):
    source_id: str
    source_name: str
    severity: Literal["info", "warning", "critical"]
    code: str
    message: str
    detected_at: datetime
