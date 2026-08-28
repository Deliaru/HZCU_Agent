"use client";

import {
  Building2,
  KeyRound,
  LogIn,
  LogOut,
  ShieldCheck,
  Wifi,
  X,
} from "lucide-react";
import { useState } from "react";
import type { FormEvent, ReactNode } from "react";

import {
  loginWithCampusCredentials,
  type AuthSession,
} from "@/lib/api";

type IdentityControlProps = {
  session: AuthSession | null;
  busy?: boolean;
  onLogout: () => void;
  onAuthenticated?: (session: AuthSession) => void;
};

export function IdentityControl({
  session,
  busy = false,
  onLogout,
  onAuthenticated,
}: IdentityControlProps) {
  const [credentialPanelOpen, setCredentialPanelOpen] = useState(false);
  const [credentialBusy, setCredentialBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [credentialError, setCredentialError] = useState<string>();

  async function submitCredentials(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (credentialBusy || !username.trim() || !password) return;
    setCredentialBusy(true);
    setCredentialError(undefined);
    try {
      const nextSession = await loginWithCampusCredentials(
        username.trim(),
        password,
      );
      setPassword("");
      setCredentialPanelOpen(false);
      onAuthenticated?.(nextSession);
    } catch (cause) {
      setCredentialError(
        cause instanceof Error ? cause.message : "校外只读会话建立失败",
      );
    } finally {
      setPassword("");
      setCredentialBusy(false);
    }
  }

  const hasPilotCampusMirror =
    !session?.authenticated &&
    session?.mirror_visibility_scopes.includes("campus");

  let primary: ReactNode;
  if (!session) {
    primary = (
      <div className="identity-control identity-loading" aria-label="正在读取身份">
        <span className="identity-glyph" aria-hidden="true">ID</span>
        <span>
          <b>身份通道</b>
          <small>正在校验</small>
        </span>
      </div>
    );
  } else if (session.authenticated) {
    const isLocalAdmin = session.subject_kind === "local_admin";
    const routeLabel =
      isLocalAdmin
        ? "本地管理会话"
        : session.query_access === "direct"
        ? "校内直连"
        : session.query_access === "vpn"
          ? "VPN 只读"
          : "校园缓存";
    primary = (
      <div className="identity-control identity-active">
        <span className="identity-glyph" aria-hidden="true">
          <ShieldCheck size={16} />
        </span>
        <span>
          <b>{isLocalAdmin ? "后台管理员" : "校园身份"}</b>
          <small>{session.subject_hint ?? "已验证"} · {routeLabel}</small>
        </span>
        <button
          type="button"
          onClick={onLogout}
          disabled={busy}
          aria-label={isLocalAdmin ? "退出后台管理员" : "退出校园身份"}
          title={isLocalAdmin ? "退出后台管理员" : "退出校园身份"}
        >
          <LogOut size={14} />
          <span className="identity-logout-label">退出</span>
        </button>
      </div>
    );
  } else if (session.login_url) {
    primary = (
      <a
        className="identity-control identity-login"
        href={campusLoginHref(session.login_url)}
      >
        <span className="identity-glyph" aria-hidden="true">
          <LogIn size={16} />
        </span>
        <span>
          <b>校内 CA 登录</b>
          <small>
            {hasPilotCampusMirror
              ? "登录以启用 CAMPUS 实时核验"
              : "解锁 CAMPUS 信源"}
          </small>
        </span>
      </a>
    );
  } else {
    primary = (
      <div className="identity-control identity-public">
        <span className="identity-glyph" aria-hidden="true">P</span>
        <span>
          <b>{hasPilotCampusMirror ? "试用访问" : "公开访问"}</b>
          <small>
            {hasPilotCampusMirror
              ? "PUBLIC + CAMPUS 本地镜像"
              : session.service_registration_required
              ? "CA 回调待校方登记"
              : "PUBLIC 信源"}
          </small>
        </span>
      </div>
    );
  }

  return (
    <div className="identity-shell">
      {primary}
      {session?.credential_handoff_available && !session.authenticated ? (
        <button
          className="identity-vpn-trigger"
          type="button"
          onClick={() => setCredentialPanelOpen(true)}
        >
          <Wifi size={13} />
          校外只读通道
        </button>
      ) : null}

      {credentialPanelOpen ? (
        <div
          className="credential-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.currentTarget === event.target && !credentialBusy) {
              setCredentialPanelOpen(false);
            }
          }}
        >
          <section
            className="credential-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="credential-title"
          >
            <header>
              <span className="credential-mark" aria-hidden="true">
                <Building2 size={18} />
              </span>
              <span>
                <small>OFF-CAMPUS ACCESS</small>
                <h2 id="credential-title">建立校外通知查询会话</h2>
              </span>
              <button
                type="button"
                aria-label="关闭登录窗口"
                disabled={credentialBusy}
                onClick={() => setCredentialPanelOpen(false)}
              >
                <X size={18} />
              </button>
            </header>

            <p>
              凭据只会经 HTTPS 交给学校批准的 VPN 边车，用于建立
              <code> campus_notice.read </code>
              会话；不会写入数据库、日志或模型上下文。
            </p>

            <form onSubmit={submitCredentials}>
              <label>
                <span>统一身份认证账号</span>
                <input
                  name="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  autoComplete="username"
                  inputMode="text"
                  disabled={credentialBusy}
                  required
                />
              </label>
              <label>
                <span>统一身份认证密码</span>
                <input
                  name="password"
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="current-password"
                  disabled={credentialBusy}
                  required
                />
              </label>
              {credentialError ? (
                <div className="credential-error" role="alert">
                  {credentialError}
                </div>
              ) : null}
              <footer>
                <span>
                  禁止申请、提交、报名、选退课及其他代办操作
                </span>
                <button type="submit" disabled={credentialBusy}>
                  <KeyRound size={15} />
                  {credentialBusy ? "正在建立安全会话" : "验证并进入"}
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function campusLoginHref(loginUrl: string): string {
  if (typeof window === "undefined") return loginUrl;
  const target = new URL(loginUrl);
  target.searchParams.set(
    "return_to",
    `${window.location.origin}${window.location.pathname}`,
  );
  return target.toString();
}
