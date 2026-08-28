"use client";

import {
  BookOpen,
  Check,
  ChevronRight,
  CircleAlert,
  Clock3,
  Copy,
  Feather,
  History,
  ListChecks,
  LoaderCircle,
  Menu,
  PanelRightOpen,
  RefreshCw,
  Send,
  Sparkles,
  Square,
  ThumbsDown,
  ThumbsUp,
  UserRound,
  Waypoints,
  X,
} from "lucide-react";
import type { FormEvent, RefObject } from "react";
import ReactMarkdown from "react-markdown";

import type {
  AgentAnswer,
  AuthSession,
  ConversationSummary,
  Evidence,
  Health,
  StudentProfile,
  UserTodo,
} from "@/lib/api";

import { CongyuArtwork, type CongyuScene } from "./congyu-artwork";
import { IdentityControl } from "./identity-control";
import { MySpacePanel, OnboardingPanel } from "./product-panels";

export type CongyuStage =
  | "idle"
  | "queued"
  | "understanding"
  | "planning"
  | "investigating"
  | "composing"
  | "completed"
  | "failed";

export type CongyuChatMessage =
  | { id: string; role: "user"; content: string; createdAt: string }
  | { id: string; role: "assistant"; answer: AgentAnswer; createdAt: string };

export type CongyuPlan = {
  objective: string;
  steps: Array<{ id: string; purpose: string; tool: string }>;
};

export type CongyuTraceActivity = {
  id: string;
  stage: CongyuStage;
  label: string;
  detail?: string;
  createdAt: number;
};

type Props = {
  conversationId?: string;
  conversations: ConversationSummary[];
  messages: CongyuChatMessage[];
  input: string;
  stage: CongyuStage;
  statusText: string;
  queuePosition: number;
  waitSeconds: number;
  liveEvidence: Evidence[];
  activeEvidence: Evidence | null;
  plan: CongyuPlan | null;
  traceActivities: CongyuTraceActivity[];
  traceStartedAt: number;
  health: Health | null;
  authSession: AuthSession | null;
  authBusy: boolean;
  profile: StudentProfile | null;
  todos: UserTodo[];
  currentTaskId?: string;
  failedTaskId?: string;
  loadingConversation: boolean;
  railOpen: boolean;
  evidenceOpen: boolean;
  spaceOpen: boolean;
  mergePrompt: boolean;
  feedbackState: Record<string, string>;
  error: string | null;
  copiedTrace?: string;
  working: boolean;
  quickQuestions: string[];
  messageListRef: RefObject<HTMLDivElement | null>;
  dialogueEndRef: RefObject<HTMLDivElement | null>;
  onInput: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
  onQuestion: (question: string) => void;
  onNew: () => void;
  onLoadConversation: (id: string) => void;
  onRailOpen: (open: boolean) => void;
  onEvidenceOpen: (open: boolean) => void;
  onSpaceOpen: (open: boolean) => void;
  onEvidence: (evidence: Evidence | null) => void;
  onLogout: () => void;
  onAuthenticated: () => void;
  onMerge: () => void;
  onDismissMerge: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onReverify: (answerId: string) => void;
  onSaveAction: (answer: AgentAnswer, action: string, index: number) => void;
  onFeedback: (answerId: string, rating: "helpful" | "not_helpful") => void;
  onConfirmSuggestion: (attributeId: string) => void;
  onProfile: (profile: StudentProfile) => void;
  onTodosChanged: () => Promise<void>;
  onPersonalDataDeleted: () => void;
  onError: (message: string | null) => void;
  onCopyTrace: (value: string) => void;
  onDialogueScroll: (following: boolean) => void;
};

