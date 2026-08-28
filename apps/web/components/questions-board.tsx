"use client";

import { ArrowLeft, BookOpen, Check, Clock3, Feather, LoaderCircle, LogIn, Send } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

import {
  getAuthSession,
  getQuestion,
  listQuestions,
  loginContributor,
  postQuestionAnswer,
  updateQuestionAnswer,
} from "@/lib/api";
import type { AuthSession, QuestionDetail, QuestionSummary } from "@/lib/api";

import { AppChrome } from "./app-chrome";
import { CongyuArtwork } from "./congyu-artwork";
import { useTheme } from "./theme-provider";

function waitingLabel(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  const days = Math.floor(seconds / 86400);
  if (days > 0) return `${days} 天`;
  return `${Math.floor(seconds / 3600)} 小时`;
}

export function QuestionsBoard({ questionId }: { questionId?: string }) {
  const { theme } = useTheme();
  const [questions, setQuestions] = useState<QuestionSummary[]>([]);
  const [question, setQuestion] = useState<QuestionDetail | null>(null);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const nextSession = await getAuthSession();
      setSession(nextSession);
      if (questionId) {
        setQuestion(await getQuestion(questionId));
      } else {
        setQuestions(await listQuestions());
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "问题悬赏版暂时无法连接");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // The route id is the only external input; load is intentionally local to
    // keep the public board simple and avoid a global create-question state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questionId]);

  const congyu = theme === "character";
  const content = questionId ? (
    <QuestionDetailView
      question={question}
      session={session}
      loading={loading}
      error={error}
      onReload={load}
      congyu={congyu}
    />
  ) : (
    <QuestionListView questions={questions} loading={loading} error={error} congyu={congyu} />
  );

  if (congyu) {
    return <CongyuQuestionsShell detail={Boolean(questionId)}>{content}</CongyuQuestionsShell>;
  }

  return (
    <AppChrome
      section="questions"
      className="questions-shell"
      channel="HZCU // COMMUNITY QUESTIONS"
      mode="PUBLIC / REVIEWED QUESTIONS"
      eyebrow="02 / QUESTION BOARD"
      title="问题悬赏版"
      utilities={<a className="back-to-agent" href="/">返回 Agent</a>}
    >
      <main className="questions-content">{content}</main>
    </AppChrome>
  );
}

function CongyuQuestionsShell({ detail, children }: { detail: boolean; children: ReactNode }) {
  return (
    <main className="congyu-questions-page">
      <div className="congyu-questions-sky" aria-hidden="true"><i /><i /></div>
      <header className="congyu-questions-nav">
        <a className="congyu-questions-brand" href="/questions">
          <span><Feather size={19} /></span>
          <span><b>琮羽悬赏板</b><small>CONGYU CAMPUS BOUNTY</small></span>
        </a>
        <nav aria-label="琮羽悬赏板导航">
          <a href="/"><ArrowLeft size={15} /> 返回调查室</a>
          <a href="/sources"><BookOpen size={15} /> 资料馆</a>
        </nav>
      </header>

      <section className="congyu-questions-hero">
        <div>
          <p><span>03</span> CAMPUS CLUE BOUNTY</p>
          <h1>{detail ? "这条悬赏，等你补上线索" : <>有些问题，<br /><em>得请大家帮忙</em></>}</h1>
          <blockquote>
            嗯……有些事，我暂时没能从学校材料里查明白。那就把线索挂出来，请学长和老师一起帮忙；可靠的回答通过审核后，我会认真收进知识库。
          </blockquote>
        </div>
        <CongyuArtwork scene="hello" sizes="(max-width: 760px) 220px, 340px" />
      </section>

      <section className="congyu-questions-desk">{children}</section>
    </main>
  );
}

function QuestionListView({
  questions,
  loading,
  error,
  congyu,
}: {
  questions: QuestionSummary[];
  loading: boolean;
  error: string | null;
  congyu: boolean;
}) {
  return (
    <section className="questions-board">
      {congyu ? (
        <header className="congyu-bounty-heading">
          <div><span>OPEN CLUES</span><h2>挂在板上的线索</h2></div>
          <p>没答上的排在前面，等得越久越靠前。来看看，哪一条正好是你熟悉的事？</p>
        </header>
      ) : (
        <header className="questions-heading">
          <div>
            <p className="eyebrow">REVIEWED CAMPUS QUESTIONS</p>
            <h1>琮羽无法回答的问题，将在这里公开悬赏回答</h1>
          </div>
          <p>你的提问将被公开，请等待学长老师为你解答问题哦！审核通过的回答将会进入琮羽的知识库~</p>
        </header>
      )}
      {loading && <div className="questions-loading"><LoaderCircle className="spin" size={20} />正在读取公开问题</div>}
      {error && <div className="form-error" role="alert">{error}</div>}
      {!loading && !error && questions.length === 0 && <div className="questions-empty">暂时没有公开问题。</div>}
      <div className="questions-list">
        {questions.map((item, index) => (
          <a className="question-list-card" href={`/questions/${item.question_id}`} key={item.question_id}>
            <span className="question-list-index">{String(index + 1).padStart(2, "0")}</span>
            <div>
              <h2>{item.title}</h2>
              <p>{item.details}</p>
              <small><Clock3 size={13} />等待 {waitingLabel(item.waiting_seconds)} · {item.answer_count} 条回答</small>
            </div>
            <b>{item.status === "answered" ? "已有回答" : "待回答"}</b>
          </a>
        ))}
      </div>
    </section>
  );
}

