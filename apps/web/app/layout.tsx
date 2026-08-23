import type { Metadata, Viewport } from "next";
import "../styles/tokens.css";
import "../styles/base.css";
import "../styles/chrome.css";
import "../styles/agent.css";
import "../styles/panels.css";
import "../styles/observatory.css";
import "../styles/admin.css";
import "../styles/login.css";
import "../styles/responsive.css";
import "../styles/blue-white.css";
import "../styles/experience.css";

export const metadata: Metadata = {
  title: "城知｜浙大城市学院校园 Agent",
  description: "能理解、会调查、有依据的浙大城市学院校园认知 Agent。",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#edf2f7",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