const STAGE_COPY: Record<CongyuStage, { label: string; note: string; scene: CongyuScene }> = {
  idle: { label: "等你来问", note: "把线索交给我吧。", scene: "idle" },
  queued: { label: "排队登记中", note: "我已经收下问题，很快开始。", scene: "idle" },
  understanding: { label: "正在读题", note: "先弄清你真正想知道什么。", scene: "working" },
  planning: { label: "铺开调查路线", note: "正在决定先查哪一本资料。", scene: "working" },
  investigating: { label: "翻阅校园资料", note: "我会把出处也一起整理好。", scene: "working" },
  composing: { label: "装订答案", note: "线索已齐，正在写成清楚的回答。", scene: "working" },
  completed: { label: "调查完成", note: "答案和依据都整理好了。", scene: "success" },
  failed: { label: "线索暂时断了", note: "可以重试，或者换一种说法。", scene: "error" },
};

function citationMarkdown(markdown: string) {
  return markdown.replace(/\[来源(\d+)\]/g, "[来源$1](#evidence-$1)");
}

function waitLabel(seconds: number) {
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function verificationLabel(mode: AgentAnswer["verification_mode"]) {
  if (mode === "live_verified") return "实时核验";
  if (mode === "historical") return "历史材料";
  if (mode === "degraded") return "降级回答";
  return "本地镜像";
}

export function CongyuAgentView(props: Props) {
  const roomMode =
    props.messages.length > 0 || props.working || Boolean(props.conversationId);
  const stageCopy = STAGE_COPY[props.stage];

  return (
    <main
      className={`congyu-agent congyu-agent-${roomMode ? "room" : "welcome"} congyu-stage-${props.stage}`}
      data-congyu-mode={roomMode ? "room" : "welcome"}
    >
      <div className="congyu-sky" aria-hidden="true">
        <span className="congyu-wing congyu-wing-left" />
        <span className="congyu-wing congyu-wing-right" />
        <span className="congyu-light-band" />
        <span className="congyu-star-field" />
      </div>

      <header className="congyu-nav">
        <a className="congyu-brand" href="/" aria-label="琮羽校园 Agent 首页">
          <span className="congyu-brand-mark"><Feather size={20} /></span>
          <span><b>琮羽</b><small>HZCU CAMPUS AGENT</small></span>
        </a>
        <nav aria-label="琮羽主题主导航">
          <button
            type="button"
            onClick={() => props.onRailOpen(true)}
            aria-label="打开会话列表"
          >
            <History size={16} /> 会话
          </button>
          <a href="/sources"><BookOpen size={16} /> 资料馆</a>
          <button type="button" onClick={() => props.onSpaceOpen(true)}>
            <UserRound size={16} /> 我的手帐
          </button>
        </nav>
        <IdentityControl
          session={props.authSession}
          busy={props.authBusy}
          onLogout={props.onLogout}
          onAuthenticated={props.onAuthenticated}
        />
      </header>

      {props.mergePrompt && (
        <aside className="congyu-merge-banner">
          <History size={17} />
          <span><b>发现登录前的手帐内容</b>要把会话、待办和反馈并入校园账号吗？</span>
          <button type="button" onClick={props.onMerge}>合并</button>
          <button type="button" onClick={props.onDismissMerge}>暂不</button>
        </aside>
      )}

      {!roomMode ? (
        <WelcomeStage {...props} />
      ) : (
        <InvestigationRoom {...props} stageCopy={stageCopy} />
      )}

      <HistoryDrawer {...props} />
      <EvidenceDrawer {...props} />
      <OnboardingPanel profile={props.profile} onSaved={props.onProfile} />
      <MySpacePanel
        open={props.spaceOpen}
        profile={props.profile}
        todos={props.todos}
        onClose={() => props.onSpaceOpen(false)}
        onProfile={props.onProfile}
        onTodosChanged={props.onTodosChanged}
        onPersonalDataDeleted={props.onPersonalDataDeleted}
        onError={(message) => props.onError(message)}
      />
    </main>
  );
}

function WelcomeStage(props: Props) {
  return (
    <section
      className="congyu-welcome-stage"
      aria-labelledby="congyu-welcome-title"
      onPointerMove={(event) => {
        if (event.pointerType === "touch") return;
        const bounds = event.currentTarget.getBoundingClientRect();
        const x = Math.max(-8, Math.min(8, ((event.clientX - bounds.left) / bounds.width - 0.5) * 16));
        const y = Math.max(-8, Math.min(8, ((event.clientY - bounds.top) / bounds.height - 0.5) * 16));
        event.currentTarget.style.setProperty("--congyu-parallax-x", `${x.toFixed(2)}px`);
        event.currentTarget.style.setProperty("--congyu-parallax-y", `${y.toFixed(2)}px`);
      }}
      onPointerLeave={(event) => {
        event.currentTarget.style.setProperty("--congyu-parallax-x", "0px");
        event.currentTarget.style.setProperty("--congyu-parallax-y", "0px");
      }}
    >
      <div className="congyu-welcome-copy">
        <p className="congyu-chapter"><span>01</span> CAMPUS INVESTIGATION</p>
        <h1 id="congyu-welcome-title">校园里的事，<br /><em>和琮羽一起查清</em></h1>
        <p className="congyu-intro">
          课程、校历、竞赛和办事流程，都可以直接问我。需要核实的地方，我会翻阅学校材料，把结论和出处一起交给你。
        </p>
        <form className="congyu-welcome-composer" onSubmit={props.onSubmit}>
          <Sparkles size={20} />
          <textarea
            value={props.input}
            onChange={(event) => props.onInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                props.onQuestion(props.input);
              }
            }}
            placeholder="告诉琮羽，你想查清什么？"
            rows={2}
            aria-label="输入校园问题"
          />
          <button type="submit" disabled={!props.input.trim()} aria-label="开始调查">
            <Send size={19} /><span>开始调查</span>
          </button>
        </form>
        <div className="congyu-quick-questions" aria-label="快速问题">
          {props.quickQuestions.map((question, index) => (
            <button type="button" key={question} onClick={() => props.onQuestion(question)}>
              <span>0{index + 1}</span><b>{question}</b><ChevronRight size={16} />
            </button>
          ))}
        </div>
      </div>
      <div className="congyu-hero-stage" aria-label="琮羽角色舞台">
        <span className="congyu-hero-insignia" aria-hidden="true">CONGYU<br /><i>羽翼调查员</i></span>
        <CongyuArtwork scene="welcome" className="congyu-desktop-hero" sizes="(max-width: 840px) 0px, 46vw" />
        <CongyuArtwork scene="mobile-welcome" className="congyu-mobile-hero" sizes="(max-width: 840px) 320px, 0px" />
        <span className="congyu-hero-caption"><b>琮羽</b><small>校园里的线索，由我来替你理清。</small></span>
      </div>
    </section>
  );
}

