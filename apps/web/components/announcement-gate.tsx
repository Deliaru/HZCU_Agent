"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { type Announcement, getUnreadAnnouncements, readAnnouncement } from "@/lib/api";

export function AnnouncementGate() {
  const pathname = usePathname();
  const dialog = useRef<HTMLDialogElement>(null);
  const [items, setItems] = useState<Announcement[]>([]);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState(false);
  const [retry, setRetry] = useState(0);
  const current = items[0];

  useEffect(() => {
    let cancelled = false;
    void getUnreadAnnouncements().then((data) => {
      if (!cancelled) { setItems(data); setLoadError(false); }
    }).catch(() => { if (!cancelled) setLoadError(true); });
    return () => { cancelled = true; };
  }, [pathname, retry]);

  useEffect(() => {
    const refresh = () => setRetry((value) => value + 1);
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);

  useEffect(() => {
    setChecked(false);
    setError("");
    const element = dialog.current;
    if (current && element && !element.open) element.showModal();
    return () => { element?.close(); };
  }, [current?.id]);

  async function confirm() {
    if (!current || !checked || busy) return;
    setBusy(true);
    setError("");
    try {
      await readAnnouncement(current.id);
      setItems((existing) => existing.filter((item) => item.id !== current.id));
    } catch {
      setError("已读确认未保存，请重试；不会跳过这条公告。");
    } finally { setBusy(false); }
  }

  if (!current) return loadError ? (
    <div className="announcement-load-error" role="status">
      公告暂时无法加载。<button type="button" onClick={() => setRetry((n) => n + 1)}>重试</button>
    </div>
  ) : null;

  return (
    <dialog ref={dialog} className="announcement-dialog" aria-labelledby="announcement-title"
      onCancel={(event) => event.preventDefault()}>
      <header><span>站内公告 · {items.length} 条待确认</span>
        <h2 id="announcement-title">{current.title}</h2>
        <time>{new Date(current.created_at).toLocaleString("zh-CN")}</time>
      </header>
      <div className="announcement-body" tabIndex={0}>{current.content}</div>
      <footer>
        <label><input type="checkbox" checked={checked} disabled={busy}
          onChange={(event) => setChecked(event.target.checked)} />我已阅读这条公告</label>
        {error && <p role="alert">{error}</p>}
        <button type="button" disabled={!checked || busy} onClick={() => void confirm()}>
          {busy ? "正在保存…" : items.length > 1 ? "确认已读，查看下一条" : "确认已读，进入网站"}
        </button>
      </footer>
    </dialog>
  );
}
