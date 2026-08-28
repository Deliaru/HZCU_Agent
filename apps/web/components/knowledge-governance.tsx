"use client";

import { Check, Plus, RefreshCw, Shield, Trash2, WandSparkles } from "lucide-react";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  createAdminContributor,
  createAdminKnowledge,
  getAdminContributors,
  getAdminKnowledge,
  getAdminQuestions,
  moderateCommunityAnswer,
  optimizeAdminKnowledge,
  publishAdminKnowledge,
  retireAdminKnowledge,
  reviewAdminQuestion,
  updateAdminKnowledge,
  updateAdminContributor,
} from "@/lib/api";
import type { Contributor, KnowledgeEntry, KnowledgeOptimization, QuestionDetail } from "@/lib/api";

type KnowledgeDraft = Parameters<typeof createAdminKnowledge>[0];

const EMPTY_DRAFT: KnowledgeDraft = {
  question_id: null,
  title: "",
  canonical_question: "",
  answer_markdown: "",
  category: "校园综合",
  alternative_phrasings: [],
  applicable_scope: "",
  maintainer_unit: "",
  basis_note: "",
  validity: "stable",
  effective_from: null,
  effective_to: null,
  visibility: "public",
  origin_answer_ids: [],
};

export function KnowledgeGovernancePanel() {
  const [questions, setQuestions] = useState<QuestionDetail[]>([]);
  const [contributors, setContributors] = useState<Contributor[]>([]);
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [draft, setDraft] = useState<KnowledgeDraft>(EMPTY_DRAFT);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [optimization, setOptimization] = useState<Record<string, KnowledgeOptimization>>({});
  const [contributorDraft, setContributorDraft] = useState({ username: "", password: "", public_name: "", unit: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    setBusy(true);
    setError(null);
    try {
      const [nextQuestions, nextContributors, nextEntries] = await Promise.all([
        getAdminQuestions(),
        getAdminContributors(),
        getAdminKnowledge(),
      ]);
      setQuestions(nextQuestions);
      setContributors(nextContributors);
      setEntries(nextEntries);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "知识治理数据读取失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function review(questionId: string, status: "open" | "rejected" | "hidden") {
    setError(null);
    try {
      await reviewAdminQuestion(questionId, { status });
      setNotice(status === "open" ? "问题已公开。" : "问题状态已更新。" );
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "问题审核失败");
    }
  }

  async function createContributor(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await createAdminContributor(contributorDraft);
      setContributorDraft({ username: "", password: "", public_name: "", unit: "" });
      setNotice("贡献者账号已创建。");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "贡献者创建失败");
    }
  }

  async function createKnowledge(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      if (editingId) {
        await updateAdminKnowledge(editingId, draft);
      } else {
        await createAdminKnowledge(draft);
      }
      setDraft(EMPTY_DRAFT);
      setEditingId(null);
      setNotice(editingId ? "人工知识草稿已更新，请重新显式发布后才会进入检索。" : "人工知识草稿已保存，请显式发布后才会进入检索。");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "人工知识保存失败");
    }
  }

  async function optimize(entryId: string) {
    try {
      const result = await optimizeAdminKnowledge(entryId);
      setOptimization((current) => ({ ...current, [entryId]: result }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "优化建议生成失败");
    }
  }

  function applyOptimization(entry: KnowledgeEntry) {
    const result = optimization[entry.entry_id];
    if (!result) return;
    setEditingId(entry.entry_id);
    setDraft({
      question_id: entry.question_id,
      title: result.suggested_title,
      canonical_question: entry.canonical_question,
      answer_markdown: entry.answer_markdown,
      category: result.suggested_category,
      alternative_phrasings: result.suggested_phrasings,
      applicable_scope: entry.applicable_scope,
      maintainer_unit: entry.maintainer_unit,
      basis_note: entry.basis_note,
      validity: entry.validity,
      effective_from: entry.effective_from,
      effective_to: entry.effective_to,
      visibility: entry.visibility,
      origin_answer_ids: entry.origin_answer_ids,
    });
    setNotice("优化建议已应用到编辑器；事实回答未被改写，仍需显式保存并发布。 ");
  }

  async function moderateAnswer(questionId: string, answerId: string, status: "visible" | "hidden") {
    setError(null);
    try {
      await moderateCommunityAnswer(questionId, answerId, status);
      setNotice(status === "hidden" ? "回答已隐藏。" : "回答已重新公开。");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "回答审核失败");
    }
  }

  async function publish(entryId: string) {
    try {
      await publishAdminKnowledge(entryId);
      setNotice("人工知识已发布，新版本已进入检索索引。");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "知识发布失败");
    }
  }

  async function toggleContributor(item: Contributor) {
    try {
      await updateAdminContributor(item.contributor_id, {
        status: item.status === "active" ? "disabled" : "active",
      });
      setNotice(item.status === "active" ? "贡献者已停用，现有会话已撤销。" : "贡献者已重新启用。");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "贡献者状态更新失败");
    }
  }

  async function resetContributor(item: Contributor) {
    const nextPassword = window.prompt(`为 ${item.public_name} 设置新密码（至少 6 位）`);
    if (!nextPassword) return;
    try {
      await updateAdminContributor(item.contributor_id, { password: nextPassword });
      setNotice("贡献者密码已重置。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "贡献者密码重置失败");
    }
  }

  async function retire(entryId: string) {
    try {
      await retireAdminKnowledge(entryId);
      setNotice("人工知识已退休，不再被检索召回。");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "知识退休失败");
    }
  }

  function beginEdit(entry: KnowledgeEntry) {
    setEditingId(entry.entry_id);
    setDraft({
      question_id: entry.question_id,
      title: entry.title,
      canonical_question: entry.canonical_question,
      answer_markdown: entry.answer_markdown,
      category: entry.category,
      alternative_phrasings: entry.alternative_phrasings,
      applicable_scope: entry.applicable_scope,
      maintainer_unit: entry.maintainer_unit,
      basis_note: entry.basis_note,
      validity: entry.validity,
      effective_from: entry.effective_from,
      effective_to: entry.effective_to,
      visibility: entry.visibility,
      origin_answer_ids: entry.origin_answer_ids,
    });
  }

  function cancelEdit() {
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
  }

  function beginKnowledgeFromAnswer(question: QuestionDetail, answer: QuestionDetail["answers"][number]) {
    const existing = entries.find((entry) => entry.question_id === question.question_id);
    if (existing) {
      setEditingId(existing.entry_id);
      setDraft({
        question_id: existing.question_id,
        title: existing.title,
        canonical_question: existing.canonical_question,
        answer_markdown: existing.answer_markdown,
        category: existing.category,
        alternative_phrasings: existing.alternative_phrasings,
        applicable_scope: existing.applicable_scope,
        maintainer_unit: existing.maintainer_unit,
        basis_note: existing.basis_note,
        validity: existing.validity,
        effective_from: existing.effective_from,
        effective_to: existing.effective_to,
        visibility: existing.visibility,
        origin_answer_ids: Array.from(new Set([...existing.origin_answer_ids, answer.answer_id])),
      });
      setNotice("已打开该问题的人工知识条目，并补选这份回答作为来源。请检查事实、范围和依据后再保存发布。");
    } else {
      setEditingId(null);
      setDraft({
        ...EMPTY_DRAFT,
        question_id: question.question_id,
        title: question.title,
        canonical_question: question.title,
        answer_markdown: answer.answer_markdown,
        origin_answer_ids: [answer.answer_id],
      });
      setNotice("回答已带入人工知识草稿。请补充适用范围和依据，保存后再显式发布。");
    }
    window.setTimeout(() => {
      document.querySelector(".knowledge-entry-form")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }

  const availableAnswers = questions.flatMap((question) =>
    (question.status === "open" || question.status === "answered" ? question.answers : [])
      .filter((answer) => answer.status === "visible")
      .map((answer) => ({ ...answer, questionTitle: question.title })),
  );
  const pendingQuestions = questions.filter((question) => question.status === "pending_review");

  return (
    <section className="knowledge-governance">
      <header className="knowledge-governance-heading">
        <div><p className="eyebrow">CURATED KNOWLEDGE / REVIEW GATE</p><h2>问题审核、贡献者与人工资料</h2></div>
        <button type="button" onClick={() => void load()} disabled={busy}><RefreshCw size={15} />刷新</button>
      </header>
      {error && <div className="form-error" role="alert">{error}</div>}
      {notice && <div className="form-success"><Check size={14} />{notice}</div>}

      <section className="knowledge-governance-section">
        <header><div><p className="eyebrow">PENDING REVIEW</p><h3>待审问题</h3></div><span>{pendingQuestions.length}</span></header>
        {pendingQuestions.length === 0 ? <p>当前没有待审问题。</p> : pendingQuestions.map((question) => (
          <article className="governance-question" key={question.question_id}>
            <div><b>{question.title}</b><p>{question.details}</p></div>
            <div><button type="button" onClick={() => void review(question.question_id, "open")}>公开</button><button type="button" onClick={() => void review(question.question_id, "rejected")}><Trash2 size={13} />驳回</button></div>
          </article>
        ))}
      </section>

      <section className="knowledge-governance-section">
        <header><div><p className="eyebrow">ANSWER REVIEW</p><h3>社区回答及入库审核</h3></div><span>{availableAnswers.length}</span></header>
        {questions.filter((question) => question.answers.length > 0).map((question) => (
          <article className="governance-question" key={`answers-${question.question_id}`}>
            <div><b>{question.title}</b>{question.answers.map((answer) => <div className="governance-answer" key={answer.answer_id}>
              <span><strong>{answer.display_name}</strong> · {answer.answer_markdown}</span>
              <small>{answer.knowledge_review_state}</small>
              <div className="governance-answer-actions">
                <button type="button" onClick={() => void moderateAnswer(question.question_id, answer.answer_id, answer.status === "hidden" ? "visible" : "hidden")}>{answer.status === "hidden" ? "恢复显示" : "隐藏"}</button>
                {answer.status === "visible" && answer.knowledge_review_state !== "published" && <button type="button" onClick={() => beginKnowledgeFromAnswer(question, answer)}>整理入库</button>}
                {answer.status === "visible" && answer.knowledge_review_state === "published" && <span className="governance-published-label">已发布入库</span>}
              </div>
            </div>)}</div>
          </article>
        ))}
        {availableAnswers.length === 0 && <p>当前没有可供入库审核的公开回答。</p>}
      </section>

      <section className="knowledge-governance-section">
        <header><div><p className="eyebrow">CONTRIBUTOR ACCOUNTS</p><h3>授权贡献者</h3></div><span>{contributors.length}</span></header>
        <div className="contributor-list">{contributors.map((item) => <div key={item.contributor_id}><Shield size={14} /><b>{item.public_name}</b><span>{item.unit || "未填写单位"}</span><small>{item.status}</small><button type="button" onClick={() => void toggleContributor(item)}>{item.status === "active" ? "停用" : "启用"}</button><button type="button" onClick={() => void resetContributor(item)}>重置密码</button></div>)}</div>
        <form className="governance-inline-form" onSubmit={createContributor}>
          <input placeholder="登录名" value={contributorDraft.username} onChange={(event) => setContributorDraft({ ...contributorDraft, username: event.target.value })} required />
          <input placeholder="初始密码" type="password" value={contributorDraft.password} onChange={(event) => setContributorDraft({ ...contributorDraft, password: event.target.value })} required minLength={6} />
          <input placeholder="公开展示名" value={contributorDraft.public_name} onChange={(event) => setContributorDraft({ ...contributorDraft, public_name: event.target.value })} required />
          <input placeholder="单位（可选）" value={contributorDraft.unit} onChange={(event) => setContributorDraft({ ...contributorDraft, unit: event.target.value })} />
          <button type="submit"><Plus size={14} />创建账号</button>
        </form>
      </section>

      <section className="knowledge-governance-section">
        <header><div><p className="eyebrow">CURATED ENTRIES</p><h3>人工知识条目</h3></div><span>{entries.length}</span></header>
        <form className="knowledge-entry-form" onSubmit={createKnowledge}>
          {editingId && <div className="governance-editing"><span>正在编辑已有条目</span><button type="button" onClick={cancelEdit}>取消编辑</button></div>}
          <input placeholder="规范标题" value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} required />
          <input placeholder="典型问题" value={draft.canonical_question} onChange={(event) => setDraft({ ...draft, canonical_question: event.target.value })} required />
          <textarea placeholder="事实回答（不会由优化功能改写）" value={draft.answer_markdown} onChange={(event) => setDraft({ ...draft, answer_markdown: event.target.value })} rows={4} required />
          <select value={draft.question_id ?? ""} onChange={(event) => setDraft({ ...draft, question_id: event.target.value || null })}><option value="">不关联问题（手工条目）</option>{questions.filter((question) => question.status !== "rejected" && question.status !== "hidden").map((question) => <option value={question.question_id} key={question.question_id}>{question.title}</option>)}</select>
          <div><input placeholder="分类" value={draft.category} onChange={(event) => setDraft({ ...draft, category: event.target.value })} /><input placeholder="维护单位" value={draft.maintainer_unit} onChange={(event) => setDraft({ ...draft, maintainer_unit: event.target.value })} /></div>
          <input placeholder="替代表达（用顿号、逗号或换行分隔）" value={draft.alternative_phrasings.join("、")} onChange={(event) => setDraft({ ...draft, alternative_phrasings: event.target.value.split(/[、,，\n]/).map((value) => value.trim()).filter(Boolean).slice(0, 12) })} />
          <textarea placeholder="适用范围（例如学院、年级或生效条件）" value={draft.applicable_scope} onChange={(event) => setDraft({ ...draft, applicable_scope: event.target.value })} rows={2} />
          <textarea placeholder="依据说明（人工核验依据、责任边界或复核提醒）" value={draft.basis_note} onChange={(event) => setDraft({ ...draft, basis_note: event.target.value })} rows={2} />
          <div><select value={draft.validity} onChange={(event) => setDraft({ ...draft, validity: event.target.value as KnowledgeDraft["validity"] })}><option value="stable">稳定</option><option value="time_bounded">时间限定</option></select><select value={draft.visibility} onChange={(event) => setDraft({ ...draft, visibility: event.target.value as KnowledgeDraft["visibility"] })}><option value="public">公开</option><option value="campus">校园</option></select></div>
          <label>纳入来源回答（可多选）<select multiple value={draft.origin_answer_ids} onChange={(event) => setDraft({ ...draft, origin_answer_ids: Array.from(event.target.selectedOptions, (option) => option.value) })}>{availableAnswers.map((answer) => <option value={answer.answer_id} key={answer.answer_id}>{answer.display_name} · {answer.questionTitle}</option>)}</select></label>
          {draft.validity === "time_bounded" && <div><label>生效时间<input type="datetime-local" value={draft.effective_from ? draft.effective_from.slice(0, 16) : ""} onChange={(event) => setDraft({ ...draft, effective_from: event.target.value ? new Date(event.target.value).toISOString() : null })} required /></label><label>失效时间<input type="datetime-local" value={draft.effective_to ? draft.effective_to.slice(0, 16) : ""} onChange={(event) => setDraft({ ...draft, effective_to: event.target.value ? new Date(event.target.value).toISOString() : null })} required /></label></div>}
          <button type="submit"><Plus size={14} />{editingId ? "更新草稿" : "保存草稿"}</button>
        </form>
        <div className="knowledge-entry-list">{entries.map((entry) => (
          <article key={entry.entry_id}>
            <header><div><b><a href={`/knowledge/${entry.entry_id}`}>{entry.title}</a></b><small>{entry.status} · {entry.visibility}</small></div><span>{entry.category}</span></header>
            <p>{entry.answer_markdown}</p>
            <footer><button type="button" onClick={() => beginEdit(entry)}>编辑</button><button type="button" onClick={() => void optimize(entry.entry_id)}><WandSparkles size={13} />优化检索表达</button>{entry.status !== "published" && entry.status !== "retired" && <button type="button" onClick={() => void publish(entry.entry_id)}>发布</button>}{entry.status === "published" && <button type="button" onClick={() => void retire(entry.entry_id)}>退休</button>}</footer>
            {optimization[entry.entry_id] && <aside>
              <b>建议标题：</b>{optimization[entry.entry_id].suggested_title}<br />
              <b>建议分类：</b>{optimization[entry.entry_id].suggested_category}<br />
              <b>检索表达：</b>{optimization[entry.entry_id].suggested_phrasings.join("、")}<br />
              <b>范围风险：</b>{optimization[entry.entry_id].scope_risk}
              {entry.status !== "retired" && <button type="button" onClick={() => applyOptimization(entry)}>应用到编辑器</button>}
            </aside>}
          </article>
        ))}</div>
      </section>
    </section>
  );
}
