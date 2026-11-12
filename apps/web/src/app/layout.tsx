import type { Metadata } from "next";
import "./globals.css";

// Inter 通过系统字体栈回退：原实现使用 next/font/google，
// 构建与 dev 时需访问 Google Fonts，在无外网环境下会阻塞页面渲染。
export const metadata: Metadata = {
  title: "Knowledge Research Agent",
  description: "Your AI-powered assistant for enterprise knowledge research and analysis",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
