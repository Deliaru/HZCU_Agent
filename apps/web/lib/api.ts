export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";

export type Evidence = {
  evidence_id: string;
  title: string;
  publisher: string;
  canonical_url: string;
  published_at: string | null;
  observed_at: string;
  fresh_until: string | null;
  excerpt: string;
  source_id: string;
  resource_ref: string | null;
  document_version_id: string | null;
  authority_level: "official" | "official_secondary" | "curated" | "unknown";
  audience_scopes: string[];
  effective_from: string | null;
  effective_to: string | null;
  retrieval_mode:
    | "memory"
    | "live_public"
    | "live_authenticated"
    | "unknown";
};

export type ClaimCitation = {
  evidence_id: string;
  relation: "supports" | "contradicts" | "context";
  support_status:
    | "full"
    | "partial"
    | "unsupported"
    | "contradicted"
    | "stale"
    | "out_of_scope";
  rationale: string;
  supporting_excerpt: string;
};

export type AnswerClaim = {
  claim_id: string;
  text: string;
  statement_type: "campus_fact" | "analysis" | "advice";
  importance: "key" | "supporting";
  scope: string;
  valid_at: string | null;
  support_status:
    | "full"
    | "partial"
    | "unsupported"
    | "contradicted"
    | "stale"
    | "not_required";
  citations: ClaimCitation[];
  uncertainty: string;
};

export type AgentAnswer = {
  answer_id: string;
  task_id: string;
  headline: string;
  answer_markdown: string;
  assumptions: string[];
  next_actions: string[];
  confidence: "low" | "medium" | "high";
  verification_mode:
    | "live_verified"
    | "cache"
    | "historical"
      | "degraded"
      | "no_campus_evidence";
  evidence: Evidence[];
  claims: AnswerClaim[];
  grounding: {
    status: string;
    summary: string;
    verifier_verdict: string;
    verifier_summary: string;
    citation_coverage: number;
    fully_supported_rate: number;
    findings: Array<{
      claim_id: string | null;
      severity: "info" | "warning" | "error";
      code: string;
      message: string;
    }>;
  } | null;
  performance: {
    scenario:
      | "no_live_read"
      | "public_live"
      | "campus_authenticated"
      | "multi_source_or_image";
    total_duration_ms: number;
    excluded_model_ttft_ms: number;
    controllable_duration_ms: number;
    first_progress_ms: number | null;
    model_call_count: number;
    tool_call_count: number;
    model_ttft_measurable: boolean;
    spans: Array<Record<string, unknown>>;
  } | null;
  profile_suggestions: ProfileAttribute[];
  created_at: string;
};

export type Health = {
  status: string;
  model_provider: "demo" | "openai" | "anthropic";
  model_configured: boolean;
};

export type AuthSession = {
  authenticated: boolean;
  auth_mode: "anonymous" | "optional_cas" | "required_cas";
  cas_enabled: boolean;
  subject_hint: string | null;
  visibility_scopes: string[];
  mirror_visibility_scopes: string[];
  login_url: string | null;
  service_registration_required: boolean;
  query_access: "direct" | "vpn" | "unavailable";
  query_access_expires_at: string | null;
  credential_handoff_available: boolean;
  read_only_capability: "campus_notice.read";
  subject_kind: "visitor" | "campus" | "local_admin";
  role: "visitor" | "student" | "admin";
  visitor_data_available: boolean;
  local_admin_enabled: boolean;
  local_admin_configured: boolean;
  local_admin_setup_available: boolean;
};

export type AgentAccess = {
  mode: "observe" | "enforce" | "paused";
  turnstile_enabled: boolean;
  turnstile_site_key: string | null;
  verification_required: boolean;
  window_remaining: number | null;
  daily_remaining: number | null;
  window_reset_at: string | null;
  daily_reset_at: string | null;
  running: number;
  queued: number;
};

export type ConversationSummary = {
  conversation_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  last_message: string | null;
  last_task_status: string | null;
};

export type ConversationDetail = {
  conversation_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  messages: Array<{
    message_id: string;
    role: "user" | "assistant";
    content: string;
    created_at: string;
    client_message_id: string | null;
  }>;
  tasks: Array<{
    task_id: string;
    user_message_id: string;
    status: string;
    error_code: string | null;
    answer_id: string | null;
    request_mode: string;
    parent_task_id: string | null;
    created_at: string;
    updated_at: string;
  }>;
};

export type AdminConversationTrace = ConversationDetail & {
  matched_trace_id: string;
  subject_kind: string | null;
};

