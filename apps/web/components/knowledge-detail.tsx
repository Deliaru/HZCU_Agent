"use client";

import { ArrowLeft, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

import { getKnowledgeEntry } from "@/lib/api";
import type { KnowledgeEntry } from "@/lib/api";

import { AppChrome } from "./app-chrome";
import { useTheme } from "./theme-provider";

export function KnowledgeDetail({ entryId }: { entryId: string }) {
  const { theme } = useTheme();
  const [entry, setEntry] = useState<KnowledgeEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const congyu = theme === "character";

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const next = await getKnowledgeEntry(entryId);
        if (!cancelled) setEntry(next);
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "人工知识条目暂时无法读取");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [entryId]);

  return (
    <AppChrome
      section="sources"
      className={congyu ? "knowledge-detail-shell knowledge-detail-shell-congyu" : "knowledge-detail-shell"}
      channel={congyu ? "CONGYU // CURATED KNOWLEDGE" : "HZCU // CURATED KNOWLEDGE"}
      mode="HUMAN-REVIEWED / NOT OFFICIAL"
      eyebrow={congyu ? "03 / 人工核验资料" : "03 / CURATED KNOWLEDGE"}
      title={congyu ? "人工核验资料" : "人工知识条目"}
      utilities={<a className="back-to-agent" href="/sources">返回来源账本</a>}
    >
      <main className="knowledge-detail-content">
        <a className="questions-back" href="/sources"><ArrowLeft size={15} />返回来源账本</a>
        {loading && <div className="questions-loading"><LoaderCircle className="spin" size={20} />正在读取人工知识</div>}
        {error && <div className="form-error" role="alert">{error}</div>}
        {entry && !loading && (
          <article className="knowledge-public-card">
            <header>
              <span className="knowledge-public-badge">Agent 人工核验资料 · 非学校官方材料</span>
              <h1>{entry.title}</h1>
              <p>{entry.canonical_question}</p>
            </header>
            <section className="knowledge-public-answer"><ReactMarkdown>{entry.answer_markdown}</ReactMarkdown></section>
            <dl className="knowledge-public-meta">
              <div><dt>分类</dt><dd>{entry.category}</dd></div>
              <div><dt>维护单位</dt><dd>{entry.maintainer_unit || "未填写"}</dd></div>
              <div><dt>适用范围</dt><dd>{entry.applicable_scope || "未填写"}</dd></div>
              <div><dt>资料属性</dt><dd>{entry.validity === "time_bounded" ? "时间限定" : "稳定条目"}</dd></div>
              {entry.validity === "time_bounded" && (
                <div>
                  <dt>生效范围</dt>
                  <dd>
                    {entry.effective_from ? new Date(entry.effective_from).toLocaleString("zh-CN") : "未填写"}
                    {" — "}
                    {entry.effective_to ? new Date(entry.effective_to).toLocaleString("zh-CN") : "未填写"}
                  </dd>
                </div>
              )}
            </dl>
            {entry.basis_note && <p className="knowledge-public-basis">依据说明：{entry.basis_note}</p>}
          </article>
        )}
      </main>
    </AppChrome>
  );
}
