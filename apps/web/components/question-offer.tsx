"use client";

import { useState } from "react";

import { createQuestionFromAnswer } from "@/lib/api";
import type { QuestionOffer } from "@/lib/api";

type QuestionOfferPanelProps = {
  answerId: string;
  offer: QuestionOffer;
};

export function QuestionOfferPanel({ answerId, offer }: QuestionOfferPanelProps) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState(offer.title);
  const [details, setDetails] = useState(offer.details);
  const [confirmed, setConfirmed] = useState(false);
  const [submitted, setSubmitted] = useState(
    Boolean(
      offer.existing_question_id &&
        offer.existing_status !== "rejected",
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (submitted) {
    return (
      <aside className="question-offer question-offer-submitted">
        <b>这个问题已经进入悬赏审核</b>
        <p>管理员审核通过后，会出现在问题广场，供授权贡献者继续核对。</p>
      </aside>
    );
  }

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await createQuestionFromAnswer(answerId, { title, details });
      setSubmitted(true);
      setOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交失败，请稍后再试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="question-offer">
      <div>
        <b>这次证据还不够完整</b>
        <p>{offer.evidence_gap}</p>
      </div>
      <button type="button" onClick={() => setOpen(true)}>
        去悬赏提问
      </button>
      {open && (
        <div className="question-offer-dialog" role="dialog" aria-modal="true">
          <div className="question-offer-card">
            <header>
              <div>
                <span>COMMUNITY QUESTION</span>
                <h3>把缺口交给问题广场</h3>
              </div>
              <button type="button" onClick={() => setOpen(false)} aria-label="关闭">
                ×
              </button>
            </header>
            <p className="question-offer-note">
              提交后先由管理员审核，公开问题不会显示你的登录信息。
            </p>
            <label>
              标题
              <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={240} />
            </label>
            <label>
              详情
              <textarea value={details} onChange={(event) => setDetails(event.target.value)} rows={6} maxLength={6000} />
            </label>
            <label className="question-offer-confirm">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              <span>我确认这是真实的校园信息缺口，并了解问题会先经过管理员审核后公开。</span>
            </label>
            {error && <p className="form-error">{error}</p>}
            <footer>
              <button type="button" onClick={() => setOpen(false)}>取消</button>
              <button type="button" disabled={busy || !confirmed || title.trim().length < 2 || details.trim().length < 2} onClick={() => void submit()}>
                {busy ? "提交中…" : "提交审核"}
              </button>
            </footer>
          </div>
        </div>
      )}
    </aside>
  );
}
