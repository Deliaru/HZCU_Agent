"use client";

import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  Check,
  Clock3,
  Feather,
  GitCompareArrows,
  History,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";

import type {
  AuthSession,
  DocumentVersion,
  SourceAlert,
  SourceResource,
  SourceStatus,
  VersionComparison,
} from "@/lib/api";

import { CongyuArtwork } from "./congyu-artwork";
import { IdentityControl } from "./identity-control";

type Props = {
  sources: SourceStatus[];
  selected?: SourceStatus;
  selectedId?: string;
  resources: SourceResource[];
  selectedResource?: SourceResource;
  selectedResourceId?: string;
  versions: DocumentVersion[];
  compareFromId?: string;
  comparison?: VersionComparison;
  alerts: SourceAlert[];
  loading: boolean;
  resourceLoading: boolean;
  historyLoading: boolean;
  error?: string;
  authSession: AuthSession | null;
  authBusy: boolean;
  onSelectSource: (id: string) => void;
  onSelectResource: (id?: string) => void;
  onCompare: (id: string) => void;
  onRefresh: () => void;
  onLogout: () => void;
  onAuthenticated: () => void;
};

function time(value: string | null) {
  if (!value) return "等待同步";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

function health(source: SourceStatus) {
  if (source.health_state === "healthy") return "同步正常";
  if (source.health_state === "waiting") return "等待首轮同步";
  if (source.health_state === "stale") return "资料可能过期";
  if (source.health_state === "disabled") return "已停用";
  return "需要留意";
}

export function CongyuSourceLibrary(props: Props) {
  const totals = {
    resources: props.sources.reduce((sum, source) => sum + source.resource_count, 0),
    versions: props.sources.reduce((sum, source) => sum + source.version_count, 0),
    healthy: props.sources.filter((source) => source.health_state === "healthy").length,
  };

  return (
    <main className="congyu-library">
      <div className="congyu-library-sky" aria-hidden="true"><i /><i /><i /></div>
      <header className="congyu-library-nav">
        <a href="/" className="congyu-library-brand"><Feather size={20} /><span><b>琮羽资料馆</b><small>CONGYU CAMPUS ARCHIVE</small></span></a>
        <a href="/"><ArrowLeft size={15} /> 返回调查室</a>
        <IdentityControl session={props.authSession} busy={props.authBusy} onLogout={props.onLogout} onAuthenticated={props.onAuthenticated} />
      </header>

      <section className="congyu-library-hero">
        <div>
          <p><span>02</span> OFFICIAL MATERIAL ARCHIVE</p>
          <h1>琮羽<br /><em>资料馆</em></h1>
          <blockquote>“我会告诉你答案从哪一页来，也会保留资料变化过的痕迹。”</blockquote>
          <div className="congyu-library-counts">
            <span><b>{props.sources.length}</b>来源目录</span><span><b>{totals.resources}</b>当前资料</span><span><b>{totals.versions}</b>历史版本</span><span><b>{totals.healthy}</b>通道正常</span>
          </div>
        </div>
        <CongyuArtwork scene="library" sizes="min(44vw, 520px)" />
      </section>

      <section className="congyu-archive-desk">
        <aside className="congyu-archive-index">
          <header><div><p>CATALOG INDEX</p><h2>来源目录</h2></div><button type="button" onClick={props.onRefresh} aria-label="刷新目录"><RefreshCw size={16} /></button></header>
          {props.loading ? <div className="congyu-library-loading"><LoaderCircle className="spin" size={18} />正在打开资料馆</div> : props.sources.map((source, index) => (
            <button type="button" key={source.source_id} className={props.selectedId === source.source_id ? "active" : ""} onClick={() => props.onSelectSource(source.source_id)}>
              <i>{String(index + 1).padStart(2, "0")}</i><span><b>{source.name}</b><small>{source.owner_department}</small></span><em className={`health-${source.health_state}`}><Check size={11} />{health(source)}</em>
            </button>
          ))}
          {props.alerts.length > 0 && <div className="congyu-archive-alert"><AlertTriangle size={15} /><span><b>{props.alerts.length} 条资料提醒</b><small>部分来源需要管理员复核</small></span></div>}
        </aside>

        <section className="congyu-archive-pages">
          {props.error ? <div className="congyu-library-error"><AlertTriangle size={17} />{props.error}</div> : null}
          {props.selected ? (
            <>
              <header className="congyu-source-cover">
                <div><p>ARCHIVE / {props.selected.source_id}</p><h2>{props.selected.name}</h2><span>{props.selected.owner_department}</span></div>
                <a href={props.selected.base_url} target="_blank" rel="noreferrer">官方入口 <ArrowUpRight size={15} /></a>
              </header>
              <div className="congyu-source-facts">
                <span><ShieldCheck size={16} /><small>来源状态</small><b>{health(props.selected)}</b></span>
                <span><Clock3 size={16} /><small>最近同步</small><b>{time(props.selected.last_success_at)}</b></span>
                <span><BookOpen size={16} /><small>语义分块</small><b>{props.selected.chunk_count}</b></span>
                <span><History size={16} /><small>不可变版本</small><b>{props.selected.version_count}</b></span>
              </div>
              <div className="congyu-resource-register">
                <header><div><p>CURRENT MATERIAL REGISTER</p><h3>当前资料页</h3></div><span>{props.selected.resource_count} 份</span></header>
                {props.resourceLoading ? <div className="congyu-library-loading"><LoaderCircle className="spin" size={18} />正在整理索引</div> : props.resources.length === 0 ? <p className="congyu-library-empty">目录已经登记，等待取得首批资料。</p> : props.resources.map((resource, index) => (
                  <div className={`congyu-resource-row ${props.selectedResourceId === resource.resource_id ? "active" : ""}`} key={resource.resource_id}>
                    <button type="button" onClick={() => props.onSelectResource(resource.resource_id)}><i>{String(index + 1).padStart(2, "0")}</i><span><b>{resource.title ?? resource.canonical_uri}</b><small>{resource.version_count} 个版本 · {resource.chunk_count} 个分块</small></span><History size={14} /></button>
                    <a href={resource.canonical_uri} target="_blank" rel="noreferrer" aria-label={`打开${resource.title ?? "官方资料"}`}><ArrowUpRight size={14} /></a>
                  </div>
                ))}
              </div>
              {props.selectedResource && (
                <section className="congyu-version-page">
                  <header><div><p>TEMPORAL PAPER TRAIL</p><h3>资料版本与变化</h3><span>{props.selectedResource.title ?? props.selectedResource.canonical_uri}</span></div><button type="button" onClick={() => props.onSelectResource(undefined)}><X size={16} /></button></header>
                  {props.historyLoading && props.versions.length === 0 ? <div className="congyu-library-loading"><LoaderCircle className="spin" size={18} />正在翻阅历史页</div> : <div className="congyu-version-grid">
                    <nav>{props.versions.map((version, index) => <button type="button" key={version.version_id} className={props.compareFromId === version.version_id ? "active" : ""} onClick={() => props.onCompare(version.version_id)}><i>V{String(props.versions.length-index).padStart(2,"0")}</i><span><b>{version.is_current ? "当前版本" : "历史版本"}</b><small>{time(version.observed_at)} · {version.chunk_count} 分块</small></span></button>)}</nav>
                    <article><header><GitCompareArrows size={17} /><span><b>STRUCTURAL DIFF</b>{props.comparison?.changed ? "检测到内容变化" : "所选版本内容一致"}</span></header><pre>{props.historyLoading ? "正在计算版本差异…" : props.comparison?.unified_diff || (props.comparison?.changed ? "资料结构或内容哈希发生了变化。" : "当前暂无可显示的逐行差异。")}</pre></article>
                  </div>}
                </section>
              )}
            </>
          ) : <div className="congyu-library-empty"><BookOpen size={24} />从左侧选择一本来源目录。</div>}
        </section>
      </section>
    </main>
  );
}
