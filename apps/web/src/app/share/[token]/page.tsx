"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Loader2, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import type { SharedReport } from "@/types";

/** 公开只读分享页：不经过 AuthGate，凭 token 读取报告快照 */
export default function SharedReportPage() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<SharedReport | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token) return;
    api
      .getShared(token)
      .then(setData)
      .catch(() => setError("分享链接不存在或已过期"));
  }, [token]);

  if (error) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background px-6">
        <div className="text-center">
          <p className="text-sm text-muted-foreground">{error}</p>
          <Link
            href="/"
            className="mt-3 inline-block text-sm font-medium text-primary hover:underline"
          >
            返回 ScholarFlow
          </Link>
        </div>
      </main>
    );
  }

  if (!data) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-background">
      <header className="border-b bg-background/95">
        <div className="mx-auto flex max-w-[860px] items-center justify-between px-5 py-3.5">
          <Link href="/" className="text-sm font-semibold tracking-tight">
            ScholarFlow
          </Link>
          <span className="inline-flex items-center gap-1.5 rounded-full border bg-muted/50 px-2.5 py-1 text-[11px] text-muted-foreground">
            <ShieldCheck className="h-3.5 w-3.5" />
            只读分享
          </span>
        </div>
      </header>

      <article className="mx-auto max-w-[860px] px-5 pb-16 pt-10 md:px-8">
        <h1 className="text-2xl font-semibold leading-snug tracking-tight">
          {data.title || "研究报告"}
        </h1>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span>生成于 {data.created_at.replace("T", " ").slice(0, 16)}</span>
          <span>·</span>
          <span>第 {data.version} 版</span>
          {data.degraded && <span className="text-amber-600">· 生成时有降级</span>}
        </div>

        {data.summary && (
          <p className="mt-5 rounded-xl border bg-muted/30 px-4 py-3 text-sm leading-relaxed text-muted-foreground">
            {data.summary}
          </p>
        )}

        <div className="report-body mt-7">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.markdown}</ReactMarkdown>
        </div>

        {data.sources.length > 0 && (
          <section className="mt-10 border-t pt-6">
            <h2 className="text-sm font-semibold">引用来源</h2>
            <ol className="mt-3 space-y-3">
              {data.sources.map((s) => (
                <li key={s.ref_no} className="flex gap-3 text-sm">
                  <span className="mt-0.5 shrink-0 text-xs font-semibold text-muted-foreground">
                    [{s.ref_no}]
                  </span>
                  <div className="min-w-0">
                    {s.url ? (
                      <a
                        href={s.url}
                        target="_blank"
                        rel="noreferrer"
                        className="font-medium hover:text-primary hover:underline"
                      >
                        {s.title || s.url}
                      </a>
                    ) : (
                      <span className="font-medium">{s.title}</span>
                    )}
                    {s.source_label && (
                      <span className="ml-2 text-xs text-muted-foreground">{s.source_label}</span>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </section>
        )}

        <p className="mt-10 border-t pt-5 text-center text-xs text-muted-foreground/80">
          内容由 AI 生成，可能存在不准确之处，请核实重要信息。本页面为只读快照，不接受编辑。
        </p>
      </article>
    </main>
  );
}