export type ProfileAttribute = {
  attribute_id: string;
  attribute_key:
    | "education_level"
    | "cohort"
    | "college"
    | "major"
    | "goal"
    | "interest";
  attribute_value: string;
  status: "confirmed" | "suggested" | "rejected";
  source_kind: string;
  supporting_user_text: string;
  source_answer_id: string | null;
  created_at: string;
  updated_at: string;
};

export type StudentProfile = {
  personalization_enabled: boolean;
  onboarding_completed: boolean;
  confirmed: ProfileAttribute[];
  suggestions: ProfileAttribute[];
};

export type UserTodo = {
  todo_id: string;
  title: string;
  notes: string;
  due_at: string | null;
  status: "open" | "done" | "archived";
  source_answer_id: string | null;
  source_action_index: number | null;
  created_at: string;
  updated_at: string;
};

export type TaskStatus = {
  task_id: string;
  status: string;
  answer_id: string | null;
  error_code: string | null;
  queue_position: number;
};

export type AdminOverview = {
  task_count: number;
  completed_count: number;
  failed_count: number;
  success_rate: number;
  median_duration_ms: number | null;
  p95_duration_ms: number | null;
  feedback_count: number;
  source_alert_count: number;
};

export type ModelProtocol =
  | "demo"
  | "openai_responses"
  | "anthropic_messages";

export type ReasoningEffort =
  | "none"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max";

export type AdminModelConfiguration = {
  protocol: ModelProtocol;
  base_url: string | null;
  agent_model: string;
  utility_model: string;
  reasoning_effort: ReasoningEffort;
  utility_reasoning_effort: ReasoningEffort;
  timeout_seconds: number;
  api_key_configured: boolean;
  api_key_hint: string | null;
  source: "environment" | "database";
  updated_at: string | null;
};

export type AdminModelConfigurationUpdate = Omit<
  AdminModelConfiguration,
  "protocol" | "api_key_configured" | "api_key_hint" | "source" | "updated_at"
> & {
  protocol: Exclude<ModelProtocol, "demo">;
  api_key?: string;
};

export type AdminAgentPolicy = {
  mode: "observe" | "enforce" | "paused";
  subject_window_limit: number;
  subject_window_seconds: number;
  subject_daily_limit: number;
  max_running_per_subject: number;
  max_queued_per_subject: number;
  global_queue_limit: number;
  queue_timeout_seconds: number;
  agent_concurrency: number;
  model_concurrency: number;
  global_daily_task_limit: number;
  global_daily_model_call_limit: number;
  per_task_model_call_limit: number;
  max_message_length: number;
  scope_policy: "balanced" | "strict";
  timezone: string;
  turnstile_enabled: boolean;
  turnstile_site_key: string | null;
  turnstile_secret_configured: boolean;
  turnstile_secret_hint: string | null;
  verification_lease_hours: number;
  ip_new_subjects_per_hour: number;
  updated_at: string | null;
  today_task_count: number;
  today_model_call_count: number;
  today_rejection_counts: Record<string, number>;
  running_count: number;
  queued_count: number;
  oldest_queue_wait_seconds: number;
};

export type AdminAgentPolicyUpdate = {
  mode: AdminAgentPolicy["mode"];
  subject_window_limit: number;
  subject_window_seconds: number;
  subject_daily_limit: number;
  max_running_per_subject: number;
  max_queued_per_subject: number;
  global_queue_limit: number;
  queue_timeout_seconds: number;
  agent_concurrency: number;
  model_concurrency: number;
  global_daily_task_limit: number;
  global_daily_model_call_limit: number;
  per_task_model_call_limit: number;
  max_message_length: number;
  scope_policy: AdminAgentPolicy["scope_policy"];
  timezone: string;
  turnstile_enabled: boolean;
  turnstile_site_key: string | null;
  turnstile_secret?: string;
  verification_lease_hours: number;
  ip_new_subjects_per_hour: number;
};

export type Feedback = {
  feedback_id: string;
  answer_id: string;
  rating: "helpful" | "not_helpful" | "incorrect" | "outdated";
  categories: string[];
  comment: string;
  created_at: string;
  updated_at: string;
};

type CredentialChallenge = {
  challenge: string;
  expires_in_seconds: number;
  capability: "campus_notice.read";
};

type LocalAdminChallenge = {
  challenge: string;
  expires_in_seconds: number;
};

