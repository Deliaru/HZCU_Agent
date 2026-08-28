"use client";

import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpRight,
  BookOpenText,
  Braces,
  Clock3,
  DatabaseZap,
  FileClock,
  GitCompareArrows,
  History,
  Layers3,
  LoaderCircle,
  RadioTower,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";

import {
  compareResourceVersions,
  getAuthSession,
  getResourceVersions,
  getSourceAlerts,
  getSourceResources,
  getSources,
  logoutCampusSession,
} from "@/lib/api";
import type {
  AuthSession,
  DocumentVersion,
  SourceAlert,
  SourceResource,
  SourceStatus,
  VersionComparison,
} from "@/lib/api";

import { AppChrome } from "./app-chrome";
import { CongyuSourceLibrary } from "./congyu-source-library";
import { IdentityControl } from "./identity-control";
import { useTheme } from "./theme-provider";

function formatDateTime(value: string | null): string {
  if (!value) return "尚未同步";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function intervalLabel(seconds: number): string {
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时`;
  return `${Math.round(seconds / 86400)} 天`;
}

function sourceState(source: SourceStatus): {
  label: string;
  tone: "ready" | "waiting" | "warning";
} {
  if (source.health_state === "healthy") {
    return { label: "同步正常", tone: "ready" };
  }
  if (source.health_state === "waiting") {
    return { label: "等待首轮同步", tone: "waiting" };
  }
  if (source.health_state === "stale") {
    return { label: "已超同步窗口", tone: "warning" };
  }
  if (source.health_state === "failing") {
    return { label: `连续失败 ${source.consecutive_failures}`, tone: "warning" };
  }
  if (source.health_state === "disabled") {
    return { label: "已停用", tone: "waiting" };
  }
  return { label: "部分能力降级", tone: "warning" };
}

function entityLabel(value: string | null): string {
  return (
    {
      notice: "通知",
      policy: "政策",
      event: "活动",
      course: "课程",
      competition: "竞赛",
      document: "文档",
    }[value ?? ""] ?? value ?? "待抽取"
  );
}

function statusLabel(value: string | null): string {
  return (
    {
      open: "进行中",
      upcoming: "即将开始",
      closed: "已结束",
      current: "现行",
      superseded: "已被替代",
      expired: "已失效",
      cancelled: "已取消",
      postponed: "已延期",
      unknown: "状态待核验",
    }[value ?? ""] ?? value ?? "状态待核验"
  );
}

function useSourceObservatoryController() {
  const [sources, setSources] = useState<SourceStatus[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [resources, setResources] = useState<SourceResource[]>([]);
  const [alerts, setAlerts] = useState<SourceAlert[]>([]);
  const [selectedResourceId, setSelectedResourceId] = useState<string>();
  const [versions, setVersions] = useState<DocumentVersion[]>([]);
  const [compareFromId, setCompareFromId] = useState<string>();
  const [comparison, setComparison] = useState<VersionComparison>();
  const [loading, setLoading] = useState(true);
  const [resourceLoading, setResourceLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  const [error, setError] = useState<string>();

  const loadSources = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    try {
      const result = await getSources();
      setSources(result);
      setAlerts(await getSourceAlerts().catch(() => []));
      setSelectedId((current) =>
        current && result.some((source) => source.source_id === current)
          ? current
          : result[0]?.source_id,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "来源状态读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSources();
    getAuthSession().then(setAuthSession).catch(() => setAuthSession(null));
  }, [loadSources]);

  const handleLogout = useCallback(async () => {
    if (authBusy) return;
    setAuthBusy(true);
    setError(undefined);
    try {
      await logoutCampusSession();
      setAuthSession(await getAuthSession());
      await loadSources();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "退出登录失败");
    } finally {
      setAuthBusy(false);
    }
  }, [authBusy, loadSources]);

  useEffect(() => {
    setSelectedResourceId(undefined);
    setVersions([]);
    setComparison(undefined);
    if (!selectedId) {
      setResources([]);
      return;
    }
    let cancelled = false;
    setResourceLoading(true);
    getSourceResources(selectedId)
      .then((result) => {
        if (!cancelled) setResources(result);
      })
      .catch(() => {
        if (!cancelled) setResources([]);
      })
      .finally(() => {
        if (!cancelled) setResourceLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId || !selectedResourceId) {
      setVersions([]);
      setComparison(undefined);
      return;
    }
    let cancelled = false;
    setHistoryLoading(true);
    getResourceVersions(selectedId, selectedResourceId)
      .then(async (result) => {
        if (cancelled) return;
        setVersions(result);
        const current = result.find((version) => version.is_current) ?? result[0];
        const previous =
          result.find((version) => version.version_id !== current?.version_id) ??
          current;
        setCompareFromId(previous?.version_id);
        if (current && previous) {
          const nextComparison = await compareResourceVersions(
            selectedId,
            selectedResourceId,
            previous.version_id,
            current.version_id,
          );
          if (!cancelled) setComparison(nextComparison);
        } else {
          setComparison(undefined);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setVersions([]);
          setComparison(undefined);
        }
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, selectedResourceId]);

  const selectComparisonBase = useCallback(
    async (versionId: string) => {
      if (!selectedId || !selectedResourceId) return;
      const current = versions.find((version) => version.is_current) ?? versions[0];
      if (!current) return;
      setCompareFromId(versionId);
      setHistoryLoading(true);
      try {
        setComparison(
          await compareResourceVersions(
            selectedId,
            selectedResourceId,
            versionId,
            current.version_id,
          ),
        );
      } catch {
        setComparison(undefined);
      } finally {
        setHistoryLoading(false);
      }
    },
    [selectedId, selectedResourceId, versions],
  );

  const selected = sources.find((source) => source.source_id === selectedId);
  const totals = useMemo(
    () => ({
      resources: sources.reduce((sum, source) => sum + source.resource_count, 0),
      versions: sources.reduce((sum, source) => sum + source.version_count, 0),
      healthy: sources.filter(
        (source) => source.health_state === "healthy",
      ).length,
      chunks: sources.reduce((sum, source) => sum + source.chunk_count, 0),
      entities: sources.reduce((sum, source) => sum + source.entity_count, 0),
    }),
    [sources],
  );
  const selectedResource = resources.find(
    (resource) => resource.resource_id === selectedResourceId,
  );

  return {
    sources,
    selectedId,
    resources,
    alerts,
    selectedResourceId,
    versions,
    compareFromId,
    comparison,
    loading,
    resourceLoading,
    historyLoading,
    authSession,
    authBusy,
    error,
    selected,
    totals,
    selectedResource,
    setAuthSession,
    setSelectedId,
    setSelectedResourceId,
    loadSources,
    handleLogout,
    selectComparisonBase,
  };
}

export function SourceObservatory() {
  const { theme } = useTheme();
  const {
    sources,
    selectedId,
    resources,
    alerts,
    selectedResourceId,
    versions,
    compareFromId,
    comparison,
    loading,
    resourceLoading,
    historyLoading,
    authSession,
    authBusy,
    error,
    selected,
    totals,
    selectedResource,
    setAuthSession,
    setSelectedId,
    setSelectedResourceId,
    loadSources,
    handleLogout,
    selectComparisonBase,
  } = useSourceObservatoryController();

  if (theme === "character") {
    return (
      <CongyuSourceLibrary
        sources={sources}
        selected={selected}
        selectedId={selectedId}
        resources={resources}
        selectedResource={selectedResource}
        selectedResourceId={selectedResourceId}
        versions={versions}
        compareFromId={compareFromId}
        comparison={comparison}
        alerts={alerts}
        loading={loading}
        resourceLoading={resourceLoading}
        historyLoading={historyLoading}
        error={error}
        authSession={authSession}
        authBusy={authBusy}
        onSelectSource={setSelectedId}
        onSelectResource={setSelectedResourceId}
        onCompare={(versionId) => void selectComparisonBase(versionId)}
        onRefresh={() => void loadSources()}
        onLogout={() => void handleLogout()}
        onAuthenticated={() => void loadSources()}
      />
    );
  }

  return (
    <AppChrome
      section="sources"
      className="sources-shell"
      channel="HZCU // SOURCE SIGNAL"
      mode={`${authSession?.authenticated ? "CAMPUS + PUBLIC" : "PUBLIC"} / READ ONLY`}
      eyebrow="02 / SOURCE REGISTRY"
      title="来源透明账本"
      utilities={
        <>
          <span className="mast-code">
            SOURCE OBSERVATORY / {sources.length.toString().padStart(3, "0")}
          </span>
          <a className="back-to-agent" href="/">
            <ArrowLeft size={14} />
            返回提问
          </a>
          <IdentityControl
            session={authSession}
            busy={authBusy}
            onLogout={() => void handleLogout()}
            onAuthenticated={(session) => {
              setAuthSession(session);
              void loadSources();
            }}
          />
        </>
      }
    >

      <section className="registry-hero">
        <div>
          <p className="eyebrow">SOURCE OBSERVATORY / 公开透明</p>
          <h1><span>校园信息</span><br /><em>不是黑箱。</em></h1>
        </div>
        <p>
          每条校园事实都应说得清从哪里来、什么时候看见、当前是哪一版。
          这里展示当前身份或 Pilot 本地镜像授权可见的官方来源与同步状态，
          不展示内部快照或凭据。
        </p>
        <div className="registry-hero-index" aria-hidden="true">
          <b>{sources.length.toString().padStart(2, "0")}</b>
          <span>REGISTERED<br />CHANNELS</span>
        </div>
      </section>

      <section className="registry-metrics" aria-label="来源汇总">
        <div>
          <span>01 / REGISTERED</span>
          <strong>{sources.length.toString().padStart(2, "0")}</strong>
          <p>当前身份可见来源</p>
        </div>
        <div>
          <span>02 / INDEX UNITS</span>
          <strong>{totals.chunks.toString().padStart(2, "0")}</strong>
          <p>语义分块索引</p>
        </div>
        <div>
          <span>03 / ENTITIES</span>
          <strong>{totals.entities.toString().padStart(2, "0")}</strong>
          <p>结构化校园实体</p>
        </div>
        <div>
          <span>04 / HEALTHY</span>
          <strong>{totals.healthy}/{sources.length || 0}</strong>
          <p>最近同步正常</p>
        </div>
      </section>

      {alerts.length > 0 && (
        <section className="source-alert-strip" aria-label="来源新鲜度告警">
          <div>
            <AlertTriangle size={17} />
            <span>
              <b>{alerts.length.toString().padStart(2, "0")} ACTIVE SIGNALS</b>
              来源异常不会被解释成“没有校园信息”
            </span>
          </div>
          <div className="source-alert-list">
            {alerts.slice(0, 4).map((alert) => (
              <span className={alert.severity} key={`${alert.source_id}-${alert.code}`}>
                {alert.source_name} · {alert.message}
              </span>
            ))}
          </div>
        </section>
      )}

      {error && (
        <div className="registry-error">
          <RadioTower size={18} />
          <span>
            <b>暂时读不到来源账本</b>
            {error}
          </span>
          <button type="button" onClick={() => void loadSources()}>
            重试
          </button>
        </div>
      )}

      <section className="registry-workspace">
        <div className="registry-ledger">
          <div className="ledger-heading">
            <div>
              <p className="eyebrow">REGISTERED CHANNELS</p>
              <h2>来源目录</h2>
            </div>
            <button
              type="button"
              aria-label="刷新来源状态"
              onClick={() => void loadSources()}
              disabled={loading}
            >
              <RefreshCw className={loading ? "spin" : ""} size={15} />
            </button>
          </div>

          {loading && sources.length === 0 ? (
            <div className="ledger-loading">
              <LoaderCircle className="spin" size={19} />
              正在读取可见来源状态
            </div>
          ) : (
            <div className="ledger-list">
              {sources.map((source, index) => {
                const state = sourceState(source);
                return (
                  <button
                    type="button"
                    key={source.source_id}
                    className={selectedId === source.source_id ? "active" : ""}
                    onClick={() => setSelectedId(source.source_id)}
                    style={
                      { "--ledger-delay": `${index * 65}ms` } as CSSProperties
                    }
                  >
                    <span className="ledger-index">
                      {(index + 1).toString().padStart(2, "0")}
                    </span>
                    <span className="ledger-name">
                      <b>{source.name}</b>
                      <small>{source.owner_department}</small>
                    </span>
                    <span className={`ledger-state ${state.tone}`}>
                      <i />
                      {state.label}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="registry-detail">
          {selected ? (
            <>
              <div className="detail-title">
                <div>
                  <span>{selected.source_id}</span>
                  <h2>{selected.name}</h2>
                  <p>{selected.owner_department}</p>
                </div>
                <a href={selected.base_url} target="_blank" rel="noreferrer">
                  打开官方入口
                  <ArrowUpRight size={15} />
                </a>
              </div>

              <div className="detail-specs">
                <div>
                  <DatabaseZap size={17} />
                  <span><small>连接器</small>{selected.connector_kind}</span>
                </div>
                <div>
                  <Clock3 size={17} />
                  <span>
                    <small>检查间隔</small>
                    {intervalLabel(selected.poll_interval_seconds)}
                  </span>
                </div>
                <div>
                  <ShieldCheck size={17} />
                  <span>
                    <small>来源健康</small>
                    {sourceState(selected).label}
                  </span>
                </div>
                <div>
                  <FileClock size={17} />
                  <span>
                    <small>最近成功</small>
                    {formatDateTime(selected.last_success_at)}
                  </span>
                </div>
              </div>

              <div className="index-stamps">
                <span>
                  <Layers3 size={13} />
                  {selected.chunk_count} 语义分块
                </span>
                <span>
                  <Braces size={13} />
                  {selected.entity_count} 结构化实体
                </span>
                <span>
                  <History size={13} />
                  {selected.version_count} 个不可变版本
                </span>
                <span>
                  <AlertTriangle size={13} />
                  新鲜至 {formatDateTime(selected.fresh_until)}
                </span>
              </div>

              <div className="host-stamps">
                <p>EXACT HTTPS HOSTS</p>
                <div>
                  {selected.allowed_hosts.map((host) => (
                    <span key={host}><ShieldCheck size={12} /> {host}</span>
                  ))}
                </div>
              </div>

              <div className="resource-register">
                <div className="resource-heading">
                  <div>
                    <p className="eyebrow">CURRENT VERSION REGISTER</p>
                    <h3>最近观察到的资源</h3>
                  </div>
                  <span>{selected.resource_count} 资源 / {selected.version_count} 版本</span>
                </div>

                {resourceLoading ? (
                  <div className="resource-empty">
                    <LoaderCircle className="spin" size={18} />
                    正在读取当前版本
                  </div>
                ) : resources.length ? (
                  <div className="resource-list">
                    {resources.map((resource, index) => (
                      <div
                        className={`resource-row ${
                          selectedResourceId === resource.resource_id ? "active" : ""
                        }`}
                        key={resource.resource_id}
                      >
                        <button
                          type="button"
                          onClick={() => setSelectedResourceId(resource.resource_id)}
                        >
                          <span>{(index + 1).toString().padStart(2, "0")}</span>
                          <div>
                            <b>{resource.title ?? resource.canonical_uri}</b>
                            <small>
                              {entityLabel(resource.entity_type)} ·{" "}
                              {statusLabel(resource.entity_status)} ·{" "}
                              {resource.version_count} 版本 · {resource.chunk_count} 分块
                            </small>
                            {resource.deadline_at && (
                              <em>截止 {formatDateTime(resource.deadline_at)}</em>
                            )}
                          </div>
                          <History size={15} />
                        </button>
                        <a
                          href={resource.canonical_uri}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={`打开官方原文：${resource.title ?? resource.canonical_uri}`}
                        >
                          <BookOpenText size={15} />
                        </a>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="resource-empty">
                    <Braces size={20} />
                    该来源已登记，等待同步 Worker 取得首批版本
                  </div>
                )}
              </div>

              {selectedResource && (
                <section className="version-workbench">
                  <div className="version-heading">
                    <div>
                      <p className="eyebrow">TEMPORAL VERSION LEDGER</p>
                      <h3>版本历史与结构差异</h3>
                      <span>{selectedResource.title ?? selectedResource.canonical_uri}</span>
                    </div>
                    <button
                      type="button"
                      aria-label="关闭版本历史"
                      onClick={() => setSelectedResourceId(undefined)}
                    >
                      <X size={15} />
                    </button>
                  </div>

                  {historyLoading && versions.length === 0 ? (
                    <div className="resource-empty">
                      <LoaderCircle className="spin" size={18} />
                      正在读取不可变版本账本
                    </div>
                  ) : (
                    <div className="version-grid">
                      <div className="version-timeline">
                        <p>选择一个历史版本，与当前版本比较</p>
                        {versions.map((version, index) => {
                          const entity = version.entities[0];
                          return (
                            <button
                              type="button"
                              className={
                                compareFromId === version.version_id ? "active" : ""
                              }
                              key={version.version_id}
                              onClick={() =>
                                void selectComparisonBase(version.version_id)
                              }
                            >
                              <span>
                                V{(versions.length - index).toString().padStart(2, "0")}
                              </span>
                              <div>
                                <b>
                                  {version.is_current ? "CURRENT / 当前版本" : "HISTORY"}
                                </b>
                                <small>
                                  观察于 {formatDateTime(version.observed_at)} ·{" "}
                                  {version.chunk_count} 分块
                                </small>
                                {entity && (
                                  <em>
                                    {entityLabel(entity.entity_type)} /{" "}
                                    {statusLabel(entity.status)}
                                  </em>
                                )}
                              </div>
                            </button>
                          );
                        })}
                      </div>

                      <div className="version-diff">
                        <div className="diff-title">
                          <GitCompareArrows size={17} />
                          <span>
                            <b>STRUCTURAL DIFF</b>
                            {comparison?.changed ? "检测到内容变化" : "所选版本内容一致"}
                          </span>
                        </div>

                        {versions[0]?.entities[0] && (
                          <div className="entity-readout">
                            <div>
                              <small>实体 / 状态</small>
                              <b>
                                {entityLabel(versions[0].entities[0].entity_type)} ·{" "}
                                {statusLabel(versions[0].entities[0].status)}
                              </b>
                            </div>
                            <div>
                              <small>截止时间</small>
                              <b>
                                {versions[0].entities[0].deadline_at
                                  ? formatDateTime(
                                      versions[0].entities[0].deadline_at,
                                    )
                                  : "未识别截止时间"}
                              </b>
                            </div>
                            <div>
                              <small>适用对象</small>
                              <b>
                                {versions[0].entities[0].audience_scopes
                                  .slice(0, 3)
                                  .join(" / ") || "待核验"}
                              </b>
                            </div>
                          </div>
                        )}

                        {comparison &&
                          Object.keys(comparison.structured_changes).length > 0 && (
                            <div className="structured-changes">
                              {Object.keys(comparison.structured_changes).map((field) => (
                                <span key={field}>{field} changed</span>
                              ))}
                            </div>
                          )}

                        <pre>
                          {historyLoading
                            ? "正在计算版本差异…"
                            : comparison?.unified_diff ||
                              (comparison?.changed
                                ? "内容哈希、解析版本或结构字段发生变化；规范化正文没有可显示的逐行差异。"
                                : "当前仅有一个版本；后续同 URL 内容变化时将在这里显示差异。")}
                        </pre>
                      </div>
                    </div>
                  )}
                </section>
              )}
            </>
          ) : (
            <div className="registry-no-selection">
              <DatabaseZap size={26} />
              选择一个来源查看版本账本
            </div>
          )}
        </div>
      </section>

      <footer className="registry-principle">
        <span>事实原则 05</span>
        <p>
          知识库只是校园世界的时间记忆，不是 Agent 的大脑。
          模型仍需理解原问题、组合工具，并在时效敏感时实时核验。
        </p>
      </footer>
    </AppChrome>
  );
}
