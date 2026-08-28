"use client";

import {
  ArrowUp,
  Check,
  ChevronRight,
  CircleAlert,
  Clock3,
  Copy,
  History,
  ListChecks,
  LoaderCircle,
  Menu,
  MessageCircleWarning,
  PanelRightOpen,
  RefreshCw,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  ThumbsDown,
  ThumbsUp,
  Waypoints,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import ReactMarkdown from "react-markdown";

import {
  cancelTask,
  createConversation,
  createTodo,
  getAgentAccess,
  getAnswer,
  getAuthSession,
  getConversation,
  getHealth,
  getProfile,
  getTask,
  getTodos,
  listConversations,
  logoutCampusSession,
  mergeVisitorData,
  putFeedback,
  resolveProfileSuggestion,
  retryTask,
  reverifyAnswer,
  sendMessage,
  streamUrl,
  verifyAgent,
  ApiError,
} from "@/lib/api";
import type {
  AgentAccess,
  AgentAnswer,
  AuthSession,
  ConversationSummary,
  Evidence,
  Health,
  StudentProfile,
  UserTodo,
} from "@/lib/api";

import { AppChrome } from "./app-chrome";
import { CongyuAgentView } from "./congyu-agent-view";
import { ConversationRail } from "./conversation-rail";
import { EvidenceDesk } from "./evidence-desk";
import { IdentityControl } from "./identity-control";
import { MySpacePanel, OnboardingPanel } from "./product-panels";
import { useTheme } from "./theme-provider";

type Stage =
  | "idle"
  | "queued"
  | "understanding"
  | "planning"
  | "investigating"
  | "composing"
  | "completed"
  | "failed";

type ChatMessage =
  | { id: string; role: "user"; content: string; createdAt: string }
  | { id: string; role: "assistant"; answer: AgentAnswer; createdAt: string };

type PlanPreview = {
  objective: string;
  steps: Array<{ id: string; purpose: string; tool: string }>;
};

type TraceActivity = {
  id: string;
  stage: Stage;
  label: string;
  detail?: string;
  createdAt: number;
};

const TRACE_PHASES: Array<{
  id: Extract<Stage, "understanding" | "planning" | "investigating" | "composing">;
  index: string;
  label: string;
  description: string;
}> = [
  {
    id: "understanding",
    index: "01",
    label: "理解",
    description: "确认问题与上下文",
  },
  {
    id: "planning",
    index: "02",
    label: "规划",
    description: "建立调查路径",
  },
  {
    id: "investigating",
    index: "03",
    label: "调查",
    description: "检索并阅读原文",
  },
  {
    id: "composing",
    index: "04",
    label: "成稿",
    description: "组织答案与引用",
  },
];

const DEFAULT_QUESTIONS = [
  "这个学年暑假后什么时候开学？",
  "国创大概什么时候会中期检查，校创需要吗？",
  "选课时怎样兼顾绩点、兴趣和后续发展？",
];

const STAGE_LABELS: Record<Stage, string> = {
  idle: "等待提问",
  queued: "任务排队中",
  understanding: "理解你的问题",
  planning: "规划调查路径",
  investigating: "核验官方信息",
  composing: "组织回答与引用",
  completed: "回答已完成",
  failed: "本次调查中断",
};

function normalizeEvidence(item: Partial<Evidence>): Evidence | null {
  if (
    typeof item.evidence_id !== "string" ||
    typeof item.title !== "string" ||
    typeof item.publisher !== "string" ||
    typeof item.canonical_url !== "string" ||
    typeof item.observed_at !== "string" ||
    typeof item.excerpt !== "string" ||
    typeof item.source_id !== "string"
  ) {
    return null;
  }
  return {
    evidence_id: item.evidence_id,
    title: item.title,
    publisher: item.publisher,
    canonical_url: item.canonical_url,
    published_at: item.published_at ?? null,
    observed_at: item.observed_at,
    fresh_until: item.fresh_until ?? null,
    excerpt: item.excerpt,
    source_id: item.source_id,
    resource_ref: item.resource_ref ?? null,
    document_version_id: item.document_version_id ?? null,
    authority_level: item.authority_level ?? "unknown",
    audience_scopes: item.audience_scopes ?? [],
    effective_from: item.effective_from ?? null,
    effective_to: item.effective_to ?? null,
    retrieval_mode: item.retrieval_mode ?? "unknown",
  };
}

function normalizeEvidenceList(value: unknown): Evidence[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) =>
      item && typeof item === "object"
        ? normalizeEvidence(item as Partial<Evidence>)
        : null,
    )
    .filter((item): item is Evidence => item !== null);
}

function withCitationLinks(markdown: string): string {
  return markdown.replace(/\[来源(\d+)\]/g, "[来源$1](#evidence-$1)");
}

