"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { type Announcement, getAdminAnnouncements, publishAnnouncement, withdrawAnnouncement } from "@/lib/api";

export function AnnouncementAdmin() {
  const [items, setItems] = useState<Announcement[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(async () => {
    try { setItems(await getAdminAnnouncements()); }
    catch { setError("公告列表加载失败，请刷新重试。"); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function publish(event: FormEvent) {
    event.preventDefault();
    if (busy || !title.trim() || !content.trim()) return;
    if (!window.confirm("发布后，接受隐私声明的用户进站将看到此公告。确认发布？")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await publishAnnouncement(title.trim(), content.trim());
      setTitle(""); setContent(""); setNotice("公告已发布。"); await load();
    } catch { setError("发布失败，请重试。"); }
    finally { setBusy(false); }
  }

  async function withdraw(item: Announcement) {
    if (busy || !window.confirm(`撤回“${item.title}”？撤回后不再向进站用户展示。`)) return;
    setBusy(true); setError(""); setNotice("");
    try { await withdrawAnnouncement(item.id); setNotice("公告已撤回。"); await load(); }
    catch { setError("撤回失败，请重试。"); }
    finally { setBusy(false); }
  }

  return <section className="announcement-admin" aria-label="公告管理">
    <h2>公告管理</h2>
    <p>公告以纯文本发布，用户接受隐私声明后逐条确认已读。已发布内容不可编辑；需要更正时，请撤回并发布新公告。</p>
    <form onSubmit={publish}>
      <label>标题<input required maxLength={160} value={title} disabled={busy} onChange={(e) => setTitle(e.target.value)} /></label>
      <label>正文<textarea required maxLength={20000} rows={8} value={content} disabled={busy} onChange={(e) => setContent(e.target.value)} /></label>
      <button disabled={busy || !title.trim() || !content.trim()}>{busy ? "处理中…" : "发布公告"}</button>
    </form>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <h3>最近发布（最多 200 条）</h3>
    <button type="button" disabled={busy} onClick={() => { setError(""); void load(); }}>刷新列表</button>
    {!items.length && <p>暂无公告。</p>}
    {items.map((item) => <article key={item.id}>
      <h3>{item.title}</h3><small>{item.active ? "发布中" : "已撤回"} · {new Date(item.created_at).toLocaleString("zh-CN")}</small>
      <p className="announcement-preview">{item.content}</p>
      {item.active && <button type="button" disabled={busy} onClick={() => void withdraw(item)}>撤回公告</button>}
    </article>)}
  </section>;
}
