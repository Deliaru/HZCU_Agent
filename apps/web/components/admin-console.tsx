"use client";

import {
  Activity,
  ArrowLeft,
  Check,
  CircleAlert,
  Clock3,
  Cpu,
  Gauge,
  KeyRound,
  LoaderCircle,
  Search,
  MessageSquareWarning,
  Network,
  Save,
  Server,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  getAdminFeedback,
  getAdminAgentPolicy,
  getAdminConversationTrace,
  getAdminModelConfiguration,
  getAdminOverview,
  getAdminTaskHealth,
  getAuthSession,
  getSourceAlerts,
  updateAdminAgentPolicy,
  updateAdminModelConfiguration,
} from "@/lib/api";
import type {
  AdminAgentPolicy,
  AdminAgentPolicyUpdate,
  AdminModelConfiguration,
  AdminConversationTrace,
  AdminOverview,
  Feedback,
  ReasoningEffort,
  SourceAlert,
} from "@/lib/api";

import { AppChrome } from "./app-chrome";

type TaskHealth = Awaited<ReturnType<typeof getAdminTaskHealth>>["items"];
type AdminView = "model" | "agent" | "telemetry";
type AccessState = "loading" | "redirecting" | "admin" | "denied" | "failed";
type ConfigDraft = {
  protocol: "openai_responses" | "anthropic_messages";
  baseUrl: string;
  agentModel: string;
  utilityModel: string;
  reasoningEffort: ReasoningEffort;
  utilityReasoningEffort: ReasoningEffort;
  timeoutSeconds: number;
};

type AgentPolicyDraft = Omit<AdminAgentPolicyUpdate, "turnstile_secret">;

const EFFORTS: ReasoningEffort[] = [
  "none",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];

