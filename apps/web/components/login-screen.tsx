"use client";

import {
  ArrowLeft,
  ArrowRight,
  Building2,
  Check,
  KeyRound,
  LockKeyhole,
  LogOut,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  getAuthSession,
  loginLocalAdmin,
  logoutCampusSession,
  setupLocalAdmin,
  type AuthSession,
} from "@/lib/api";
import { CongyuArtwork } from "./congyu-artwork";
import { useTheme } from "./theme-provider";

const AUTH_ERRORS: Record<string, string> = {
  CAS_STATE_INVALID: "登录状态已经失效，请重新发起校园 CA 登录。",
  CAS_TICKET_INVALID: "学校返回的登录票据无效，请重新登录。",
  CAS_VALIDATION_FAILED: "校园 CA 暂时没有完成身份校验。",
};

export function LoginScreen() {
  const { theme } = useTheme();
  const [session, setSession] = useState<AuthSession | null>(null);
  const [loadError, setLoadError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");

  useEffect(() => {
    getAuthSession().then(setSession).catch((cause) => {
      setLoadError(cause instanceof Error ? cause.message : "身份服务暂时无法连接");
    });
  }, []);

  const context = useMemo(() => {
    if (typeof window === "undefined") {
      return { returnPath: "/admin", authError: undefined as string | undefined };
    }
    const parameters = new URLSearchParams(window.location.search);
    const requested = parameters.get("return_to");
    const returnPath =
      requested?.startsWith("/") && !requested.startsWith("//")
        ? requested
        : "/admin";
    const code = parameters.get("auth_error") ?? "";
    return { returnPath, authError: AUTH_ERRORS[code] ?? (code || undefined) };
  }, []);

  const loginHref = useMemo(() => {
    if (!session?.login_url || typeof window === "undefined") return undefined;
    const target = new URL(session.login_url);
    target.searchParams.set(
      "return_to",
      `${window.location.origin}${context.returnPath}`,
    );
    return target.toString();
  }, [context.returnPath, session?.login_url]);

  const authenticated = session?.authenticated ?? false;
  const isAdmin = session?.role === "admin";
  const localAdminEnabled = session?.local_admin_enabled ?? false;
  const setupMode = localAdminEnabled && !session?.local_admin_configured;
  const canSubmitLocalAdmin =
    localAdminEnabled &&
    (session?.local_admin_configured || session?.local_admin_setup_available);

  async function submitLocalAdmin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (setupMode && password !== confirmation) {
      setLoadError("两次输入的密码不一致。");
      return;
    }
    setBusy(true);
    setLoadError(undefined);
    try {
      const nextSession = setupMode
        ? await setupLocalAdmin(username, password)
        : await loginLocalAdmin(username, password);
      setSession(nextSession);
      setPassword("");
      setConfirmation("");
      window.location.assign(context.returnPath);
    } catch (cause) {
      setPassword("");
      setConfirmation("");
      setLoadError(cause instanceof Error ? cause.message : "后台管理员登录失败");
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setBusy(true);
    setLoadError(undefined);
    try {
      await logoutCampusSession();
      setSession(await getAuthSession());
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : "退出登录失败");
    } finally {
      setBusy(false);
    }
  }

  const title = localAdminEnabled
    ? setupMode
      ? "设置后台\n管理员。"
      : "进入系统\n后台。"
    : "用校园 CA\n确认你的身份。";

  return (
    <main className={theme === "character" ? "congyu-login" : "login-shell"}>
      <section className="login-poster" aria-label="城知系统管理身份入口">
        {theme === "character" && (
          <div className="congyu-login-character">
            <div className="congyu-login-character-copy">
              <p>SECURE CAMPUS GATE / 04</p>
              <h2>欢迎回来，<br />调查员。</h2>
              <span>确认身份后，就能继续使用校园资料权限与管理工具。</span>
            </div>
            <CongyuArtwork scene="mobile-welcome" sizes="min(44vw, 500px)" />
          </div>
        )}
        <a className="login-brand" href="/" aria-label="返回城知首页">
          <span>{theme === "character" ? "琮" : "城"}</span><span>{theme === "character" ? "羽" : "知"}</span>
          <small>{theme === "character" ? "CONGYU CAMPUS AGENT" : "HZCU CAMPUS INTELLIGENCE"}</small>
        </a>
        <div className="login-poster-copy">
          <span>ADMINISTRATION CHANNEL / 01</span>
          <strong aria-hidden="true">ADM</strong>
          <p>独立后台身份，只负责服务器配置与运行管理。</p>
        </div>
        <div className="login-poster-register" aria-hidden="true">
          <i />
          <span>LOCAL ADMIN<br />SESSION TOKEN</span>
          <b>01</b>
        </div>
      </section>

      <section className="login-stage">
        <header className="login-stage-head">
          <a href="/"><ArrowLeft size={15} /> 返回 Agent</a>
          <span><i /> {localAdminEnabled ? "LOCAL ADMIN" : "HZCU CA CONNECTION"}</span>
        </header>

        <div className="login-form-plane">
          <span className="login-kicker">
            {localAdminEnabled ? "SERVER CONSOLE / 系统管理" : "CAMPUS ACCOUNT / 校园统一身份"}
          </span>
          {localAdminEnabled ? (
            <LockKeyhole size={30} strokeWidth={1.4} />
          ) : (
            <Building2 size={30} strokeWidth={1.4} />
          )}
          <h1>{title.split("\n").map((line, index) => (
            <span key={line}>{line}{index === 0 ? <br /> : null}</span>
          ))}</h1>
          <p>
            {localAdminEnabled
              ? setupMode
                ? "使用服务器中已经登记的管理员账号完成首次设置。密码只在本机生成 scrypt 哈希；你可以输入与校园账号相同的密码，但两套系统不会自动同步。"
                : "使用城知服务器自己的管理员账号登录。该登录不经过校园 CA，仅用于模型、API 与运行状态管理。"
              : "账号与密码只在学校统一身份认证页面输入。城知只接收一次性登录票据。"}
          </p>

          {context.authError || loadError ? (
            <div className="login-notice error" role="alert">
              {context.authError ?? loadError}
            </div>
          ) : null}

          {!session && !loadError ? (
            <div className="login-loading" aria-live="polite">
              <i /><span>正在读取身份通道</span>
            </div>
          ) : null}

          {session && !authenticated && canSubmitLocalAdmin ? (
            <form className="login-credential-form" onSubmit={submitLocalAdmin}>
              <label>
                <span>管理员账号</span>
                <div>
                  <UserRound size={16} />
                  <input
                    name="username"
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    autoComplete="username"
                    disabled={busy}
                    required
                  />
                </div>
              </label>
              <label>
                <span>{setupMode ? "设置管理员密码" : "管理员密码"}</span>
                <div>
                  <KeyRound size={16} />
                  <input
                    name="password"
                    type="password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    autoComplete={setupMode ? "new-password" : "current-password"}
                    minLength={6}
                    maxLength={256}
                    disabled={busy}
                    required
                  />
                </div>
              </label>
              {setupMode ? (
                <label>
                  <span>再次输入密码</span>
                  <div>
                    <ShieldCheck size={16} />
                    <input
                      name="password-confirmation"
                      type="password"
                      value={confirmation}
                      onChange={(event) => setConfirmation(event.target.value)}
                      autoComplete="new-password"
                      minLength={6}
                      maxLength={256}
                      disabled={busy}
                      required
                    />
                  </div>
                </label>
              ) : null}
              <button className="login-primary-action" type="submit" disabled={busy}>
                <LockKeyhole size={18} />
                <span>
                  <b>{busy ? "正在验证" : setupMode ? "设置并进入后台" : "进入管理后台"}</b>
                  <small>{setupMode ? "首次设置完成后立即登录" : "使用服务器本地管理员身份"}</small>
                </span>
                <ArrowRight size={20} />
              </button>
            </form>
          ) : null}

          {session && !authenticated && setupMode && !session.local_admin_setup_available ? (
            <div className="login-notice">
              <b>后台管理员尚未初始化</b>
              <span>生产环境请先通过服务器运维流程设置管理员凭据。</span>
            </div>
          ) : null}

          {session && !authenticated && loginHref ? (
            <a className="login-secondary-action" href={loginHref}>
              <ShieldCheck size={16} />
              <span><b>使用校园 CA</b><small>需要校方登记回调地址</small></span>
              <ArrowRight size={17} />
            </a>
          ) : null}

          {session && !authenticated && !localAdminEnabled && !loginHref ? (
            <div className="login-notice">
              <b>身份登录当前不可用</b>
              <span>服务器尚未启用本地管理员或校园 CA 登录。</span>
            </div>
          ) : null}

          {authenticated && isAdmin ? (
            <a className="login-primary-action success" href={context.returnPath}>
              <Check size={18} />
              <span>
                <b>身份已确认</b>
                <small>
                  {session?.subject_hint ?? "ADMIN"} · {session?.subject_kind === "local_admin" ? "后台管理员" : "校园管理员"}
                </small>
              </span>
              <ArrowRight size={20} />
            </a>
          ) : null}

          {authenticated && !isAdmin ? (
            <div className="login-notice">
              <b>当前身份不是管理员</b>
              <span>{session?.subject_hint ?? "校园用户"} 可以继续使用 Agent 与来源账本。</span>
              <button type="button" disabled={busy} onClick={() => void logout()}>
                <LogOut size={14} /> 退出并更换身份
              </button>
            </div>
          ) : null}

          <footer className="login-capability">
            <span><b>01</b> 本地凭据验证</span>
            <span><b>02</b> 服务器角色校验</span>
            <span><b>03</b> 管理配置生效</span>
          </footer>
        </div>
      </section>
    </main>
  );
}
