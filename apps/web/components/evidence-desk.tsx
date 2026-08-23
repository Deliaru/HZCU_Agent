"use client";

import {
  BookOpenText,
  ExternalLink,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";

import type { Evidence } from "@/lib/api";

type Props = {
  evidence: Evidence[];
  active: Evidence | null;
  open: boolean;
  stage?: string;
  onSelect: (evidence: Evidence) => void;
  onClose: () => void;
};

function formatDate(value: string | null): string {
  if (!value) return "页面未标注日期";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

export function EvidenceDesk({
  evidence = [],
  active,
  open,
  stage = "idle",
  onSelect,
  onClose,
}: Props) {
  const audienceScopes = active?.audience_scopes ?? [];
  return (
    <aside
      className={`evidence-desk evidence-stage-${stage} ${
        evidence.length ? "evidence-populated" : ""
      } ${open ? "evidence-open" : ""}`}
      aria-label="官方证据"
    >
      <div className="desk-heading">
        <div>
          <p className="eyebrow">EVIDENCE DESK</p>
          <h2>官方依据</h2>
        </div>
        <span>
          <small>REC</small>
          {evidence.length.toString().padStart(2, "0")}
        </span>
        <button
          className="desk-close"
          type="button"
          onClick={onClose}
          aria-label="关闭证据面板"
        >
          <X size={18} />
        </button>
      </div>

      {active ? (
        <article className="evidence-focus">
          <div className="document-icon">
            <BookOpenText size={20} />
          </div>
          <p className="evidence-source">{active.publisher}</p>
          <h3>{active.title}</h3>
          <div className="evidence-badges">
            <span>
              {active.retrieval_mode === "memory" ? "镜像" : "实时"}
            </span>
            <span>
              {active.authority_level === "official"
                ? "官方"
                : active.authority_level === "official_secondary"
                  ? "官方关联"
                  : "已登记"}
            </span>
            <span>{formatDate(active.observed_at)} 核验</span>
          </div>
          {audienceScopes.length > 0 ? (
            <p className="evidence-date">
              适用范围 {audienceScopes.join(" · ")}
            </p>
          ) : null}
          <p className="evidence-date">
            发布 {formatDate(active.published_at)}
          </p>
          <p className="evidence-excerpt">{active.excerpt}</p>
          <a href={active.canonical_url} target="_blank" rel="noreferrer">
            查看官方原文 <ExternalLink size={14} />
          </a>
        </article>
      ) : (
        <div className="evidence-empty">
          <div className="radar">
            <Search size={22} />
            <i />
            <i />
          </div>
          <h3>证据会在调查时出现</h3>
          <p>所有校园事实必须指向本次工作区里的官方材料。</p>
        </div>
      )}

      {evidence.length > 0 && (
        <div className="evidence-index">
          {evidence.map((item, index) => (
            <button
              id={`evidence-${index + 1}`}
              type="button"
              key={item.evidence_id}
              className={active?.evidence_id === item.evidence_id ? "active" : ""}
              onClick={() => onSelect(item)}
            >
              <span>{(index + 1).toString().padStart(2, "0")}</span>
              <p>{item.title}</p>
              <ExternalLink size={13} />
            </button>
          ))}
        </div>
      )}

      <div className="desk-footnote">
        <ShieldCheck size={14} />
        <span>只展示当前身份可见的来源与正文</span>
      </div>
    </aside>
  );
}