function duration(value: number | null): string {
  if (value === null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

function draftFrom(config: AdminModelConfiguration): ConfigDraft {
  return {
    protocol:
      config.protocol === "anthropic_messages"
        ? "anthropic_messages"
        : "openai_responses",
    baseUrl: config.base_url ?? "",
    agentModel: config.agent_model,
    utilityModel: config.utility_model,
    reasoningEffort: config.reasoning_effort,
    utilityReasoningEffort: config.utility_reasoning_effort,
    timeoutSeconds: config.timeout_seconds,
  };
}

function agentPolicyDraftFrom(policy: AdminAgentPolicy): AgentPolicyDraft {
  return {
    mode: policy.mode,
    subject_window_limit: policy.subject_window_limit,
    subject_window_seconds: policy.subject_window_seconds,
    subject_daily_limit: policy.subject_daily_limit,
    max_running_per_subject: policy.max_running_per_subject,
    max_queued_per_subject: policy.max_queued_per_subject,
    global_queue_limit: policy.global_queue_limit,
    queue_timeout_seconds: policy.queue_timeout_seconds,
    agent_concurrency: policy.agent_concurrency,
    model_concurrency: policy.model_concurrency,
    global_daily_task_limit: policy.global_daily_task_limit,
    global_daily_model_call_limit: policy.global_daily_model_call_limit,
    per_task_model_call_limit: policy.per_task_model_call_limit,
    max_message_length: policy.max_message_length,
    scope_policy: policy.scope_policy,
    timezone: policy.timezone,
    turnstile_enabled: policy.turnstile_enabled,
    turnstile_site_key: policy.turnstile_site_key,
    verification_lease_hours: policy.verification_lease_hours,
    ip_new_subjects_per_hour: policy.ip_new_subjects_per_hour,
  };
}

export function AdminConsole() {
  const [access, setAccess] = useState<AccessState>("loading");
  const [view, setView] = useState<AdminView>("model");
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [tasks, setTasks] = useState<TaskHealth>([]);
  const [alerts, setAlerts] = useState<SourceAlert[]>([]);
  const [feedback, setFeedback] = useState<Feedback[]>([]);
  const [configuration, setConfiguration] =
    useState<AdminModelConfiguration | null>(null);
  const [draft, setDraft] = useState<ConfigDraft | null>(null);
  const [agentPolicy, setAgentPolicy] = useState<AdminAgentPolicy | null>(null);
  const [agentPolicyDraft, setAgentPolicyDraft] = useState<AgentPolicyDraft | null>(null);
  const [turnstileSecret, setTurnstileSecret] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [savingAgentPolicy, setSavingAgentPolicy] = useState(false);
  const [notice, setNotice] = useState<string>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const session = await getAuthSession();
        if (cancelled) return;
        if (!session.authenticated) {
          setAccess("redirecting");
          window.location.replace("/login?return_to=/admin");
          return;
        }
        if (session.role !== "admin") {
          setAccess("denied");
          return;
        }
        const [config, nextPolicy, nextOverview, nextTasks, nextFeedback, nextAlerts] =
          await Promise.all([
            getAdminModelConfiguration(),
            getAdminAgentPolicy(),
            getAdminOverview(),
            getAdminTaskHealth(),
            getAdminFeedback(),
            getSourceAlerts(),
          ]);
        if (cancelled) return;
        setConfiguration(config);
        setDraft(draftFrom(config));
        setAgentPolicy(nextPolicy);
        setAgentPolicyDraft(agentPolicyDraftFrom(nextPolicy));
        setOverview(nextOverview);
        setTasks(nextTasks.items);
        setFeedback(nextFeedback);
        setAlerts(nextAlerts);
        setAccess("admin");
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "管理后台读取失败");
        setAccess("failed");
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function saveModelConfiguration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft || saving) return;
    setSaving(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const saved = await updateAdminModelConfiguration({
        protocol: draft.protocol,
        base_url: draft.baseUrl.trim() || null,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        agent_model: draft.agentModel.trim(),
        utility_model: draft.utilityModel.trim(),
        reasoning_effort: draft.reasoningEffort,
        utility_reasoning_effort: draft.utilityReasoningEffort,
        timeout_seconds: Number(draft.timeoutSeconds),
      });
      setConfiguration(saved);
      setDraft(draftFrom(saved));
      setApiKey("");
      setNotice("配置已写入服务器，新创建的 Agent 任务将使用这组端点。 ");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "模型配置保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function saveAgentPolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!agentPolicyDraft || savingAgentPolicy) return;
    setSavingAgentPolicy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const saved = await updateAdminAgentPolicy({
        ...agentPolicyDraft,
        ...(turnstileSecret.trim() ? { turnstile_secret: turnstileSecret.trim() } : {}),
      });
      setAgentPolicy(saved);
      setAgentPolicyDraft(agentPolicyDraftFrom(saved));
      setTurnstileSecret("");
      setNotice("Agent 策略已保存：新准入立即生效；运行中任务不取消。 ");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Agent 策略保存失败");
    } finally {
      setSavingAgentPolicy(false);
    }
  }

  if (access === "loading" || access === "redirecting") {
    return (
      <main className="admin-entry-loading">
        <LoaderCircle size={24} />
        <span>{access === "redirecting" ? "正在进入管理员登录" : "正在校验管理员身份"}</span>
      </main>
    );
  }

  if (access === "denied" || access === "failed") {
    return (
      <AppChrome
        section="admin"
        className="admin-shell admin-shell-denied"
        channel="HZCU // SERVER CONTROL"
        mode="SERVER ADMIN / ROLE CONTROL"
        eyebrow="03 / ADMIN CONSOLE"
        title="访问校验"
        utilities={
          <a className="back-to-agent" href="/">
            <ArrowLeft size={14} /> 返回提问
          </a>
        }
      >
        <section className="admin-denied">
          <span className="admin-denied-code">403 / ROLE GATE</span>
          <ShieldCheck size={32} />
          <h1>{access === "failed" ? "管理服务暂时无法连接" : "当前身份不是管理员"}</h1>
          <p>
            {access === "failed"
              ? error
              : "管理后台只对已配置的服务器管理员身份开放。"}
          </p>
          <a href="/login?return_to=/admin">
            {access === "failed" ? "重新进入管理后台" : "更换管理员身份"}
          </a>
        </section>
      </AppChrome>
    );
  }

  return (
    <AppChrome
      section="admin"
      className="admin-shell"
      channel="HZCU // SERVER CONTROL"
      mode="SERVER ADMIN / SYSTEM CONFIG"
      eyebrow="03 / ADMIN CONSOLE"
      title="系统管理"
      utilities={
        <>
          <a className="back-to-agent" href="/">
            <ArrowLeft size={14} /> 返回提问
          </a>
          <span className="admin-role-mark">
            <ShieldCheck size={15} /> SERVER ADMIN
          </span>
        </>
      }
    >
      <section className="admin-content">
        <header className="admin-console-heading">
          <div>
            <p className="eyebrow">SERVER CONSOLE / CAMPUS ADMIN</p>
            <h1>
              公用模型，<em>由服务器决定。</em>
            </h1>
          </div>
          <p>
            在这里维护所有用户共用的模型协议、端点、密钥与模型名称。
            保存后由服务器统一接管，浏览器不会读取已保存的 API 密钥。
          </p>
          <strong>
            {configuration?.protocol === "anthropic_messages" ? "A" : "R"}
            <span>ACTIVE PROTOCOL</span>
          </strong>
        </header>

        <nav className="admin-section-nav" aria-label="管理后台功能">
          <button
            type="button"
            className={view === "model" ? "active" : ""}
            onClick={() => setView("model")}
          >
            <span>01</span><Server size={17} /> 模型与 API
          </button>
          <button
            type="button"
            className={view === "agent" ? "active" : ""}
            onClick={() => setView("agent")}
          >
            <span>02</span><ShieldCheck size={17} /> Agent 策略
          </button>
          <button
            type="button"
            className={view === "telemetry" ? "active" : ""}
            onClick={() => setView("telemetry")}
          >
            <span>03</span><Activity size={17} /> 运行监测
          </button>
        </nav>

        {view === "model" && draft && configuration ? (
          <section className="model-config-workspace">
            <aside className="model-config-status">
              <span className="model-config-kicker">ACTIVE ENDPOINT</span>
              <b>{configuration.protocol === "anthropic_messages" ? "ANTHROPIC" : configuration.protocol === "demo" ? "DEMO" : "RESPONSES"}</b>
              <dl>
                <div><dt>配置来源</dt><dd>{configuration.source === "database" ? "管理后台" : "服务器启动参数"}</dd></div>
                <div><dt>密钥状态</dt><dd>{configuration.api_key_hint ?? "未配置"}</dd></div>
                <div><dt>主模型</dt><dd>{configuration.agent_model}</dd></div>
                <div><dt>辅助模型</dt><dd>{configuration.utility_model}</dd></div>
                <div><dt>最近更新</dt><dd>{configuration.updated_at ? new Date(configuration.updated_at).toLocaleString("zh-CN") : "随服务启动"}</dd></div>
              </dl>
              <p><i /> 当前状态只展示密钥尾号，已保存的完整密钥不会返回浏览器。</p>
            </aside>

            <form className="model-config-form" onSubmit={saveModelConfiguration}>
              <header>
                <div>
                  <p className="eyebrow">PUBLIC MODEL GATEWAY</p>
                  <h2>公用模型端点</h2>
                </div>
                <span>{draft.protocol === "anthropic_messages" ? "POST /v1/messages" : "POST /v1/responses"}</span>
              </header>

              <fieldset className="protocol-selector">
                <legend>接口协议</legend>
                <label className={draft.protocol === "openai_responses" ? "active" : ""}>
                  <input
                    type="radio"
                    name="protocol"
                    value="openai_responses"
                    checked={draft.protocol === "openai_responses"}
                    onChange={() => setDraft({ ...draft, protocol: "openai_responses" })}
                  />
                  <Network size={19} />
                  <span><b>OpenAI Responses</b><small>Responses API 与兼容中转端点</small></span>
                  <i>R</i>
                </label>
                <label className={draft.protocol === "anthropic_messages" ? "active" : ""}>
                  <input
                    type="radio"
                    name="protocol"
                    value="anthropic_messages"
                    checked={draft.protocol === "anthropic_messages"}
                    onChange={() => setDraft({ ...draft, protocol: "anthropic_messages" })}
                  />
                  <Cpu size={19} />
                  <span><b>Anthropic Messages</b><small>Messages API 与 Anthropic 兼容端点</small></span>
                  <i>A</i>
                </label>
              </fieldset>

              <div className="config-field full">
                <label htmlFor="model-base-url">API 根地址或完整端点</label>
                <div>
                  <Network size={15} />
                  <input
                    id="model-base-url"
                    type="url"
                    value={draft.baseUrl}
                    onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })}
                    placeholder={draft.protocol === "anthropic_messages" ? "https://api.anthropic.com 或 …/v1/messages" : "https://api.openai.com/v1 或 …/responses"}
                    spellCheck={false}
                  />
                </div>
                <small>留空使用协议官方地址；粘贴完整端点时服务器会自动归一化。</small>
              </div>

              <div className="config-field full">
                <label htmlFor="model-api-key">公用 API 密钥</label>
                <div>
                  <KeyRound size={15} />
                  <input
                    id="model-api-key"
                    type="password"
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                    placeholder={configuration.api_key_configured ? `留空保留当前密钥 ${configuration.api_key_hint ?? ""}` : "首次配置必须填写"}
                    autoComplete="new-password"
                    spellCheck={false}
                  />
                </div>
                <small>保存后立即清空输入框；再次打开后台也不会回填。</small>
              </div>

              <div className="config-field">
                <label htmlFor="agent-model">主 Agent 模型</label>
                <div>
                  <Cpu size={15} />
                  <input
                    id="agent-model"
                    value={draft.agentModel}
                    onChange={(event) => setDraft({ ...draft, agentModel: event.target.value })}
                    required
                    spellCheck={false}
                  />
                </div>
              </div>

              <div className="config-field">
                <label htmlFor="utility-model">辅助推演模型</label>
                <div>
                  <Cpu size={15} />
                  <input
                    id="utility-model"
                    value={draft.utilityModel}
                    onChange={(event) => setDraft({ ...draft, utilityModel: event.target.value })}
                    required
                    spellCheck={false}
                  />
                </div>
              </div>

              <div className="config-field">
                <label htmlFor="agent-effort">主模型推理强度</label>
                <select
                  id="agent-effort"
                  value={draft.reasoningEffort}
                  onChange={(event) => setDraft({ ...draft, reasoningEffort: event.target.value as ReasoningEffort })}
                >
                  {EFFORTS.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
                </select>
              </div>

              <div className="config-field">
                <label htmlFor="utility-effort">辅助模型推理强度</label>
                <select
                  id="utility-effort"
                  value={draft.utilityReasoningEffort}
                  onChange={(event) => setDraft({ ...draft, utilityReasoningEffort: event.target.value as ReasoningEffort })}
                >
                  {EFFORTS.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
                </select>
              </div>

              <div className="config-field timeout">
                <label htmlFor="model-timeout">请求超时（秒）</label>
                <input
                  id="model-timeout"
                  type="number"
                  min={10}
                  max={600}
                  step={1}
                  value={draft.timeoutSeconds}
                  onChange={(event) => setDraft({ ...draft, timeoutSeconds: Number(event.target.value) })}
                  required
                />
              </div>

              <div className="config-form-note">
                <span>协议行为</span>
                <p>
                  Responses 直接使用结构化输出；Anthropic 优先使用 Messages 结构化输出，
                  兼容端点不支持时自动改用一次原子化工具返回，不改变 Agent 的推演逻辑。
                </p>
              </div>

              {error ? <div className="config-save-message error" role="alert">{error}</div> : null}
              {notice ? <div className="config-save-message success"><Check size={14} />{notice}</div> : null}

              <footer className="config-form-actions">
                <span>SERVER-WIDE / 新任务生效</span>
                <button type="submit" disabled={saving}>
                  {saving ? <LoaderCircle size={16} /> : <Save size={16} />}
                  {saving ? "正在写入服务器" : "保存并应用配置"}
                </button>
              </footer>
            </form>
          </section>
        ) : null}

        {view === "telemetry" ? (
          <TelemetryPanel
            overview={overview}
            tasks={tasks}
            feedback={feedback}
            alerts={alerts}
          />
        ) : null}
        {view === "agent" && agentPolicy && agentPolicyDraft ? (
          <AgentPolicyPanel
            policy={agentPolicy}
            draft={agentPolicyDraft}
            secret={turnstileSecret}
            saving={savingAgentPolicy}
            notice={notice}
            error={error}
            onDraftChange={setAgentPolicyDraft}
            onSecretChange={setTurnstileSecret}
            onSubmit={saveAgentPolicy}
          />
        ) : null}
      </section>
    </AppChrome>
  );
}