export type SourceStatus = {
  source_id: string;
  name: string;
  owner_department: string;
  base_url: string;
  allowed_hosts: string[];
  visibility: string;
  authority_level: string;
  connector_kind: string;
  poll_interval_seconds: number;
  default_ttl_seconds: number;
  enabled: boolean;
  resource_count: number;
  version_count: number;
  last_run_status: string | null;
  last_run_started_at: string | null;
  last_success_at: string | null;
  latest_version_at: string | null;
  fresh_until: string | null;
  health_state:
    | "healthy"
    | "degraded"
    | "stale"
    | "failing"
    | "disabled"
    | "waiting";
  consecutive_failures: number;
  chunk_count: number;
  entity_count: number;
};

export type SourceResource = {
  resource_id: string;
  canonical_uri: string;
  resource_type: string;
  first_seen_at: string;
  last_seen_at: string;
  current_version_id: string | null;
  title: string | null;
  publisher: string | null;
  published_at: string | null;
  observed_at: string | null;
  quality_status: string | null;
  version_count: number;
  chunk_count: number;
  entity_type: string | null;
  entity_status: string | null;
  deadline_at: string | null;
  audience_scopes: string[];
};

export type CampusEntity = {
  entity_id: string;
  entity_type: string;
  canonical_name: string;
  status: string;
  department: string | null;
  starts_at: string | null;
  deadline_at: string | null;
  ends_at: string | null;
  effective_from: string | null;
  effective_to: string | null;
  audience_scopes: string[];
  action_items: string[];
  locations: string[];
  document_number: string | null;
  relation_kind: string | null;
  related_title: string | null;
  confidence: number;
  extractor_version: string;
};

export type DocumentVersion = {
  version_id: string;
  content_hash: string;
  title: string;
  publisher: string;
  published_at: string | null;
  effective_from: string | null;
  effective_to: string | null;
  observed_at: string;
  parser_version: string;
  quality_status: string;
  is_current: boolean;
  chunk_count: number;
  entities: CampusEntity[];
};

export type VersionComparison = {
  resource_id: string;
  from_version_id: string;
  to_version_id: string;
  changed: boolean;
  title_changed: boolean;
  unified_diff: string;
  structured_changes: Record<
    string,
    { from: unknown; to: unknown }
  >;
  truncated: boolean;
};

export type SourceAlert = {
  source_id: string;
  source_name: string;
  severity: "info" | "warning" | "critical";
  code: string;
  message: string;
  detected_at: string;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly details: Record<string, unknown>;
  readonly retryAfter: number | null;

  constructor(
    message: string,
    options: {
      status: number;
      code?: string | null;
      details?: Record<string, unknown>;
      retryAfter?: number | null;
    },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code ?? null;
    this.details = options.details ?? {};
    this.retryAfter = options.retryAfter ?? null;
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const csrfToken =
    typeof document === "undefined" ? null : readCookie("hzcu_csrf");
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)
        ? { "X-CSRF-Token": csrfToken }
        : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.text();
    let message = body || `请求失败（${response.status}）`;
    let code: string | null = null;
    let details: Record<string, unknown> = {};
    try {
    const parsed = JSON.parse(body) as {
        detail?: string | { code?: string; message?: string; [key: string]: unknown };
      };
      if (typeof parsed.detail === "string") {
        message = parsed.detail;
      } else if (parsed.detail) {
        message = parsed.detail.message ?? message;
        code = typeof parsed.detail.code === "string" ? parsed.detail.code : null;
        details = parsed.detail;
      }
    } catch {
      // Keep the plain response body when the endpoint did not return JSON.
    }
    const retryAfterValue = response.headers.get("Retry-After");
    const retryAfter = retryAfterValue ? Number.parseInt(retryAfterValue, 10) : null;
    throw new ApiError(message, {
      status: response.status,
      code,
      details,
      retryAfter: Number.isFinite(retryAfter) ? retryAfter : null,
    });
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of document.cookie.split(";")) {
    const normalized = part.trim();
    if (normalized.startsWith(prefix)) {
      return decodeURIComponent(normalized.slice(prefix.length));
    }
  }
  return null;
}

export async function getHealth(): Promise<Health> {
  return apiFetch<Health>("/health");
}

export async function getAuthSession(): Promise<AuthSession> {
  return apiFetch<AuthSession>("/auth/me", { cache: "no-store" });
}

export async function getAgentAccess(): Promise<AgentAccess> {
  return apiFetch<AgentAccess>("/agent/access", { cache: "no-store" });
}

