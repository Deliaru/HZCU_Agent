import {
  Database,
  Gauge,
  LayoutGrid,
  MessageSquareText,
  Radio,
} from "lucide-react";
import type { ReactNode } from "react";

export type AppSection = "agent" | "questions" | "sources" | "admin";

type AppChromeProps = {
  section: AppSection;
  className?: string;
  channel: string;
  mode: string;
  title: string;
  eyebrow: string;
  mobileAction?: ReactNode;
  utilities?: ReactNode;
  children: ReactNode;
};

const NAVIGATION: Array<{
  id: AppSection;
  href: string;
  label: string;
  shortLabel: string;
  index: string;
  icon: typeof MessageSquareText;
}> = [
  {
    id: "agent",
    href: "/",
    label: "校园 Agent",
    shortLabel: "AGENT",
    index: "01",
    icon: MessageSquareText,
  },
  {
    id: "questions",
    href: "/questions",
    label: "问题悬赏版",
    shortLabel: "Q BOARD",
    index: "02",
    icon: MessageSquareText,
  },
  {
    id: "sources",
    href: "/sources",
    label: "来源账本",
    shortLabel: "SOURCE",
    index: "03",
    icon: Database,
  },
  {
    id: "admin",
    href: "/admin",
    label: "系统管理",
    shortLabel: "ADMIN",
    index: "04",
    icon: Gauge,
  },
];

export function AppChrome({
  section,
  className = "",
  channel,
  mode,
  title,
  eyebrow,
  mobileAction,
  utilities,
  children,
}: AppChromeProps) {
  return (
    <main className={`app-chrome app-section-${section} ${className}`.trim()}>
      <div className="ambient-grid" aria-hidden="true" />

      <aside className="app-rail" aria-label="全局功能导航">
        <a className="app-rail-brand" href="/" aria-label="城知首页">
          <span>HZ</span>
          <b>城知</b>
        </a>
        <nav>
          {NAVIGATION.map((item) => {
            const Icon = item.icon;
            return (
              <a
                key={item.id}
                href={item.href}
                className={section === item.id ? "active" : ""}
                aria-current={section === item.id ? "page" : undefined}
                aria-label={item.label}
              >
                <small>{item.index}</small>
                <Icon size={19} strokeWidth={1.7} />
                <span>{item.shortLabel}</span>
              </a>
            );
          })}
        </nav>
        <div className="app-rail-tail" aria-hidden="true">
          <Radio size={15} />
          <span>06</span>
        </div>
      </aside>

      <section className="chrome-frame">
        <div className="calibration-strip" aria-hidden="true">
          <span>{channel}</span>
          <i />
          <span>{mode}</span>
        </div>

        <header className="masthead">
          <div className="mast-mobile-actions">
            {mobileAction}
            <details className="mobile-global-menu">
              <summary aria-label="打开全局导航">
                <LayoutGrid size={18} />
              </summary>
              <nav aria-label="移动端全局导航">
                {NAVIGATION.map((item) => {
                  const Icon = item.icon;
                  return (
                    <a
                      key={item.id}
                      href={item.href}
                      className={section === item.id ? "active" : ""}
                      aria-current={section === item.id ? "page" : undefined}
                    >
                      <span>{item.index}</span>
                      <Icon size={17} />
                      {item.label}
                    </a>
                  );
                })}
              </nav>
            </details>
          </div>

          <a className="brand" href="/" aria-label="城知首页">
            <span className="brand-seal" aria-hidden="true">
              <b>城</b>
              <b>知</b>
            </span>
            <span className="brand-copy">
              <strong>校园认知 Agent</strong>
              <small>HZCU CAMPUS INTELLIGENCE</small>
            </span>
          </a>

          <div className="mast-context">
            <span>{eyebrow}</span>
            <b>{title}</b>
          </div>

          <div className="mast-actions">{utilities}</div>
        </header>

        <div className="chrome-body">{children}</div>
      </section>
    </main>
  );
}