function InvestigationRoom(props: Props & { stageCopy: (typeof STAGE_COPY)[CongyuStage] }) {
  return (
    <section className="congyu-room-shell">
      <div className="congyu-page-sweep" aria-hidden="true" />
      <section className="congyu-dialogue-panel" aria-label="与琮羽对话">
        <header className="congyu-room-header">
          <div>
            <p>INVESTIGATION ROOM</p>
            <h1>校园调查室</h1>
          </div>
          <div className="congyu-room-actions">
            <button type="button" onClick={props.onNew}><Sparkles size={15} /> 新调查</button>
            <button type="button" onClick={() => props.onRailOpen(true)}><Menu size={16} /> 历史</button>
            <button type="button" onClick={() => props.onEvidenceOpen(true)}><PanelRightOpen size={16} /> 资料 {props.liveEvidence.length}</button>
          </div>
        </header>

        {props.conversationId && (
          <div className="congyu-trace-strip">
            <span>手帐编号 · {props.conversationId.slice(5, 17).toUpperCase()}</span>
            <button type="button" onClick={() => props.onCopyTrace(props.conversationId!)}>
              {props.copiedTrace === props.conversationId ? <Check size={13} /> : <Copy size={13} />}
              {props.copiedTrace === props.conversationId ? "已复制" : "复制"}
            </button>
          </div>
        )}

        <section className="congyu-mobile-status" aria-label={`琮羽状态：${props.stageCopy.label}`}>
          <CongyuArtwork scene={props.stageCopy.scene} sizes="128px" />
          <div><span>{props.stageCopy.label}</span><p>{props.stageCopy.note}</p></div>
        </section>

        <div
          ref={props.messageListRef}
          className="congyu-message-list"
          onScroll={(event) => {
            const target = event.currentTarget;
            props.onDialogueScroll(target.scrollHeight - target.scrollTop - target.clientHeight < 120);
          }}
        >
          {props.loadingConversation && <div className="congyu-loading"><LoaderCircle className="spin" size={18} />正在恢复手帐</div>}
          {props.messages.map((message) =>
            message.role === "user" ? (
              <article className="congyu-user-message" key={message.id}>
                <span>你</span><p>{message.content}</p>
              </article>
            ) : (
              <AnswerCard key={message.id} answer={message.answer} {...props} />
            ),
          )}
          {props.working && <WorkingCard {...props} />}
          {props.error && (
            <div className="congyu-error" role="alert">
              <CircleAlert size={18} /><span>{props.error}</span>
              {props.failedTaskId && <button type="button" onClick={props.onRetry}><RefreshCw size={14} />重试</button>}
              <button type="button" onClick={() => props.onError(null)} aria-label="关闭提示"><X size={15} /></button>
            </div>
          )}
          <div ref={props.dialogueEndRef} />
        </div>

        <form className="congyu-room-composer" onSubmit={props.onSubmit}>
          <div>
            <Feather size={19} />
            <textarea
              value={props.input}
              onChange={(event) => props.onInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  props.onQuestion(props.input);
                }
              }}
              placeholder="继续追问，或交给琮羽一条新线索……"
              rows={2}
              disabled={props.working}
              aria-label="输入校园问题"
            />
            <button type="submit" disabled={!props.input.trim() || props.working} aria-label="发送">
              {props.working ? <LoaderCircle className="spin" size={20} /> : <Send size={19} />}
            </button>
          </div>
          <small>事实回答保持克制；重要结论请同时查看右侧官方依据。</small>
        </form>
      </section>

      <Notebook {...props} />
    </section>
  );
}