export async function verifyAgent(token: string): Promise<{ verified_until: string }> {
  return apiFetch<{ verified_until: string }>("/agent/verification", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export async function logoutCampusSession(): Promise<void> {
  return apiFetch<void>("/auth/logout", { method: "POST" });
}

async function submitLocalAdminCredentials(
  action: "login" | "setup",
  username: string,
  password: string,
): Promise<AuthSession> {
  const challenge = await apiFetch<LocalAdminChallenge>(
    "/auth/local-admin/challenge",
    { cache: "no-store" },
  );
  return apiFetch<AuthSession>(`/auth/local-admin/${action}`, {
    method: "POST",
    body: JSON.stringify({ username, password, challenge: challenge.challenge }),
  });
}

export async function loginLocalAdmin(
  username: string,
  password: string,
): Promise<AuthSession> {
  return submitLocalAdminCredentials("login", username, password);
}

export async function setupLocalAdmin(
  username: string,
  password: string,
): Promise<AuthSession> {
  return submitLocalAdminCredentials("setup", username, password);
}

export async function loginWithCampusCredentials(
  username: string,
  password: string,
): Promise<AuthSession> {
  const challenge = await apiFetch<CredentialChallenge>(
    "/auth/credential-challenge",
    { cache: "no-store" },
  );
  return apiFetch<AuthSession>("/auth/credential-login", {
    method: "POST",
    body: JSON.stringify({
      username,
      password,
      challenge: challenge.challenge,
    }),
  });
}

export async function getSources(): Promise<SourceStatus[]> {
  return apiFetch<SourceStatus[]>("/sources");
}

export async function getSourceAlerts(): Promise<SourceAlert[]> {
  return apiFetch<SourceAlert[]>("/sources/alerts");
}

export async function getSourceResources(
  sourceId: string,
  limit = 12,
): Promise<SourceResource[]> {
  return apiFetch<SourceResource[]>(
    `/sources/${encodeURIComponent(sourceId)}/resources?limit=${limit}`,
  );
}

export async function getResourceVersions(
  sourceId: string,
  resourceId: string,
): Promise<DocumentVersion[]> {
  return apiFetch<DocumentVersion[]>(
    `/sources/${encodeURIComponent(sourceId)}/resources/${encodeURIComponent(resourceId)}/versions`,
  );
}

export async function compareResourceVersions(
  sourceId: string,
  resourceId: string,
  fromVersionId?: string,
  toVersionId?: string,
): Promise<VersionComparison> {
  const parameters = new URLSearchParams();
  if (fromVersionId) parameters.set("from_version_id", fromVersionId);
  if (toVersionId) parameters.set("to_version_id", toVersionId);
  const suffix = parameters.size ? `?${parameters.toString()}` : "";
  return apiFetch<VersionComparison>(
    `/sources/${encodeURIComponent(sourceId)}/resources/${encodeURIComponent(resourceId)}/compare${suffix}`,
  );
}

export async function createConversation(title?: string): Promise<string> {
  const response = await apiFetch<{ conversation_id: string }>("/conversations", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
  return response.conversation_id;
}

export async function sendMessage(
  conversationId: string,
  message: string,
  clientMessageId = crypto.randomUUID(),
): Promise<{ task_id: string; stream_url: string; queue_position: number }> {
  return apiFetch(`/conversations/${conversationId}/messages`, {
    method: "POST",
    body: JSON.stringify({ message, client_message_id: clientMessageId }),
  });
}

export function streamUrl(path: string): string {
  if (/^https?:\/\//.test(path)) {
    return path;
  }
  if (typeof window === "undefined") return path;
  return new URL(path, window.location.origin).toString();
}

export async function listConversations(
  cursor?: string,
): Promise<{ items: ConversationSummary[]; next_cursor: string | null }> {
  return apiFetch(`/conversations${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export async function getConversation(
  conversationId: string,
): Promise<ConversationDetail> {
  return apiFetch(`/conversations/${encodeURIComponent(conversationId)}`);
}

export async function renameConversation(
  conversationId: string,
  title: string,
): Promise<ConversationSummary> {
  return apiFetch(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export async function deleteConversation(conversationId: string): Promise<void> {
  return apiFetch(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
  });
}

export async function getTask(taskId: string): Promise<TaskStatus> {
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}`, { cache: "no-store" });
}

export async function cancelTask(taskId: string): Promise<TaskStatus> {
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
  });
}

export async function retryTask(
  taskId: string,
): Promise<{ task_id: string; stream_url: string; queue_position: number }> {
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}/retry`, {
    method: "POST",
  });
}

export async function reverifyAnswer(
  answerId: string,
): Promise<{ task_id: string; stream_url: string; queue_position: number }> {
  return apiFetch(`/answers/${encodeURIComponent(answerId)}/reverify`, {
    method: "POST",
  });
}

export async function getAnswer(answerId: string): Promise<AgentAnswer> {
  return apiFetch(`/answers/${encodeURIComponent(answerId)}`, { cache: "no-store" });
}

export async function getProfile(): Promise<StudentProfile> {
  return apiFetch("/profile", { cache: "no-store" });
}

export async function updateProfile(payload: {
  personalization_enabled?: boolean;
  onboarding_completed?: boolean;
  attributes?: Array<{
    attribute_key: ProfileAttribute["attribute_key"];
    attribute_value: string;
  }>;
}): Promise<StudentProfile> {
  return apiFetch("/profile", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deletePersonalData(): Promise<void> {
  return apiFetch("/profile", { method: "DELETE" });
}

export async function resolveProfileSuggestion(
  attributeId: string,
  action: "confirm" | "reject",
): Promise<ProfileAttribute> {
  return apiFetch(
    `/profile/suggestions/${encodeURIComponent(attributeId)}/${action}`,
    { method: "POST" },
  );
}

export async function deleteProfileAttribute(
  attributeKey: ProfileAttribute["attribute_key"],
): Promise<void> {
  return apiFetch(
    `/profile/attributes/${encodeURIComponent(attributeKey)}`,
    { method: "DELETE" },
  );
}

export async function getTodos(): Promise<UserTodo[]> {
  return apiFetch("/todos", { cache: "no-store" });
}

export async function createTodo(payload: {
  title: string;
  notes?: string;
  due_at?: string | null;
  source_answer_id?: string;
  source_action_index?: number;
}): Promise<UserTodo> {
  return apiFetch("/todos", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateTodo(
  todoId: string,
  payload: Partial<Pick<UserTodo, "title" | "notes" | "due_at" | "status">>,
): Promise<UserTodo> {
  return apiFetch(`/todos/${encodeURIComponent(todoId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteTodo(todoId: string): Promise<void> {
  return apiFetch(`/todos/${encodeURIComponent(todoId)}`, { method: "DELETE" });
}

export async function putFeedback(
  answerId: string,
  payload: {
    rating: "helpful" | "not_helpful" | "incorrect" | "outdated";
    categories?: string[];
    comment?: string;
  },
): Promise<void> {
  return apiFetch(`/answers/${encodeURIComponent(answerId)}/feedback`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function postFeedback(
  answerId: string,
  payload: {
    rating: "helpful" | "not_helpful" | "incorrect" | "outdated";
    categories?: string[];
    comment?: string;
  },
): Promise<void> {
  return apiFetch("/feedback", {
    method: "POST",
    body: JSON.stringify({ answer_id: answerId, ...payload }),
  });
}

export async function mergeVisitorData(): Promise<void> {
  return apiFetch("/identity/merge-visitor", { method: "POST" });
}

export async function deleteVisitorData(): Promise<void> {
  return apiFetch("/identity/visitor-data", { method: "DELETE" });
}

export async function getAdminOverview(): Promise<AdminOverview> {
  return apiFetch("/admin/overview", { cache: "no-store" });
}

export async function getAdminModelConfiguration(): Promise<AdminModelConfiguration> {
  return apiFetch("/admin/model-config", { cache: "no-store" });
}

export async function getAdminAgentPolicy(): Promise<AdminAgentPolicy> {
  return apiFetch("/admin/agent-policy", { cache: "no-store" });
}

export async function updateAdminAgentPolicy(
  payload: AdminAgentPolicyUpdate,
): Promise<AdminAgentPolicy> {
  return apiFetch("/admin/agent-policy", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function updateAdminModelConfiguration(
  payload: AdminModelConfigurationUpdate,
): Promise<AdminModelConfiguration> {
  return apiFetch("/admin/model-config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function getAdminTaskHealth(): Promise<{
  items: Array<{
    task_id: string;
    conversation_id: string;
    answer_id: string | null;
    status: string;
    error_code: string | null;
    request_mode: string;
    model_call_count: number | null;
    tool_call_count: number | null;
    total_duration_ms: number | null;
    created_at: string;
  }>;
}> {
  return apiFetch("/admin/task-health", { cache: "no-store" });
}

export async function getAdminConversationTrace(
  traceId: string,
): Promise<AdminConversationTrace> {
  return apiFetch(`/admin/conversation-trace/${encodeURIComponent(traceId)}`, {
    cache: "no-store",
  });
}

export async function getAdminFeedback(): Promise<Feedback[]> {
  return apiFetch("/admin/feedback", { cache: "no-store" });
}