function formatWait(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function formatResetAt(value: string | null): string {
  if (!value) return "稍后";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "稍后";
  return date.toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function describeAgentError(cause: unknown): string {
  if (!(cause instanceof ApiError)) {
    return cause instanceof Error ? cause.message : "任务提交失败，请稍后重试。";
  }
  if (cause.code === "HUMAN_VERIFICATION_REQUIRED") {
    return "请先完成一次人机验证，验证成功后会自动继续原问题。";
  }
  if (cause.code === "TURNSTILE_NOT_CONFIGURED") {
    return "人机验证尚未完成服务器配置，请联系管理员。";
  }
  if (cause.code === "SUBJECT_RATE_LIMITED") {
    return `本设备 30 分钟额度已用完，请在 ${formatWait(cause.retryAfter ?? 0)} 后再试。`;
  }
  if (cause.code === "SUBJECT_DAILY_LIMITED") {
    return "本设备今日试用额度已用完，明天再来即可。";
  }
  if (cause.code === "SUBJECT_QUEUE_FULL") {
    return cause.retryAfter && cause.retryAfter > 0
      ? `本设备已有任务在运行或排队，请等待约 ${formatWait(cause.retryAfter)} 后再试。`
      : "本设备已有任务在运行或排队，请等待当前任务完成。";
  }
  if (cause.code === "GLOBAL_QUEUE_FULL") {
    return cause.retryAfter && cause.retryAfter > 0
      ? `公共队列已满，请约 ${formatWait(cause.retryAfter)} 后再试。`
      : "公共队列已满，请稍后再试。";
  }
  if (cause.code === "PUBLIC_AGENT_PAUSED") {
    return "公众提问暂时暂停，管理员仍可继续测试。";
  }
  if (cause.code === "AGENT_DAILY_TASK_BUDGET_EXHAUSTED") {
    return "今日公共试用额度已用完，请明天再试。";
  }
  if (cause.retryAfter && cause.retryAfter > 0) {
    return `${cause.message}（约 ${formatWait(cause.retryAfter)} 后重试）`;
  }
  return cause.message;
}

function describeTaskFailure(errorCode: string | null | undefined, fallback?: string): string {
  switch (errorCode) {
    case "AGENT_MODEL_BUDGET_EXHAUSTED":
      return "今日 Agent 模型调用额度已用完，请明天再试。";
    case "QUEUE_TIMEOUT":
      return "公共队列等待超时，请稍后重新提交。";
    case "SERVICE_RESTARTED":
      return "服务重启中断了上次任务，可以重新提交原问题。";
    case "CANCELED_BY_USER":
      return "调查已取消。";
    default:
      return fallback ?? "本次调查没有完成，可以保留原问题后重试。";
  }
}

function useCampusAgentController() {
  const [conversationId, setConversationId] = useState<string>();
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [stage, setStage] = useState<Stage>("idle");
  const [statusText, setStatusText] = useState("可以直接说你真正担心的事");
  const [queuePosition, setQueuePosition] = useState(0);
  const [waitSeconds, setWaitSeconds] = useState(0);
  const [liveEvidence, setLiveEvidence] = useState<Evidence[]>([]);
  const [activeEvidence, setActiveEvidence] = useState<Evidence | null>(null);
  const [plan, setPlan] = useState<PlanPreview | null>(null);
  const [traceActivities, setTraceActivities] = useState<TraceActivity[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [agentAccess, setAgentAccess] = useState<AgentAccess | null>(null);
  const [verificationQuestion, setVerificationQuestion] = useState<string>();
  const [profile, setProfile] = useState<StudentProfile | null>(null);
  const [todos, setTodos] = useState<UserTodo[]>([]);
  const [currentTaskId, setCurrentTaskId] = useState<string>();
  const [failedTaskId, setFailedTaskId] = useState<string>();
  const [authBusy, setAuthBusy] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [spaceOpen, setSpaceOpen] = useState(false);
  const [mergePrompt, setMergePrompt] = useState(false);
  const [feedbackState, setFeedbackState] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [copiedTrace, setCopiedTrace] = useState<string>();
  const eventSourceRef = useRef<EventSource | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const dialogueEndRef = useRef<HTMLDivElement | null>(null);
  const recoveryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const traceSequenceRef = useRef(0);
  const traceStartedAtRef = useRef(Date.now());
  const workStartedAtRef = useRef<number | null>(null);
  const shouldFollowDialogueRef = useRef(true);

  const working = ["queued", "understanding", "planning", "investigating", "composing"].includes(
    stage,
  );

  const pushTrace = useCallback(
    (nextStage: Stage, label: string, detail?: string) => {
      traceSequenceRef.current += 1;
      const activity: TraceActivity = {
        id: `trace-${traceSequenceRef.current}`,
        stage: nextStage,
        label,
        detail,
        createdAt: Date.now(),
      };
      setTraceActivities((current) => {
        const previous = current[current.length - 1];
        if (previous?.label === label && previous?.detail === detail) return current;
        return [...current, activity].slice(-6);
      });
    },
    [],
  );

  const refreshHistory = useCallback(async () => {
    const result = await listConversations();
    setConversations(result.items);
    return result.items;
  }, []);

  const refreshTodos = useCallback(async () => {
    setTodos(await getTodos());
  }, []);

  const refreshAgentAccess = useCallback(async () => {
    const next = await getAgentAccess();
    setAgentAccess(next);
    return next;
  }, []);

  const finishTaskWithAnswer = useCallback(
    async (answer: AgentAnswer) => {
      const normalizedAnswer = {
        ...answer,
        evidence: normalizeEvidenceList(answer.evidence),
      };
      setMessages((current) => {
        if (
          current.some(
            (item) => item.role === "assistant" && item.id === normalizedAnswer.answer_id,
          )
        ) {
          return current;
        }
        return [
          ...current,
          {
            id: normalizedAnswer.answer_id,
            role: "assistant" as const,
            answer: normalizedAnswer,
            createdAt: normalizedAnswer.created_at,
          },
        ];
      });
      setLiveEvidence(normalizedAnswer.evidence);
      setActiveEvidence(normalizedAnswer.evidence[0] ?? null);
      setStage("completed");
      workStartedAtRef.current = null;
      setCurrentTaskId(undefined);
      setFailedTaskId(undefined);
      setStatusText(
        normalizedAnswer.verification_mode === "live_verified"
          ? "回答已通过实时官方材料核验"
          : "回答已完成，并标注了证据边界",
      );
      await Promise.all([refreshHistory(), getProfile().then(setProfile)]);
    },
    [refreshHistory],
  );

  const recoverTask = useCallback(
    async (taskId: string) => {
      try {
        const task = await getTask(taskId);
        setQueuePosition(task.queue_position);
        if (task.status === "completed" && task.answer_id) {
          await finishTaskWithAnswer(await getAnswer(task.answer_id));
        } else if (task.status === "failed" || task.status === "canceled") {
          setStage("failed");
          workStartedAtRef.current = null;
          setCurrentTaskId(undefined);
           setFailedTaskId(taskId);
           setStatusText(
             task.error_code === "SERVICE_RESTARTED"
               ? "服务重启中断了上次任务"
               : "本次调查没有完成",
           );
           setError(describeTaskFailure(task.error_code));
        }
      } catch {
        setStatusText("正在恢复任务连接");
      }
    },
    [finishTaskWithAnswer],
  );

  const openTaskStream = useCallback(
    (path: string, taskId: string, initialQueue = 0, taskStartedAt?: number) => {
      eventSourceRef.current?.close();
      if (recoveryTimerRef.current) clearTimeout(recoveryTimerRef.current);
      setCurrentTaskId(taskId);
      setFailedTaskId(undefined);
      setQueuePosition(initialQueue);
      const startedAt = workStartedAtRef.current ?? taskStartedAt ?? Date.now();
      workStartedAtRef.current = startedAt;
      setWaitSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
      traceSequenceRef.current = 0;
      traceStartedAtRef.current = startedAt;
      setTraceActivities([
        {
          id: "trace-0",
          stage: initialQueue > 0 ? "queued" : "understanding",
          label: initialQueue > 0 ? "任务已进入队列" : "正在建立任务通道",
          detail: initialQueue > 0 ? `前方还有 ${initialQueue} 个任务` : "正在连接任务事件流",
          createdAt: Date.now(),
        },
      ]);
      if (initialQueue > 0) {
        setStage("queued");
        setStatusText(`前面还有 ${initialQueue} 个任务`);
      }
      const source = new EventSource(streamUrl(path), { withCredentials: true });
      eventSourceRef.current = source;
      let terminated = false;
      let connectionInterrupted = false;

      source.onopen = () => {
        if (recoveryTimerRef.current) {
          clearTimeout(recoveryTimerRef.current);
          recoveryTimerRef.current = null;
        }
        setStatusText(
          connectionInterrupted
            ? "任务通道已恢复，正在等待下一条真实进度"
            : initialQueue > 0
              ? `前面还有 ${initialQueue} 个任务`
              : "任务通道已连接，等待模型开始处理",
        );
        connectionInterrupted = false;
      };

      source.addEventListener("task.accepted", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          queue_position: number;
        };
        setQueuePosition(data.queue_position);
        if (data.queue_position > 0) {
          setStage("queued");
          setStatusText(`前面还有 ${data.queue_position} 个任务`);
          pushTrace(
            "queued",
            "任务正在排队",
            `当前队列位置 ${data.queue_position}`,
          );
        }
      });

      source.addEventListener("thinking.started", () => {
        setStage("understanding");
        setQueuePosition(0);
        setStatusText("正在结合问题、对话与已确认画像理解真实需求");
        pushTrace("understanding", "开始理解问题", "正在确认语境、对象与时间范围");
      });
      source.addEventListener("perception.completed", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          goals: Array<{ goal: string; confidence: number }>;
        };
        setStage("planning");
        setStatusText(data.goals[0]?.goal ?? "已理解问题，正在规划调查");
        pushTrace(
          "planning",
          "调查目标已确认",
          data.goals[0]?.goal ?? "准备建立调查路径",
        );
      });
      source.addEventListener("plan.created", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as PlanPreview;
        setPlan(data);
        setStage("planning");
        setStatusText(data.objective);
        pushTrace(
          "planning",
          "调查路径已建立",
          `${data.steps.length} 个只读步骤准备执行`,
        );
      });
      source.addEventListener("tool.started", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as { purpose: string };
        setStage("investigating");
        setStatusText(data.purpose);
        pushTrace("investigating", "开始读取校园资料", data.purpose);
      });
      source.addEventListener("tool.completed", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          evidence?: unknown;
          warnings?: unknown;
        };
        const newEvidence = normalizeEvidenceList(data.evidence);
        const warnings = Array.isArray(data.warnings)
          ? data.warnings.filter((item): item is string => typeof item === "string")
          : [];
        if (newEvidence.length) {
          setLiveEvidence((current) => {
            const seen = new Set(current.map((item) => item.canonical_url));
            return [...current, ...newEvidence.filter((item) => !seen.has(item.canonical_url))];
          });
          setActiveEvidence((current) => current ?? newEvidence[0]);
        }
        setStatusText(
          newEvidence.length
            ? `已取得 ${newEvidence.length} 条新的官方材料`
            : warnings[0] ?? "本轮检索没有取得新证据",
        );
        pushTrace(
          "investigating",
          newEvidence.length ? `新增 ${newEvidence.length} 条证据` : "本轮读取已结束",
          newEvidence[0]?.title ?? warnings[0] ?? "正在判断是否需要继续调查",
        );
      });
      source.addEventListener("investigation.round.started", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          round: number;
          step_count: number;
        };
        setStage("investigating");
        setStatusText(`第 ${data.round} 轮调查将处理 ${data.step_count} 项资料`);
        pushTrace(
          "investigating",
          `第 ${data.round} 轮调查开始`,
          `${data.step_count} 个只读步骤将依次或并行执行`,
        );
      });
      source.addEventListener("investigation.round.completed", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          round: number;
          tool_calls: number;
          evidence_count: number;
        };
        pushTrace(
          "investigating",
          `第 ${data.round} 轮调查完成`,
          `${data.tool_calls} 次读取 · 累计 ${data.evidence_count} 条证据`,
        );
      });
      source.addEventListener("answer.composing", () => {
        setStage("composing");
        setStatusText("正在区分官方事实、分析建议与不确定性");
        pushTrace(
          "composing",
          "开始组织回答",
          "基于已取得的证据生成可追溯答案",
        );
      });
      source.addEventListener("evidence.assessed", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          status: string;
          can_answer: boolean;
          summary?: string;
        };
        const fullySupported = data.status === "sufficient";
        setStage(data.can_answer ? "composing" : "investigating");
        setStatusText(
          fullySupported
            ? "现有证据可以支持回答，正在继续成稿"
            : data.can_answer
              ? "已确认现有材料的边界，正在组织一份保守回答"
              : "证据仍有缺口，正在决定下一步",
        );
        pushTrace(
          data.can_answer ? "composing" : "investigating",
          fullySupported
            ? "证据支持检查通过"
            : data.can_answer
              ? "证据边界已经确认"
              : "发现证据缺口",
          fullySupported
            ? "现有材料已经覆盖主要结论"
            : data.can_answer
              ? "回答会明确标注当前材料的适用与证据边界"
              : "正在判断是否调整调查路径或明确证据限制",
        );
      });
      source.addEventListener("answer.verification.started", () => {
        setStage("composing");
        setStatusText("正在独立复核关键结论与引用关系");
        pushTrace("composing", "开始独立复核", "检查关键结论是否得到材料支持");
      });
      source.addEventListener("answer.verification.completed", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          verdict: string;
          summary?: string;
        };
        pushTrace(
          "composing",
          data.verdict === "passed" ? "答案复核通过" : "答案正在按复核结果调整",
          data.verdict === "passed" ? "关键结论与引用关系已确认" : "正在修正证据边界",
        );
      });
      source.addEventListener("answer.citation_repair.started", () => {
        setStatusText("正在校准答案与校园原文的引用对应");
        pushTrace("composing", "开始校准引用", "逐项核对结论与证据定位");
      });
      source.addEventListener("answer.citation_repair.completed", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as { passed: boolean };
        pushTrace(
          "composing",
          data.passed ? "引用校准完成" : "引用校准未通过",
          data.passed ? "即将交付最终回答" : "将采用更保守的回答并明确证据限制",
        );
      });
      source.addEventListener("answer.completed", (event) => {
        terminated = true;
        const answer = JSON.parse((event as MessageEvent).data) as AgentAnswer;
        void finishTaskWithAnswer(answer);
        source.close();
      });
      source.addEventListener("task.failed", (event) => {
        const data = JSON.parse((event as MessageEvent).data) as {
          message?: string;
          error_code?: string;
        };
        terminated = true;
        setStage("failed");
        workStartedAtRef.current = null;
        setCurrentTaskId(undefined);
        setFailedTaskId(taskId);
        setStatusText(
          data.error_code === "CANCELED_BY_USER" ? "调查已取消" : "本次调查中断",
        );
        setError(describeTaskFailure(data.error_code, data.message));
        source.close();
      });
      source.onerror = () => {
        if (!terminated) {
          connectionInterrupted = true;
          setStatusText("事件连接中断，正在自动恢复任务状态");
          recoveryTimerRef.current = setTimeout(() => void recoverTask(taskId), 1200);
        }
      };
    },
    [finishTaskWithAnswer, pushTrace, recoverTask],
  );

  const loadConversation = useCallback(
    async (id: string) => {
      eventSourceRef.current?.close();
      setLoadingConversation(true);
      setError(null);
      setConversationId(id);
      setRailOpen(false);
      try {
        const detail = await getConversation(id);
        const answerTasks = detail.tasks.filter((task) => task.answer_id);
        const answers = await Promise.all(
          answerTasks.map((task) => getAnswer(task.answer_id as string)),
        );
        const restored: ChatMessage[] = [
          ...detail.messages
            .filter((message) => message.role === "user")
            .map((message) => ({
              id: message.message_id,
              role: "user" as const,
              content: message.content,
              createdAt: message.created_at,
            })),
          ...answers.map((answer) => ({
            id: answer.answer_id,
            role: "assistant" as const,
            answer,
            createdAt: answer.created_at,
          })),
        ].sort(
          (left, right) =>
            new Date(left.createdAt).getTime() - new Date(right.createdAt).getTime(),
        );
        setMessages(restored);
        const lastAnswer = [...answers].sort(
          (left, right) =>
            new Date(right.created_at).getTime() - new Date(left.created_at).getTime(),
        )[0];
        setLiveEvidence(lastAnswer?.evidence ?? []);
        setActiveEvidence(lastAnswer?.evidence[0] ?? null);
        const activeTask = [...detail.tasks]
          .reverse()
          .find((task) => ["queued", "running"].includes(task.status));
        const failedTask = [...detail.tasks]
          .reverse()
          .find((task) => ["failed", "canceled"].includes(task.status));
        if (activeTask) {
          setStage(activeTask.status === "queued" ? "queued" : "investigating");
          const activeStatus = await getTask(activeTask.task_id).catch(() => null);
          openTaskStream(
            `/api/v1/tasks/${activeTask.task_id}/events`,
            activeTask.task_id,
            activeStatus?.queue_position ?? 0,
            new Date(activeTask.created_at).getTime(),
          );
        } else if (failedTask && !lastAnswer) {
          setStage("failed");
          setFailedTaskId(failedTask.task_id);
          setStatusText(
            failedTask.error_code === "SERVICE_RESTARTED"
              ? "服务重启中断了上次任务"
              : "上次调查未完成",
          );
        } else {
          setStage(lastAnswer ? "completed" : "idle");
          setStatusText(lastAnswer ? "已恢复历史回答" : "开始这段新对话");
        }
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "历史会话恢复失败");
      } finally {
        setLoadingConversation(false);
      }
    },
    [openTaskStream],
  );

  useEffect(() => {
    let cancelled = false;
    async function boot() {
      try {
        const session = await getAuthSession();
        if (cancelled) return;
        setAuthSession(session);
        setMergePrompt(session.authenticated && session.visitor_data_available);
        const [nextHealth, nextProfile, nextTodos, nextAccess] = await Promise.all([
          getHealth(),
          getProfile(),
          getTodos(),
          getAgentAccess(),
          refreshHistory(),
        ]);
        if (cancelled) return;
        setHealth(nextHealth);
        setProfile(nextProfile);
        setTodos(nextTodos);
        setAgentAccess(nextAccess);
      } catch {
        if (!cancelled) setError("身份与 Agent 服务暂时无法连接。");
      }
    }
    void boot();

    const parameters = new URLSearchParams(window.location.search);
    const authError = parameters.get("auth_error");
    if (authError) setError("校园登录未完成，请重新从本页发起。");
    if (parameters.has("auth") || authError) {
      parameters.delete("auth");
      parameters.delete("auth_error");
      window.history.replaceState(
        {},
        "",
        `${window.location.pathname}${parameters.size ? `?${parameters}` : ""}`,
      );
    }
    return () => {
      cancelled = true;
      eventSourceRef.current?.close();
      if (recoveryTimerRef.current) clearTimeout(recoveryTimerRef.current);
    };
  }, [refreshHistory]);

  useEffect(() => {
    if (!working) return;
    const updateElapsed = () => {
      const startedAt = workStartedAtRef.current;
      if (startedAt !== null) {
        setWaitSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
      }
    };
    updateElapsed();
    const timer = setInterval(updateElapsed, 1000);
    return () => clearInterval(timer);
  }, [working]);

  useEffect(() => {
    if (shouldFollowDialogueRef.current) {
      dialogueEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [messages, statusText, traceActivities]);

  const quickQuestions = useMemo(() => {
    const goal = profile?.confirmed.find((item) => item.attribute_key === "goal");
    return goal
      ? [
          `我以${goal.attribute_value}为目标，现在最该留意哪些校园机会？`,
          ...DEFAULT_QUESTIONS.slice(0, 2),
        ]
      : DEFAULT_QUESTIONS;
  }, [profile]);

  async function handleLogout() {
    if (authBusy) return;
    setAuthBusy(true);
    try {
      await logoutCampusSession();
      const session = await getAuthSession();
      setAuthSession(session);
      setMessages([]);
      setConversationId(undefined);
      await Promise.all([
        refreshHistory(),
        getProfile().then(setProfile),
        refreshTodos(),
        refreshAgentAccess(),
      ]);
      setStage("idle");
      setStatusText("已退出校园身份，当前只使用公开信源");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "退出登录失败");
    } finally {
      setAuthBusy(false);
    }
  }

  async function handleAuthenticated() {
    eventSourceRef.current?.close();
    const session = await getAuthSession();
    setAuthSession(session);
    setMergePrompt(session.visitor_data_available);
    setConversationId(undefined);
    setMessages([]);
    await Promise.all([
      refreshHistory(),
      getProfile().then(setProfile),
      refreshTodos(),
      refreshAgentAccess(),
    ]);
    setStage("idle");
    setStatusText("校园身份已验证，可以查询授权信源");
  }

  function newConversation() {
    eventSourceRef.current?.close();
    setConversationId(undefined);
    setMessages([]);
    setLiveEvidence([]);
    setActiveEvidence(null);
    setPlan(null);
    setTraceActivities([]);
    workStartedAtRef.current = null;
    setWaitSeconds(0);
    setError(null);
    setStage("idle");
    setStatusText("开始一段新对话");
    setRailOpen(false);
  }

  async function submitQuestion(question: string) {
    const normalized = question.trim();
    if (!normalized || working) return;
    if (authSession?.auth_mode === "required_cas" && !authSession.authenticated) {
      setError("请先通过学校统一身份认证，再开始提问。");
      return;
    }
    setError(null);
    let access: AgentAccess;
    try {
      access = await refreshAgentAccess();
    } catch (cause) {
      setStage("idle");
      setStatusText("暂时无法确认试用准入状态");
      setError(describeAgentError(cause));
      return;
    }
    // A paused public gate must not prevent the local administrator from
    // exercising the Agent from the same UI.  The backend still applies the
    // global queue and model budgets to that request; this only mirrors the
    // documented admin bypass for the public pause/anonymous quotas.
    if (access.mode === "paused" && authSession?.role !== "admin") {
      setStage("idle");
      setStatusText("公众提问暂时暂停");
      setError("公众提问暂时暂停，管理员仍可继续测试。");
      return;
    }
    if (access.verification_required) {
      if (!access.turnstile_site_key) {
        setStage("idle");
        setStatusText("人机验证尚未完成配置");
        setError("人机验证尚未完成服务器配置，请联系管理员。");
        return;
      }
      setVerificationQuestion(normalized);
      setStage("idle");
      setStatusText("请完成一次人机验证，验证成功后自动继续");
      return;
    }
    if (access.mode === "enforce" && access.window_remaining === 0) {
      setStage("idle");
      setStatusText("本设备的滚动窗口额度已用完");
      setError(`请在 ${formatResetAt(access.window_reset_at)} 后再试。`);
      return;
    }
    if (access.mode === "enforce" && access.daily_remaining === 0) {
      setStage("idle");
      setStatusText("本设备今日额度已用完");
      setError(`明天 ${formatResetAt(access.daily_reset_at)} 后可继续试用。`);
      return;
    }
    setInput("");
    setStage("understanding");
    setStatusText("正在保留原意并理解你真正需要解决的事");
    setPlan(null);
    setLiveEvidence([]);
    setActiveEvidence(null);
    setTraceActivities([]);
    workStartedAtRef.current = Date.now();
    shouldFollowDialogueRef.current = true;
    const optimisticId = crypto.randomUUID();
    setMessages((current) => [
      ...current,
      {
        id: optimisticId,
        role: "user",
        content: normalized,
        createdAt: new Date().toISOString(),
      },
    ]);
    try {
      const id = conversationId ?? (await createConversation());
      setConversationId(id);
      const task = await sendMessage(id, normalized, optimisticId);
      openTaskStream(task.stream_url, task.task_id, task.queue_position);
      await refreshHistory();
      void refreshAgentAccess();
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "HUMAN_VERIFICATION_REQUIRED") {
        setMessages((current) => current.filter((item) => item.id !== optimisticId));
        setVerificationQuestion(normalized);
        setStage("idle");
        setStatusText("请完成一次人机验证，验证成功后自动继续");
        setError(describeAgentError(cause));
        return;
      }
      setMessages((current) => current.filter((item) => item.id !== optimisticId));
      setStage("idle");
      workStartedAtRef.current = null;
      setStatusText("任务没有成功提交");
      setError(describeAgentError(cause));
      void refreshAgentAccess();
    }
  }

  async function handleCancel() {
    if (!currentTaskId) return;
    try {
      await cancelTask(currentTaskId);
      setStage("failed");
      workStartedAtRef.current = null;
      setFailedTaskId(currentTaskId);
      setCurrentTaskId(undefined);
      setStatusText("调查已取消");
      void refreshAgentAccess();
    } catch (cause) {
      setError(describeAgentError(cause));
    }
  }

  async function handleRetry() {
    if (!failedTaskId) return;
    setError(null);
    try {
      workStartedAtRef.current = Date.now();
      shouldFollowDialogueRef.current = true;
      const task = await retryTask(failedTaskId);
      openTaskStream(task.stream_url, task.task_id, task.queue_position);
      void refreshAgentAccess();
    } catch (cause) {
      workStartedAtRef.current = null;
      setStage("idle");
      setStatusText("重试没有成功提交");
      setError(describeAgentError(cause));
    }
  }

  async function handleReverify(answerId: string) {
    setError(null);
    try {
      workStartedAtRef.current = Date.now();
      shouldFollowDialogueRef.current = true;
      const task = await reverifyAnswer(answerId);
      openTaskStream(task.stream_url, task.task_id, task.queue_position);
      void refreshAgentAccess();
    } catch (cause) {
      workStartedAtRef.current = null;
      setStage("idle");
      setStatusText("实时核验没有成功提交");
      setError(describeAgentError(cause));
    }
  }

  async function saveAction(answer: AgentAnswer, action: string, index: number) {
    await createTodo({
      title: action,
      source_answer_id: answer.answer_id,
      source_action_index: index,
    });
    await refreshTodos();
    setSpaceOpen(true);
  }

  async function feedback(
    answerId: string,
    rating: "helpful" | "not_helpful" | "incorrect" | "outdated",
  ) {
    await putFeedback(answerId, { rating });
    setFeedbackState((current) => ({ ...current, [answerId]: rating }));
  }

  async function mergeIdentity() {
    await mergeVisitorData();
    setMergePrompt(false);
    const session = await getAuthSession();
    setAuthSession(session);
    await Promise.all([
      refreshHistory(),
      getProfile().then(setProfile),
      refreshTodos(),
    ]);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void submitQuestion(input);
  }

  async function copyTraceId(value: string) {
    const fallbackCopy = () => {
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.append(textarea);
      try {
        textarea.select();
        return document.execCommand("copy");
      } finally {
        textarea.remove();
      }
    };
    try {
      let copied = false;
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(value);
          copied = true;
        } catch {
          copied = fallbackCopy();
        }
      } else {
        copied = fallbackCopy();
      }
      if (!copied) throw new Error("copy failed");
      setCopiedTrace(value);
      window.setTimeout(() => {
        setCopiedTrace((current) => (current === value ? undefined : current));
      }, 1600);
    } catch {
      setError(`无法自动复制，请手动记录：${value}`);
    }
  }

  async function completeAgentVerification() {
    const pending = verificationQuestion;
    try {
      await refreshAgentAccess();
      setVerificationQuestion(undefined);
      if (pending) await submitQuestion(pending);
    } catch (cause) {
      setError(describeAgentError(cause));
    }
  }

  function handlePersonalDataDeleted() {
    eventSourceRef.current?.close();
    if (recoveryTimerRef.current) clearTimeout(recoveryTimerRef.current);
    setProfile(null);
    setTodos([]);
    setMessages([]);
    setConversationId(undefined);
    setConversations([]);
    setLiveEvidence([]);
    setActiveEvidence(null);
    setPlan(null);
    setTraceActivities([]);
    setCurrentTaskId(undefined);
    setFailedTaskId(undefined);
    setVerificationQuestion(undefined);
    setFeedbackState({});
    workStartedAtRef.current = null;
    setWaitSeconds(0);
    setError(null);
    setStage("idle");
    setStatusText("个人数据已删除，工作区已清空；安全额度按原窗口继续计算");
    void getProfile().then(setProfile).catch(() => {
      setError("个人数据已删除，但初始化新画像失败，请刷新页面。");
    });
    void refreshAgentAccess();
  }

  return {
    conversationId,
    conversations,
    messages,
    input,
    stage,
    statusText,
    queuePosition,
    waitSeconds,
    liveEvidence,
    activeEvidence,
    plan,
    traceActivities,
    health,
    authSession,
    agentAccess,
    verificationQuestion,
    profile,
    todos,
    currentTaskId,
    failedTaskId,
    authBusy,
    loadingConversation,
    railOpen,
    evidenceOpen,
    spaceOpen,
    mergePrompt,
    feedbackState,
    error,
    copiedTrace,
    messageListRef,
    dialogueEndRef,
    traceStartedAtRef,
    shouldFollowDialogueRef,
    working,
    quickQuestions,
    setInput,
    setRailOpen,
    setEvidenceOpen,
    setSpaceOpen,
    setActiveEvidence,
    setProfile,
    setMergePrompt,
    setError,
    refreshTodos,
    handleLogout,
    handleAuthenticated,
    newConversation,
    submitQuestion,
    loadConversation,
    mergeIdentity,
    handleCancel,
    completeAgentVerification,
    handleRetry,
    handleReverify,
    saveAction,
    feedback,
    onSubmit,
    copyTraceId,
    handlePersonalDataDeleted,
  };
}