function AnswerCard({ answer, ...props }: Props & { answer: AgentAnswer }) {
  return (
    <article className="congyu-answer-card">
      <header>
        <CongyuArtwork
          scene="avatar"
          className="congyu-answer-avatar"
          sizes="58px"
        />
        <div><p>调查答复</p><h2>{answer.headline}</h2></div>
        <div className="congyu-answer-meta"><b>{verificationLabel(answer.verification_mode)}</b><span>置信度 {answer.confidence}</span></div>
      </header>
      <div className="congyu-answer-paper markdown">
        <ReactMarkdown
          components={{
            a: ({ href, children }) => (
              <a
                href={href}
                onClick={(event) => {
                  const match = href?.match(/^#evidence-(\d+)$/);
                  if (!match) return;
                  event.preventDefault();
                  const evidence = answer.evidence[Number(match[1]) - 1];
                  if (evidence) {
                    props.onEvidence(evidence);
                    props.onEvidenceOpen(true);
                  }
                }}
                target={href?.startsWith("http") ? "_blank" : undefined}
                rel={href?.startsWith("http") ? "noreferrer" : undefined}
              >{children}</a>
            ),
          }}
        >{citationMarkdown(answer.answer_markdown)}</ReactMarkdown>
      </div>
      {answer.assumptions.length > 0 && <aside className="congyu-uncertainty"><CircleAlert size={15} />{answer.assumptions.join("；")}</aside>}
      {answer.next_actions.length > 0 && (
        <div className="congyu-next-actions"><p>可以收进待办</p>{answer.next_actions.map((action, index) => (
          <button type="button" key={action} onClick={() => props.onSaveAction(answer, action, index)}><ListChecks size={13} />{action}</button>
        ))}</div>
      )}
      {answer.profile_suggestions?.length > 0 && (
        <div className="congyu-profile-suggestions"><p>要把这些信息记进你的手帐吗？</p>{answer.profile_suggestions.map((item) => (
          <span key={item.attribute_id}><b>{item.attribute_value}</b><button type="button" onClick={() => props.onConfirmSuggestion(item.attribute_id)}>确认</button></span>
        ))}</div>
      )}
      <footer>
        <span>这次调查</span>
        <button className={props.feedbackState[answer.answer_id] === "helpful" ? "active" : ""} type="button" onClick={() => props.onFeedback(answer.answer_id, "helpful")}><ThumbsUp size={14} />有帮助</button>
        <button className={props.feedbackState[answer.answer_id] === "not_helpful" ? "active" : ""} type="button" onClick={() => props.onFeedback(answer.answer_id, "not_helpful")}><ThumbsDown size={14} />有问题</button>
        <button type="button" disabled={props.working} onClick={() => props.onReverify(answer.answer_id)}><RefreshCw size={14} />重新核验</button>
        <details><summary>追溯信息</summary><button type="button" onClick={() => props.onCopyTrace(answer.task_id)}><Copy size={12} />任务 {answer.task_id.slice(5, 17).toUpperCase()}</button></details>
      </footer>
    </article>
  );
}

function WorkingCard(props: Props) {
  const copy = STAGE_COPY[props.stage];
  return (
    <section className="congyu-working-card" aria-live="polite">
      <header><span className="congyu-pulse" /><div><p>LIVE INVESTIGATION</p><h2>{copy.label}</h2></div><span><Clock3 size={13} />{waitLabel(props.waitSeconds)}</span></header>
      <p className="congyu-working-status">{props.statusText}</p>
      <div className="congyu-route-line" aria-label="调查阶段">
        {["理解", "规划", "查证", "成稿"].map((label, index) => <span key={label} className={index <= Math.max(0, ["understanding", "planning", "investigating", "composing"].indexOf(props.stage)) ? "active" : ""}><i>{index + 1}</i>{label}</span>)}
      </div>
      <div className="congyu-activity-list">{props.traceActivities.slice(-4).map((activity) => <p key={activity.id}><b>{activity.label}</b>{activity.detail && <small>{activity.detail}</small>}</p>)}</div>
      <footer>
        {props.plan && <details><summary><Waypoints size={13} />调查路线 · {props.plan.steps.length} 步</summary>{props.plan.steps.map((step, index) => <span key={step.id}><i>{index + 1}</i>{step.purpose}</span>)}</details>}
        <span>{props.queuePosition ? `队列 #${props.queuePosition}` : `${props.liveEvidence.length} 条资料已进入手帐`}</span>
        <button type="button" onClick={props.onCancel}><Square size={12} />取消</button>
      </footer>
    </section>
  );
}

function Notebook(props: Props) {
  const copy = STAGE_COPY[props.stage];
  const visibleEvidence = props.liveEvidence.slice(0, 8);
  return (
    <aside className="congyu-notebook" aria-label="琮羽调查手帐">
      <header><span>CONGYU'S FIELD NOTES</span><h2>琮羽调查手帐</h2></header>
      <section className="congyu-status-page">
        <CongyuArtwork scene={copy.scene} sizes="210px" />
        <div><span className={`congyu-status-stamp congyu-status-${props.stage}`}>{copy.label}</span><p>{copy.note}</p></div>
      </section>
      <nav className="congyu-notebook-tabs" aria-label="手帐页签"><span className="active">本轮线索</span><button type="button" onClick={() => props.onRailOpen(true)}>会话索引</button></nav>
      <div className="congyu-evidence-index">
        {visibleEvidence.length === 0 ? <p className="congyu-empty-notes">提问后，查到的官方资料会按顺序钉在这里。</p> : visibleEvidence.map((item, index) => (
          <button type="button" key={item.evidence_id} className={props.activeEvidence?.evidence_id === item.evidence_id ? "active" : ""} onClick={() => props.onEvidence(item)}>
            <i>{String(index + 1).padStart(2, "0")}</i><span><b>{item.title}</b><small>{item.publisher}</small></span><ChevronRight size={14} />
          </button>
        ))}
      </div>
      {props.activeEvidence && <EvidencePage evidence={props.activeEvidence} onClose={() => props.onEvidence(null)} />}
      <footer><span>{props.health?.status === "ok" ? "官方资料通道已连接" : "资料通道连接中"}</span><b>{props.liveEvidence.length.toString().padStart(2, "0")} CLUES</b></footer>
    </aside>
  );
}

function EvidencePage({ evidence, onClose }: { evidence: Evidence; onClose: () => void }) {
  return (
    <article className="congyu-evidence-page">
      <button type="button" onClick={onClose} aria-label="收起资料"><X size={15} /></button>
      <p>OFFICIAL MATERIAL</p><h3>{evidence.title}</h3><span>{evidence.publisher}</span><blockquote>{evidence.excerpt}</blockquote>
      <a href={evidence.canonical_url} target="_blank" rel="noreferrer">打开官方原文 <ChevronRight size={14} /></a>
    </article>
  );
}

function HistoryDrawer(props: Props) {
  return (
    <div className={`congyu-drawer-layer congyu-history-layer ${props.railOpen ? "open" : ""}`} aria-hidden={!props.railOpen}>
      <button className="congyu-drawer-scrim" type="button" onClick={() => props.onRailOpen(false)} aria-label="关闭会话抽屉" />
      <aside className="congyu-history-drawer">
        <header><div><p>NOTEBOOK INDEX</p><h2>调查记录</h2></div><button type="button" onClick={() => props.onRailOpen(false)} aria-label="关闭会话列表"><X size={18} /></button></header>
        <button className="congyu-new-investigation" type="button" onClick={() => { props.onNew(); props.onRailOpen(false); }}><Sparkles size={16} />开始一轮新调查</button>
        <div>{props.conversations.length === 0 ? <p className="congyu-empty-history">还没有调查记录。</p> : props.conversations.map((item, index) => (
          <button type="button" key={item.conversation_id} className={props.conversationId === item.conversation_id ? "active" : ""} onClick={() => { props.onLoadConversation(item.conversation_id); props.onRailOpen(false); }}>
            <i>{String(index + 1).padStart(2, "0")}</i><span><b>{item.title || "未命名调查"}</b><small>{new Date(item.updated_at).toLocaleDateString("zh-CN")}</small></span><ChevronRight size={14} />
          </button>
        ))}</div>
      </aside>
    </div>
  );
}

function EvidenceDrawer(props: Props) {
  return (
    <div className={`congyu-drawer-layer congyu-evidence-layer ${props.evidenceOpen ? "open" : ""}`} aria-hidden={!props.evidenceOpen}>
      <button className="congyu-drawer-scrim" type="button" onClick={() => props.onEvidenceOpen(false)} aria-label="关闭资料抽屉" />
      <aside className="congyu-evidence-drawer">
        <header><div><p>FIELD MATERIALS</p><h2>本轮资料</h2></div><button type="button" onClick={() => props.onEvidenceOpen(false)}><X size={18} /></button></header>
        <div className="congyu-drawer-evidence-list">{props.liveEvidence.length === 0 ? <p>调查资料还未进入手帐。</p> : props.liveEvidence.map((item, index) => (
          <button type="button" key={item.evidence_id} onClick={() => props.onEvidence(item)}><i>{index + 1}</i><span><b>{item.title}</b><small>{item.publisher}</small></span></button>
        ))}</div>
        {props.activeEvidence && <EvidencePage evidence={props.activeEvidence} onClose={() => props.onEvidence(null)} />}
      </aside>
    </div>
  );
}