function TelemetryPanel({
  overview,
  tasks,
  feedback,
  alerts,
}: {
  overview: AdminOverview | null;
  tasks: TaskHealth;
  feedback: Feedback[];
  alerts: SourceAlert[];
}) {
  return (
    <section className="admin-telemetry">
      <div className="admin-metrics">
        <article><Activity size={19} /><span><small>任务成功率</small><b>{overview ? `${(overview.success_rate * 100).toFixed(1)}%` : "—"}</b></span></article>
        <article><Clock3 size={19} /><span><small>端到端中位数</small><b>{duration(overview?.median_duration_ms ?? null)}</b></span></article>
        <article><Gauge size={19} /><span><small>端到端 P95</small><b>{duration(overview?.p95_duration_ms ?? null)}</b></span></article>
        <article><MessageSquareWarning size={19} /><span><small>反馈 / 来源告警</small><b>{overview ? `${overview.feedback_count} / ${overview.source_alert_count}` : "—"}</b></span></article>
      </div>

      <TraceLookup tasks={tasks} />

      <section className="admin-table">
        <header><div><p className="eyebrow">LATEST TASK HEALTH</p><h2>最近任务</h2></div><span>{tasks.length.toString().padStart(3, "0")} REC</span></header>
        <div className="admin-table-scroll"><table><thead><tr><th>任务</th><th>状态</th><th>模式</th><th>模型 / 工具</th><th>耗时</th><th>错误</th></tr></thead><tbody>
          {tasks.map((task) => <tr key={task.task_id}><td><code>{task.task_id.slice(-10)}</code></td><td><i className={`task-state ${task.status}`}>{task.status}</i></td><td>{task.request_mode}</td><td>{task.model_call_count ?? "—"} / {task.tool_call_count ?? "—"}</td><td>{duration(task.total_duration_ms)}</td><td>{task.error_code ?? "—"}</td></tr>)}
        </tbody></table></div>
      </section>

      <section className="admin-table">
        <header><div><p className="eyebrow">USER SIGNALS</p><h2>最近反馈</h2></div><span>{feedback.length.toString().padStart(3, "0")} REC</span></header>
        <div className="admin-table-scroll"><table><thead><tr><th>回答</th><th>评价</th><th>类别</th><th>说明</th><th>时间</th></tr></thead><tbody>
          {feedback.map((item) => <tr key={item.feedback_id}><td><code>{item.answer_id.slice(-10)}</code></td><td>{item.rating}</td><td>{item.categories.join(" · ") || "—"}</td><td>{item.comment || "—"}</td><td>{new Date(item.updated_at).toLocaleString("zh-CN")}</td></tr>)}
        </tbody></table></div>
      </section>

      <section className="admin-alerts">
        <header><CircleAlert size={17} /><div><p className="eyebrow">SOURCE WATCH</p><h2>来源告警</h2></div></header>
        {alerts.length ? alerts.map((alert) => <article key={`${alert.source_id}-${alert.code}`}><i>{alert.severity}</i><span><b>{alert.source_name}</b><small>{alert.message}</small></span></article>) : <p>当前没有来源告警。</p>}
      </section>
    </section>
  );
}

