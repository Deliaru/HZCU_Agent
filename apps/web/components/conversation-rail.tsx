"use client";

import {
  Archive,
  MessageSquarePlus,
  Settings2,
  ShieldCheck,
  X,
} from "lucide-react";

import type { ReactNode } from "react";
import type { AuthSession, ConversationSummary } from "@/lib/api";

type Props = {
  conversations: ConversationSummary[];
  selectedId?: string;
  session: AuthSession | null;
  channelOnline: boolean;
  channelDetail: string;
  identityControl: ReactNode;
  open: boolean;
  onClose: () => void;
  onNew: () => void;
  onSelect: (id: string) => void;
  onOpenSpace: () => void;
};

function shortDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

export function ConversationRail({
  conversations,
  selectedId,
  session,
  channelOnline,
  channelDetail,
  identityControl,
  open,
  onClose,
  onNew,
  onSelect,
  onOpenSpace,
}: Props) {
  return (
    <aside
      className={`context-rail conversation-rail ${open ? "rail-open" : ""}`}
      aria-label="会话历史"
    >
      <div className="rail-mobile-heading">
        <b>会话轨</b>
        <button type="button" onClick={onClose} aria-label="关闭会话历史">
          <X size={19} />
        </button>
      </div>
      <div className="rail-number">
        <span>THREADS</span>
        06
      </div>
      <button className="new-thread" type="button" onClick={onNew}>
        <MessageSquarePlus size={17} />
        新对话
        <span>NEW</span>
      </button>
      <div className="rail-runtime">
        <p className="eyebrow">SYSTEM STATUS</p>
        <div>
          <span className={`signal ${channelOnline ? "online" : ""}`} />
          <span>
            <b>{channelOnline ? "信息通道在线" : "正在连接"}</b>
            <small>{channelDetail}</small>
          </span>
        </div>
        <div className="rail-identity">{identityControl}</div>
      </div>
      <div className="thread-index">
        <p className="eyebrow">RECENT SIGNALS</p>
        {conversations.length ? (
          conversations.map((conversation) => (
            <button
              type="button"
              key={conversation.conversation_id}
              className={
                selectedId === conversation.conversation_id ? "active" : ""
              }
              onClick={() => onSelect(conversation.conversation_id)}
            >
              <Archive size={14} />
              <span>
                <b>{conversation.title ?? "未命名对话"}</b>
                <small>
                  {shortDate(conversation.updated_at)}
                  {conversation.last_task_status
                    ? ` · ${conversation.last_task_status}`
                    : ""}
                </small>
                <code title={conversation.conversation_id}>
                  C · {conversation.conversation_id.slice(5, 17).toUpperCase()}
                </code>
              </span>
            </button>
          ))
        ) : (
          <div className="thread-empty">
            还没有历史会话。你的第一道问题会保存在这台设备上。
          </div>
        )}
      </div>
      <div className="rail-product-links">
        <button type="button" onClick={onOpenSpace}>
          <Settings2 size={16} />
          <span>
            <b>我的空间</b>
            <small>画像 · 待办 · 数据</small>
          </span>
        </button>
        {session?.role === "admin" && (
          <a href="/admin">
            <ShieldCheck size={16} />
            <span>
              <b>只读运营台</b>
              <small>试用健康与反馈</small>
            </span>
          </a>
        )}
      </div>
      <div className="source-ledger">
        <p className="eyebrow">WORKSPACE</p>
        <div>
          <span>访问范围</span>
          <i className="source-on">
            {session?.authenticated
              ? "CAMPUS"
              : session?.mirror_visibility_scopes.includes("campus")
                ? "PUBLIC + CAMPUS"
                : "PUBLIC"}
          </i>
        </div>
        <div>
          <span>数据保存</span>
          <i>180 DAYS</i>
        </div>
        <a href="/sources">查看来源透明账本 →</a>
      </div>
    </aside>
  );
}