function QuestionDetailView({
  question,
  session,
  loading,
  error,
  onReload,
  congyu,
}: {
  question: QuestionDetail | null;
  session: AuthSession | null;
  loading: boolean;
  error: string | null;
  onReload: () => Promise<void>;
  congyu: boolean;
}) {
  const [loginOpen, setLoginOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [answer, setAnswer] = useState("");
  const [editingAnswerId, setEditingAnswerId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  async function submitLogin() {
    setBusy(true);
    setFormError(null);
    try {
      await loginContributor(username, password);
      setLoginOpen(false);
      setPassword("");
      await onReload();
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : "贡献者登录失败");
    } finally {
      setBusy(false);
    }
  }

  async function submitAnswer() {
    if (!question || answer.trim().length < 2) return;
    setBusy(true);
    setFormError(null);
    try {
      if (editingAnswerId) {
        await updateQuestionAnswer(question.question_id, editingAnswerId, answer);
      } else {
        await postQuestionAnswer(question.question_id, answer);
      }
      setAnswer("");
      setEditingAnswerId(null);
      setMessage(editingAnswerId ? "回答已更新，已发布知识仍需管理员复审。" : "回答已发布，尚未纳入 Agent 资料库。");
      await onReload();
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : "回答提交失败");
    } finally {
      setBusy(false);
    }
  }

  function beginEdit(item: QuestionDetail["answers"][number]) {
    setEditingAnswerId(item.answer_id);
    setAnswer(item.answer_markdown);
    setMessage(null);
    setFormError(null);
  }

  function cancelEdit() {
    setEditingAnswerId(null);
    setAnswer("");
    setFormError(null);
  }

  const canAnswer = session?.role === "contributor" || session?.role === "admin";
  return (
    <section className="question-detail">
      <a className="questions-back" href="/questions"><ArrowLeft size={15} />返回问题悬赏版</a>
      {loading && <div className="questions-loading"><LoaderCircle className="spin" size={20} />正在读取问题</div>}
      {error && <div className="form-error" role="alert">{error}</div>}
      {question && !loading && (
        <>
          <header className="question-detail-heading">
            <span className="question-status">{question.status === "answered" ? "已有回答" : "待回答"}</span>
            <h1>{question.title}</h1>
            <p>{question.details}</p>
            <small><Clock3 size={13} />公开等待 {waitingLabel(question.waiting_seconds)}</small>
          </header>
          {question.evidence_gap && <aside className="question-evidence-gap"><b>原回答的证据缺口</b><span>{question.evidence_gap}</span></aside>}
          <section className="community-answers">
            <header><h2>授权贡献者回答</h2><span>{question.answers.length} 条</span></header>
            {question.answers.length === 0 ? <p className="questions-empty">还没有回答，欢迎授权贡献者补充。</p> : question.answers.map((item) => (
              <article key={item.answer_id} className="community-answer-card">
                <header><b>{item.display_name}</b>{item.unit && <span>{item.unit}</span>}<small>{new Date(item.created_at).toLocaleString("zh-CN")}</small></header>
                <ReactMarkdown>{item.answer_markdown}</ReactMarkdown>
                <footer>
                  <span>{item.knowledge_review_state === "published" ? "已纳入 Agent 资料库" : item.knowledge_review_state === "source_changed" ? "来源回答已变化，等待复审" : "授权贡献者回答，尚未纳入资料库"}</span>
                  {item.knowledge_review_state === "published" && <Check size={14} />}
                  {item.can_edit && <button type="button" onClick={() => beginEdit(item)}>编辑</button>}
                </footer>
              </article>
            ))}
          </section>
          {message && <p className="form-success">{message}</p>}
          {formError && <p className="form-error" role="alert">{formError}</p>}
          {canAnswer ? (
            <section className="community-answer-form">
              <label>{editingAnswerId ? "编辑你的回答" : "补充一个可核验的回答"}<textarea value={answer} onChange={(event) => setAnswer(event.target.value)} rows={7} maxLength={12000} /></label>
              <div className="community-answer-actions">
                {editingAnswerId && <button type="button" onClick={cancelEdit}>取消编辑</button>}
                <button type="button" disabled={busy || answer.trim().length < 2} onClick={() => void submitAnswer()}><Send size={15} />{busy ? "提交中…" : editingAnswerId ? "保存修改" : "发布回答"}</button>
              </div>
            </section>
          ) : (
            <section className="contributor-login-box">
              <p>问题悬赏版只允许授权贡献者回答。</p>
              <button type="button" onClick={() => setLoginOpen((value) => !value)}><LogIn size={15} />贡献者登录</button>
              {loginOpen && (
                <div className="contributor-login-form">
                  <input placeholder="登录名" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
                  <input placeholder="密码" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
                  <button type="button" disabled={busy || !username || !password} onClick={() => void submitLogin()}>{busy ? "登录中…" : "登录并回答"}</button>
                </div>
              )}
            </section>
          )}
        </>
      )}
    </section>
  );
}