export function CampusAgent() {
  const { theme } = useTheme();
  const {
    conversationId,
    conversations,
    messages,
    input,
    stage,
    statusText,
    queuePosition,
    waitSeconds,
    liveEvidence,
    activeEvidence,
    plan,
    traceActivities,
    health,
    authSession,
    agentAccess,
    verificationQuestion,
    profile,
    todos,
    currentTaskId,
    failedTaskId,
    authBusy,
    loadingConversation,
    railOpen,
    evidenceOpen,
    spaceOpen,
    mergePrompt,
    feedbackState,
    error,
    copiedTrace,
    messageListRef,
    dialogueEndRef,
    traceStartedAtRef,
    shouldFollowDialogueRef,
    working,
    quickQuestions,
    setInput,
    setRailOpen,
    setEvidenceOpen,
    setSpaceOpen,
    setActiveEvidence,
    setProfile,
    setMergePrompt,
    setError,
    refreshTodos,
    handleLogout,
    handleAuthenticated,
    newConversation,
    submitQuestion,
    loadConversation,
    mergeIdentity,
    handleCancel,
    completeAgentVerification,
    handleRetry,
    handleReverify,
    saveAction,
    feedback,
    onSubmit,
    copyTraceId,
    handlePersonalDataDeleted,
  } = useCampusAgentController();

  if (theme === "character") {
    return (
      <>
        <AgentAccessPanel
          access={agentAccess}
          pendingQuestion={verificationQuestion}
          onVerified={completeAgentVerification}
        />
        <CongyuAgentView
          conversationId={conversationId}
          conversations={conversations}
          messages={messages}
          input={input}
          stage={stage}
          statusText={statusText}
          queuePosition={queuePosition}
          waitSeconds={waitSeconds}
          liveEvidence={liveEvidence}
          activeEvidence={activeEvidence}
          plan={plan}
          traceActivities={traceActivities}
          traceStartedAt={traceStartedAtRef.current}
          health={health}
          authSession={authSession}
          authBusy={authBusy}
          profile={profile}
          todos={todos}
          currentTaskId={currentTaskId}
          failedTaskId={failedTaskId}
          loadingConversation={loadingConversation}
          railOpen={railOpen}
          evidenceOpen={evidenceOpen}
          spaceOpen={spaceOpen}
          mergePrompt={mergePrompt}
          feedbackState={feedbackState}
          error={error}
          copiedTrace={copiedTrace}
          working={working}
          quickQuestions={quickQuestions}
          messageListRef={messageListRef}
          dialogueEndRef={dialogueEndRef}
          onInput={setInput}
          onSubmit={onSubmit}
          onQuestion={(question) => void submitQuestion(question)}
          onNew={newConversation}
          onLoadConversation={(id) => void loadConversation(id)}
          onRailOpen={setRailOpen}
          onEvidenceOpen={setEvidenceOpen}
          onSpaceOpen={setSpaceOpen}
          onEvidence={setActiveEvidence}
          onLogout={() => void handleLogout()}
          onAuthenticated={() => void handleAuthenticated()}
          onMerge={() => void mergeIdentity()}
          onDismissMerge={() => setMergePrompt(false)}
          onCancel={() => void handleCancel()}
          onRetry={() => void handleRetry()}
          onReverify={(answerId) => void handleReverify(answerId)}
          onSaveAction={(answer, action, index) => void saveAction(answer, action, index)}
          onFeedback={(answerId, rating) => void feedback(answerId, rating)}
          onConfirmSuggestion={(attributeId) => {
            void resolveProfileSuggestion(attributeId, "confirm").then(async () => {
              setProfile(await getProfile());
            });
          }}
          onProfile={setProfile}
          onTodosChanged={refreshTodos}
          onPersonalDataDeleted={handlePersonalDataDeleted}
          onError={setError}
          onCopyTrace={(value) => void copyTraceId(value)}
          onDialogueScroll={(following) => {
            shouldFollowDialogueRef.current = following;
          }}
        />
      </>
    );
  }

  return (
    <AppChrome
      section="agent"
      className="app-shell stage6-shell"
      channel="HZCU // CAMPUS SIGNAL"
      mode="PILOT ≤ 50 / READ ONLY"
      eyebrow="06 / SEMANTIC TERMINAL"
      title="问答工作区"
      mobileAction={
        <button
          className="mobile-rail-trigger"
          type="button"
          onClick={() => {
            setEvidenceOpen(false);
            setRailOpen(true);
          }}
          aria-label="打开会话历史"
        >
          <Menu size={20} />
        </button>
      }
      utilities={null}
    >

      <AgentAccessPanel
        access={agentAccess}
        pendingQuestion={verificationQuestion}
        onVerified={completeAgentVerification}
      />

      {mergePrompt && (
        <div className="merge-banner">
          <History size={17} />
          <span>
            <b>这台设备上有登录前的内容</b>
            是否把会话、待办和反馈合并到校园账号？
          </span>
          <button type="button" onClick={() => void mergeIdentity()}>
            合并
          </button>
          <button type="button" onClick={() => setMergePrompt(false)}>
            暂不
          </button>
        </div>
      )}

      <section
        className={`workspace task-stage-${stage} ${working ? "is-working" : ""}`}
      >
        <ConversationRail
          conversations={conversations}
          selectedId={conversationId}
          session={authSession}
          channelOnline={health?.status === "ok"}
          channelDetail={
            health?.model_provider === "demo"
              ? "当前为演示模式"
              : "校园问答服务已连接"
          }
          identityControl={
            <IdentityControl
              session={authSession}
              busy={authBusy}
              onLogout={() => void handleLogout()}
              onAuthenticated={() => void handleAuthenticated()}
            />
          }
          open={railOpen}
          onClose={() => setRailOpen(false)}
          onNew={newConversation}
          onSelect={(id) => void loadConversation(id)}
          onOpenSpace={() => {
            setRailOpen(false);
            setEvidenceOpen(false);
            setSpaceOpen(true);
          }}
        />

        <section className="dialogue" aria-label="与校园 Agent 对话">
          {conversationId && (
            <div className="conversation-trace-strip">
              <code title={conversationId}>
                <span className="trace-id-short">
                  会话 · {conversationId.slice(5, 17).toUpperCase()}
                </span>
              </code>
              <button
                type="button"
                onClick={() => void copyTraceId(conversationId)}
                aria-label="复制完整会话追溯 ID"
              >
                {copiedTrace === conversationId ? <Check size={14} /> : <Copy size={14} />}
                {copiedTrace === conversationId ? "已复制" : "复制"}
              </button>
            </div>
          )}
          {messages.length === 0 && (
            <>
              <div className="hero-copy">
                <div className="hero-register">
                  <p className="eyebrow">ASK WHAT YOU ACTUALLY MEAN</p>
                  <span>SEMANTIC CHANNEL / OPEN</span>
                </div>
                <div className="hero-wordmark" aria-hidden="true">
                  <span>HZCU CAMPUS<br />KNOWLEDGE FIELD</span>
                  <span>06</span>
                  <div className="hero-signal-map">
                    <i />
                    <i />
                    <i />
                    <i />
                    <b>OFFICIAL SOURCE NETWORK</b>
                  </div>
                </div>
                <h1>
                  <span>HZCU AGENT</span>
                  <em>城知</em>
                </h1>
                <p>
                  我是城知，专门帮你查清城院里的各种问题。课程、校历、竞赛、办事流程……
                  直接说你想知道什么就好；需要核实的地方，我会找到学校的官方材料，并把出处一起给你。
                </p>
                <div className="hero-method" aria-hidden="true">
                  <span><b>01</b>理解语境</span>
                  <span><b>02</b>翻阅材料</span>
                  <span><b>03</b>核验证据</span>
                </div>
                <div className="hero-index" aria-hidden="true">
                  <b>
                    {Math.max(conversations.length, 1)
                      .toString()
                      .padStart(2, "0")}
                  </b>
                  <span>
                    ACTIVE
                    <br />
                    CHANNEL
                  </span>
                </div>
              </div>
              <div className="question-prompts" aria-label="快速问题">
                {quickQuestions.map((question, index) => (
                  <button
                    type="button"
                    key={question}
                    onClick={() => void submitQuestion(question)}
                  >
                    <span>0{index + 1}</span>
                    {question}
                    <ChevronRight size={16} />
                  </button>
                ))}
              </div>
            </>
          )}

          <div
            ref={messageListRef}
            className="message-list"
            aria-label="对话记录"
            onScroll={(event) => {
              const target = event.currentTarget;
              shouldFollowDialogueRef.current =
                target.scrollHeight - target.scrollTop - target.clientHeight < 120;
            }}
          >
            {loadingConversation && (
              <div className="history-loading">
                <LoaderCircle className="spin" size={18} /> 正在恢复完整会话
              </div>
            )}
            {messages.map((message) =>
              message.role === "user" ? (
                <article className="user-message" key={message.id}>
                  <span>你</span>
                  <p>{message.content}</p>
                </article>
              ) : (
                <article className="agent-message" key={message.id}>
                  <div className="answer-kicker">
                    <span>城知</span>
                    <div>
                      <i>
                        <Check size={12} />
                        {message.answer.verification_mode === "live_verified"
                          ? "实时"
                          : message.answer.verification_mode === "historical"
                            ? "历史"
                            : message.answer.verification_mode === "degraded"
                              ? "降级"
                              : "缓存"}
                      </i>
                      {message.answer.grounding?.citation_coverage === 1 && (
                        <i>
                          <Check size={12} /> 引用完整
                        </i>
                      )}
                      <small>置信度 {message.answer.confidence}</small>
                    </div>
                  </div>
                  <h3>{message.answer.headline}</h3>
                  <div className="answer-strata">
                    <span>
                      官方事实{" "}
                      {
                        message.answer.claims.filter(
                          (claim) => claim.statement_type === "campus_fact",
                        ).length
                      }
                    </span>
                    <span>
                      分析 / 建议{" "}
                      {
                        message.answer.claims.filter(
                          (claim) => claim.statement_type !== "campus_fact",
                        ).length
                      }
                    </span>
                    {message.answer.assumptions.length > 0 && (
                      <span>含 {message.answer.assumptions.length} 项假设</span>
                    )}
                  </div>
                  <div className="markdown">
                    <ReactMarkdown
                      components={{
                        a: ({ href, children }) => (
                          <a
                            href={href}
                            onClick={() => {
                              const match = href?.match(/^#evidence-(\d+)$/);
                              if (match) {
                                const item =
                                  message.answer.evidence[Number(match[1]) - 1];
                                if (item) {
                                  setActiveEvidence(item);
                                  setRailOpen(false);
                                  setEvidenceOpen(true);
                                }
                              }
                            }}
                            target={href?.startsWith("http") ? "_blank" : undefined}
                            rel={href?.startsWith("http") ? "noreferrer" : undefined}
                          >
                            {children}
                          </a>
                        ),
                      }}
                    >
                      {withCitationLinks(message.answer.answer_markdown)}
                    </ReactMarkdown>
                  </div>
                  {message.answer.assumptions.length > 0 && (
                    <div className="answer-uncertainty">
                      <MessageCircleWarning size={15} />
                      <span>{message.answer.assumptions.join("；")}</span>
                    </div>
                  )}
                  {message.answer.next_actions.length > 0 && (
                    <div className="next-actions">
                      <p>接下来可以</p>
                      {message.answer.next_actions.map((action, index) => (
                        <button
                          type="button"
                          key={action}
                          onClick={() =>
                            void saveAction(message.answer, action, index)
                          }
                        >
                          <ListChecks size={13} /> 保存为待办 · {action}
                        </button>
                      ))}
                    </div>
                  )}
                  {message.answer.profile_suggestions?.length > 0 && (
                    <div className="inline-profile-suggestions">
                      <p>你刚刚明确提到的信息，可选择加入画像</p>
                      {message.answer.profile_suggestions.map((suggestion) => (
                        <span key={suggestion.attribute_id}>
                          <b>{suggestion.attribute_value}</b>
                          <button
                            type="button"
                            onClick={async () => {
                              await resolveProfileSuggestion(
                                suggestion.attribute_id,
                                "confirm",
                              );
                              setProfile(await getProfile());
                            }}
                          >
                            确认
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="answer-actions">
                    <span>这次回答</span>
                    <button
                      type="button"
                      className={
                        feedbackState[message.answer.answer_id] === "helpful"
                          ? "active"
                          : ""
                      }
                      onClick={() =>
                        void feedback(message.answer.answer_id, "helpful")
                      }
                    >
                      <ThumbsUp size={14} /> 有帮助
                    </button>
                    <button
                      type="button"
                      className={
                        feedbackState[message.answer.answer_id] === "not_helpful"
                          ? "active"
                          : ""
                      }
                      onClick={() =>
                        void feedback(message.answer.answer_id, "not_helpful")
                      }
                    >
                      <ThumbsDown size={14} /> 有问题
                    </button>
                    <button
                      type="button"
                      disabled={working}
                      onClick={() => void handleReverify(message.answer.answer_id)}
                    >
                      <RefreshCw size={14} /> 重新实时核验
                    </button>
                  </div>
                  <details className="answer-trace-ids">
                    <summary>查看追溯信息</summary>
                    <div aria-label="回答追溯标识">
                      {[
                        ["任务", message.answer.task_id],
                        ["回答", message.answer.answer_id],
                      ].map(([label, traceId]) => (
                        <button
                          type="button"
                          key={traceId}
                          title={traceId}
                          onClick={() => void copyTraceId(traceId)}
                        >
                          <code>{label} · {traceId.slice(5, 17).toUpperCase()}</code>
                          {copiedTrace === traceId ? <Check size={12} /> : <Copy size={12} />}
                        </button>
                      ))}
                    </div>
                  </details>
                </article>
              ),
            )}

            {working && (
              <section
                className={`live-trace trace-stage-${stage}`}
                aria-label="模型工作进度"
              >
                <p className="trace-live-status" role="status" aria-live="polite" aria-atomic="true">
                  {statusText}
                </p>
                <header className="trace-heading">
                  <span className="trace-orb" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  <span>
                    <small>LIVE INVESTIGATION</small>
                    <b key={stage}>{STAGE_LABELS[stage]}</b>
                  </span>
                  <div className="trace-clock">
                    <Clock3 size={13} />
                    <span>{formatWait(waitSeconds)}</span>
                    <i>持续处理中</i>
                  </div>
                </header>

                <div className="trace-phase-grid" aria-label="调查阶段">
                  {TRACE_PHASES.map((phase) => {
                    const active = phase.id === stage;
                    const done = stageOrder(stage) > stageOrder(phase.id);
                    return (
                      <div
                        key={phase.id}
                        className={active ? "active" : done ? "done" : "waiting"}
                      >
                        <span>{done ? <Check size={12} /> : phase.index}</span>
                        <b>{phase.label}</b>
                        <small>{active ? statusText : phase.description}</small>
                      </div>
                    );
                  })}
                </div>

                <div className="trace-current">
                  <div className="trace-activity-stream">
                    <div className="trace-section-label">
                      <span>实时进度</span>
                      <i>{traceActivities.length} 条更新</i>
                    </div>
                    <div className="trace-events">
                      {traceActivities.slice(-4).map((activity, index, visible) => (
                        <div
                          key={activity.id}
                          className={index === visible.length - 1 ? "current" : ""}
                        >
                          <span>
                            收到 +{Math.max(
                              0,
                              Math.floor(
                                (activity.createdAt - traceStartedAtRef.current) / 1000,
                              ),
                            )}
                            s
                          </span>
                          <p>
                            <b>{activity.label}</b>
                            {activity.detail && <small>{activity.detail}</small>}
                          </p>
                        </div>
                      ))}
                      <div className="trace-processing">
                        <span aria-hidden="true"><i /><i /><i /></span>
                        <p>模型正在工作，进度会自动更新</p>
                      </div>
                    </div>
                  </div>

                  <div className="trace-flow" aria-hidden="true">
                    <span />
                    <i />
                    <i />
                    <i />
                    <b>{liveEvidence.length.toString().padStart(2, "0")}</b>
                    <small>EVIDENCE</small>
                  </div>

                  <div className="trace-answer-assembly">
                    <div className="trace-section-label">
                      <span>答案装配</span>
                      <i>{stage === "composing" ? "ACTIVE" : "STANDBY"}</i>
                    </div>
                    <strong key={`${stage}-${statusText}`}>{statusText}</strong>
                    <div className="answer-skeleton" aria-hidden="true">
                      <i />
                      <i />
                      <i />
                      <i />
                      <i />
                    </div>
                    <div className="trace-metrics">
                      <span><b>{liveEvidence.length}</b> 条证据已进入工作区</span>
                      <span>{queuePosition ? `队列 #${queuePosition}` : "通道已连接"}</span>
                    </div>
                  </div>
                </div>

                <footer className="trace-footer">
                  {plan ? (
                    <details className="plan-preview">
                      <summary>
                        <Waypoints size={13} /> 查看本轮调查路径 · {plan.steps.length} 步
                      </summary>
                      <div>
                        {plan.steps.map((step, index) => (
                          <span key={step.id}>
                            <i>{String(index + 1).padStart(2, "0")}</i>
                            {step.purpose}
                          </span>
                        ))}
                      </div>
                    </details>
                  ) : (
                    <span className="trace-privacy-note">
                      展示任务动作与证据进度，不展示模型内部推理
                    </span>
                  )}
                  {currentTaskId && (
                    <button
                      className="trace-task-id"
                      type="button"
                      title={currentTaskId}
                      onClick={() => void copyTraceId(currentTaskId)}
                      aria-label="复制当前任务追溯 ID"
                    >
                      <code>任务 · {currentTaskId.slice(5, 17).toUpperCase()}</code>
                      {copiedTrace === currentTaskId ? <Check size={12} /> : <Copy size={12} />}
                    </button>
                  )}
                  <button
                    className="cancel-task"
                    type="button"
                    onClick={() => void handleCancel()}
                  >
                    <Square size={12} /> 取消调查
                  </button>
                </footer>
              </section>
            )}
            {error && (
              <div className="error-notice recoverable-error">
                <CircleAlert size={17} />
                <span>{error}</span>
                {failedTaskId && (
                  <button
                    type="button"
                    className="failed-task-id"
                    title={failedTaskId}
                    onClick={() => void copyTraceId(failedTaskId)}
                    aria-label="复制失败任务追溯 ID"
                  >
                    <code>{failedTaskId}</code>
                    {copiedTrace === failedTaskId ? <Check size={12} /> : <Copy size={12} />}
                  </button>
                )}
                <button type="button" onClick={() => setError(null)} aria-label="关闭">
                  <X size={15} />
                </button>
                {failedTaskId && (
                  <button type="button" onClick={() => void handleRetry()}>
                    <RefreshCw size={14} /> 重试
                  </button>
                )}
              </div>
            )}
            <div ref={dialogueEndRef} />
          </div>

          <form className="composer" onSubmit={onSubmit}>
            <div className="composer-inner">
              <Sparkles size={18} />
              <textarea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void submitQuestion(input);
                  }
                }}
                placeholder="想了解什么？直接问就好……"
                rows={2}
                disabled={working}
                aria-label="输入校园问题"
              />
              <button
                type="submit"
                disabled={!input.trim() || working}
                aria-label="发送"
              >
                {working ? (
                  <LoaderCircle className="spin" size={20} />
                ) : (
                  <Send size={19} />
                )}
              </button>
            </div>
            <p>
              {authSession?.subject_kind === "local_admin"
                ? "后台身份用于系统管理；校园镜像仍按当前试用范围提供查询。"
                : authSession?.authenticated
                ? "校园身份只扩展可查询信源；城知不会代办申请、选课或报名。"
                : authSession?.mirror_visibility_scopes.includes("campus")
                  ? "试用可查已镜像校园材料；实时校内核验仍需登录。"
                  : "匿名设备数据彼此隔离；登录后由你决定是否合并。"}
            </p>
          </form>
        </section>

        <button
          className="mobile-evidence-trigger"
          type="button"
          onClick={() => {
            setRailOpen(false);
            setEvidenceOpen(true);
          }}
        >
          <PanelRightOpen size={17} />
          证据 {liveEvidence.length}
        </button>
        <EvidenceDesk
          evidence={liveEvidence}
          active={activeEvidence}
          open={evidenceOpen}
          stage={stage}
          onSelect={setActiveEvidence}
          onClose={() => setEvidenceOpen(false)}
        />
      </section>

      <OnboardingPanel profile={profile} onSaved={setProfile} />
      <MySpacePanel
        open={spaceOpen}
        profile={profile}
        todos={todos}
        onClose={() => setSpaceOpen(false)}
        onProfile={setProfile}
        onTodosChanged={refreshTodos}
        onPersonalDataDeleted={handlePersonalDataDeleted}
        onError={setError}
      />
    </AppChrome>
  );
}

type TurnstileWidgetApi = {
  render: (
    element: HTMLElement,
    options: {
      sitekey: string;
      theme?: "light" | "dark" | "auto";
      callback: (token: string) => void;
      "expired-callback"?: () => void;
      "error-callback"?: () => void;
    },
  ) => string | number;
  reset: (widgetId?: string | number) => void;
};

function AgentAccessPanel({
  access,
  pendingQuestion,
  onVerified,
}: {
  access: AgentAccess | null;
  pendingQuestion?: string;
  onVerified: () => Promise<void>;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | number | undefined>(undefined);
  const [widgetError, setWidgetError] = useState<string>();
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    if (!access?.verification_required || !access.turnstile_site_key) {
      widgetIdRef.current = undefined;
      return;
    }
    let cancelled = false;
    const renderWidget = () => {
      if (cancelled || !containerRef.current || widgetIdRef.current !== undefined) return;
      const turnstile = (window as Window & { turnstile?: TurnstileWidgetApi }).turnstile;
      if (!turnstile) {
        setWidgetError("人机验证组件尚未加载，请稍后重试。");
        return;
      }
      setWidgetError(undefined);
      widgetIdRef.current = turnstile.render(containerRef.current, {
        sitekey: access.turnstile_site_key as string,
        theme: "auto",
        callback: (token) => {
          setVerifying(true);
          setWidgetError(undefined);
          void verifyAgent(token)
            .then(onVerified)
            .catch((cause) => {
              setWidgetError(describeAgentError(cause));
              if (widgetIdRef.current !== undefined) turnstile.reset(widgetIdRef.current);
            })
            .finally(() => setVerifying(false));
        },
        "expired-callback": () => setWidgetError("验证已过期，请重新完成验证。"),
        "error-callback": () => setWidgetError("验证组件暂时不可用，请稍后重试。"),
      });
    };

    const existing = document.querySelector<HTMLScriptElement>(
      "script[data-hzcu-turnstile]",
    );
    if ((window as Window & { turnstile?: TurnstileWidgetApi }).turnstile) {
      renderWidget();
    } else if (existing) {
      existing.addEventListener("load", renderWidget);
      existing.addEventListener("error", () => setWidgetError("验证组件加载失败，请稍后重试。"));
    } else {
      const script = document.createElement("script");
      script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
      script.async = true;
      script.defer = true;
      script.dataset.hzcuTurnstile = "true";
      script.addEventListener("load", renderWidget);
      script.addEventListener("error", () => setWidgetError("验证组件加载失败，请稍后重试。"));
      document.head.appendChild(script);
    }
    return () => {
      cancelled = true;
      if (existing) existing.removeEventListener("load", renderWidget);
    };
  }, [access?.turnstile_site_key, access?.verification_required, onVerified]);

  if (!access) return null;
  const showQuota = access.mode === "enforce" && (
    access.window_remaining !== null || access.daily_remaining !== null
  );
  return (
    <>
      {showQuota ? (
        <div className="agent-access-status" role="status">
          <span>匿名试用</span>
          <b>滚动窗口剩余 {access.window_remaining ?? "不限"}</b>
          <b>今日剩余 {access.daily_remaining ?? "不限"}</b>
          <small>窗口重置：{formatResetAt(access.window_reset_at)}</small>
          <small>日额度重置：{formatResetAt(access.daily_reset_at)}</small>
          {(access.running > 0 || access.queued > 0) && <small>运行 / 排队：{access.running} / {access.queued}</small>}
        </div>
      ) : null}
      {access.verification_required ? (
        <section className="agent-verification-gate" aria-live="polite">
          <div>
            <ShieldCheck size={18} />
            <span>
              <b>先完成一次人机验证</b>
              <small>{pendingQuestion ? `验证后自动继续：${pendingQuestion}` : "验证租约有效期为 24 小时。"}</small>
            </span>
          </div>
          <div ref={containerRef} className="agent-turnstile-widget" />
          {verifying ? <small>正在确认验证结果…</small> : null}
          {widgetError ? <small className="agent-verification-error">{widgetError}</small> : null}
        </section>
      ) : null}
    </>
  );
}

function stageOrder(stage: Stage): number {
  return [
    "idle",
    "queued",
    "understanding",
    "planning",
    "investigating",
    "composing",
    "completed",
    "failed",
  ].indexOf(stage);
}