function AgentPolicyPanel({
  policy,
  draft,
  secret,
  saving,
  notice,
  error,
  onDraftChange,
  onSecretChange,
  onSubmit,
}: {
  policy: AdminAgentPolicy;
  draft: AgentPolicyDraft;
  secret: string;
  saving: boolean;
  notice?: string;
  error?: string;
  onDraftChange: (draft: AgentPolicyDraft) => void;
  onSecretChange: (secret: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const setNumber = (key: keyof AgentPolicyDraft, value: string) => {
    onDraftChange({ ...draft, [key]: Number(value) } as AgentPolicyDraft);
  };

  return (
    <section className="agent-policy-workspace">
      <aside className="agent-policy-status">
        <span className="model-config-kicker">AGENT ADMISSION / LIVE POLICY</span>
        <b>{draft.mode.toUpperCase()}</b>
        <dl>
          <div><dt>今日任务</dt><dd>{policy.today_task_count} / {policy.global_daily_task_limit}</dd></div>
          <div><dt>今日模型调用</dt><dd>{policy.today_model_call_count} / {policy.global_daily_model_call_limit}</dd></div>
          <div><dt>今日拒绝</dt><dd>{Object.entries(policy.today_rejection_counts).length ? Object.entries(policy.today_rejection_counts).map(([code, count]) => `${code} ${count}`).join(" · ") : "—"}</dd></div>
          <div><dt>运行 / 排队</dt><dd>{policy.running_count} / {policy.queued_count}</dd></div>
          <div><dt>最长等待</dt><dd>{policy.oldest_queue_wait_seconds ? `${policy.oldest_queue_wait_seconds}s` : "—"}</dd></div>
          <div><dt>Turnstile</dt><dd>{policy.turnstile_secret_configured ? `已配置 ${policy.turnstile_secret_hint ?? ""}` : "未配置"}</dd></div>
        </dl>
        <p><i /> 保存后新准入立即使用新策略；已经运行的任务不会被取消。</p>
      </aside>

      <form className="model-config-form agent-policy-form" onSubmit={onSubmit}>
        <header>
          <div>
            <p className="eyebrow">PUBLIC AGENT SAFETY GATE</p>
            <h2>匿名试用与用途保护</h2>
          </div>
          <span>DB / HOT RELOAD</span>
        </header>

        <fieldset className="protocol-selector agent-policy-modes">
          <legend>公众状态</legend>
          {([
            ["observe", "观察", "记录命中与额度，不拒绝公众任务"],
            ["enforce", "执行", "启用额度、队列、人机验证和用途限制"],
            ["paused", "暂停", "暂停公众新任务，管理员仍可测试"],
          ] as const).map(([value, label, description]) => (
            <label className={draft.mode === value ? "active" : ""} key={value}>
              <input
                type="radio"
                name="agent-policy-mode"
                value={value}
                checked={draft.mode === value}
                onChange={() => onDraftChange({ ...draft, mode: value })}
              />
              <ShieldCheck size={18} />
              <span><b>{label}</b><small>{description}</small></span>
              <i>{value === "observe" ? "O" : value === "enforce" ? "E" : "P"}</i>
            </label>
          ))}
        </fieldset>

        <div className="agent-policy-grid">
          <div className="config-field">
            <label htmlFor="agent-window-limit">匿名窗口次数</label>
            <input id="agent-window-limit" type="number" min={1} max={100} value={draft.subject_window_limit} onChange={(event) => setNumber("subject_window_limit", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-window-seconds">窗口时长（秒）</label>
            <input id="agent-window-seconds" type="number" min={1} max={86400} value={draft.subject_window_seconds} onChange={(event) => setNumber("subject_window_seconds", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-daily-limit">匿名每日次数</label>
            <input id="agent-daily-limit" type="number" min={1} max={1000} value={draft.subject_daily_limit} onChange={(event) => setNumber("subject_daily_limit", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-max-queued">单主体排队上限</label>
            <input id="agent-max-queued" type="number" min={0} max={8} value={draft.max_queued_per_subject} onChange={(event) => setNumber("max_queued_per_subject", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-max-running">单主体运行上限</label>
            <input id="agent-max-running" type="number" min={1} max={4} value={draft.max_running_per_subject} onChange={(event) => setNumber("max_running_per_subject", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-queue-limit">全局队列上限</label>
            <input id="agent-queue-limit" type="number" min={1} max={10000} value={draft.global_queue_limit} onChange={(event) => setNumber("global_queue_limit", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-queue-timeout">排队超时（秒）</label>
            <input id="agent-queue-timeout" type="number" min={1} max={86400} value={draft.queue_timeout_seconds} onChange={(event) => setNumber("queue_timeout_seconds", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-concurrency">Agent 并发</label>
            <input id="agent-concurrency" type="number" min={1} max={64} value={draft.agent_concurrency} onChange={(event) => setNumber("agent_concurrency", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="model-concurrency">模型调用并发</label>
            <input id="model-concurrency" type="number" min={1} max={64} value={draft.model_concurrency} onChange={(event) => setNumber("model_concurrency", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="agent-daily-budget">全局每日任务</label>
            <input id="agent-daily-budget" type="number" min={1} max={100000} value={draft.global_daily_task_limit} onChange={(event) => setNumber("global_daily_task_limit", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="model-daily-budget">全局每日模型调用</label>
            <input id="model-daily-budget" type="number" min={1} max={1000000} value={draft.global_daily_model_call_limit} onChange={(event) => setNumber("global_daily_model_call_limit", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="task-call-limit">单任务模型调用</label>
            <input id="task-call-limit" type="number" min={1} max={100} value={draft.per_task_model_call_limit} onChange={(event) => setNumber("per_task_model_call_limit", event.target.value)} />
          </div>
          <div className="config-field">
            <label htmlFor="message-length">问题长度（字）</label>
            <input id="message-length" type="number" min={1} max={20000} value={draft.max_message_length} onChange={(event) => setNumber("max_message_length", event.target.value)} />
          </div>
        </div>

        <div className="agent-policy-options">
          <div className="config-field">
            <label htmlFor="scope-policy">用途策略</label>
            <select id="scope-policy" value={draft.scope_policy} onChange={(event) => onDraftChange({ ...draft, scope_policy: event.target.value as AgentPolicyDraft["scope_policy"] })}>
              <option value="balanced">平衡校园限定</option>
              <option value="strict">严格校园限定</option>
            </select>
          </div>
          <div className="config-field">
            <label htmlFor="policy-timezone">日界线时区</label>
            <input id="policy-timezone" value={draft.timezone} onChange={(event) => onDraftChange({ ...draft, timezone: event.target.value })} />
          </div>
        </div>

        <fieldset className="agent-policy-turnstile">
          <legend>Turnstile 人机验证</legend>
          <label className="agent-policy-check">
            <input type="checkbox" checked={draft.turnstile_enabled} onChange={(event) => onDraftChange({ ...draft, turnstile_enabled: event.target.checked })} />
            <span><b>启用验证租约</b><small>仅在 enforce 模式对匿名主体要求验证</small></span>
          </label>
          <div className="agent-policy-options">
            <div className="config-field">
              <label htmlFor="turnstile-site-key">Sitekey</label>
              <input id="turnstile-site-key" value={draft.turnstile_site_key ?? ""} onChange={(event) => onDraftChange({ ...draft, turnstile_site_key: event.target.value.trim() || null })} spellCheck={false} />
            </div>
            <div className="config-field">
              <label htmlFor="turnstile-secret">Secret（只写入，不回显）</label>
              <input id="turnstile-secret" type="password" value={secret} onChange={(event) => onSecretChange(event.target.value)} placeholder={policy.turnstile_secret_configured ? `留空保留当前密钥 ${policy.turnstile_secret_hint ?? ""}` : "配置后才能切换 enforce"} autoComplete="new-password" spellCheck={false} />
            </div>
            <div className="config-field">
              <label htmlFor="verification-lease">验证租约（小时）</label>
              <input id="verification-lease" type="number" min={1} max={168} value={draft.verification_lease_hours} onChange={(event) => setNumber("verification_lease_hours", event.target.value)} />
            </div>
            <div className="config-field">
              <label htmlFor="ip-new-subjects">同出口新主体 / 小时</label>
              <input id="ip-new-subjects" type="number" min={1} max={10000} value={draft.ip_new_subjects_per_hour} onChange={(event) => setNumber("ip_new_subjects_per_hour", event.target.value)} />
            </div>
          </div>
        </fieldset>

        {error ? <div className="config-save-message error" role="alert">{error}</div> : null}
        {notice ? <div className="config-save-message success"><Check size={14} />{notice}</div> : null}
        <footer className="config-form-actions">
          <span>NEW ADMISSION / HOT RELOAD</span>
          <button type="submit" disabled={saving}>
            {saving ? <LoaderCircle size={16} /> : <Save size={16} />}
            {saving ? "正在应用策略" : "保存并应用策略"}
          </button>
        </footer>
      </form>
    </section>
  );
}

function TraceLookup({ tasks }: { tasks: TaskHealth }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<AdminConversationTrace | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();

  async function lookup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const traceId = query.trim();
    if (!traceId || loading) return;
    setLoading(true);
    setError(undefined);
    try {
      setResult(await getAdminConversationTrace(traceId));
    } catch (cause) {
      setResult(null);
      setError(cause instanceof Error ? cause.message : "没有找到这条追溯记录");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="admin-trace-lookup">
      <header>
        <div><p className="eyebrow">CONVERSATION TRACE</p><h2>会话追溯</h2></div>
        <span>支持会话 / 消息 / 任务 / 回答 ID</span>
      </header>
      <form onSubmit={lookup}>
        <Search size={17} />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="粘贴 conv_、msg_、task_ 或 ans_ 开头的完整 ID"
          aria-label="追溯 ID"
          spellCheck={false}
        />
        <button type="submit" disabled={loading || !query.trim()}>
          {loading ? <LoaderCircle size={15} /> : "查找会话"}
        </button>
      </form>
      {!result && !error && tasks[0] ? (
        <button
          type="button"
          className="admin-trace-recent"
          onClick={() => setQuery(tasks[0].conversation_id)}
        >
          最近会话 <code>{tasks[0].conversation_id}</code>
        </button>
      ) : null}
      {error ? <p className="admin-trace-error" role="alert">{error}</p> : null}
      {result ? (
        <div className="admin-trace-result">
          <div className="admin-trace-summary">
            <span><small>会话 ID</small><code>{result.conversation_id}</code></span>
            <span><small>主体类型</small><b>{result.subject_kind ?? "unknown"}</b></span>
            <span><small>创建时间</small><b>{new Date(result.created_at).toLocaleString("zh-CN")}</b></span>
            <span><small>任务数</small><b>{result.tasks.length}</b></span>
          </div>
          <div className="admin-trace-dialogue">
            {result.messages.map((message) => (
              <article key={message.message_id} className={message.role}>
                <header><b>{message.role === "user" ? "用户" : "Agent"}</b><code>{message.message_id}</code></header>
                <p>{message.content}</p>
              </article>
            ))}
          </div>
          <div className="admin-trace-tasks">
            {result.tasks.map((task) => (
              <article key={task.task_id}>
                <i className={`task-state ${task.status}`}>{task.status}</i>
                <span><small>任务</small><code>{task.task_id}</code></span>
                <span><small>回答</small><code>{task.answer_id ?? "—"}</code></span>
                <span><small>模式</small><b>{task.request_mode}</b></span>
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}
