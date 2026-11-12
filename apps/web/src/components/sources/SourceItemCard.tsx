"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";
import Link from "next/link";
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Download,
  ExternalLink,
  Eye,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatRelevance } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { SourceItem } from "@/types";
import { SourceTypeIcon } from "./SourceTypeIcon";

/** 解析 kb://doc/{id}#c{idx} → 知识库来源的文档与片段定位 */
export function parseKbUrl(url: string): { documentId: number; chunkIndex: number } | null {
  const match = /kb:\/\/doc\/(\d+)#c(\d+)/.exec(url);
  if (!match) return null;
  return { documentId: parseInt(match[1], 10), chunkIndex: parseInt(match[2], 10) };
}

type FulltextPhase = "idle" | "loading" | "pending" | "indexed" | "failed";

function initialFulltextPhase(source: SourceItem): FulltextPhase {
  const st = source.meta?.document_status;
  if (st === "indexed") return "indexed";
  if (st === "pending" || st === "parsing" || st === "embedding") return "pending";
  if (st === "failed") return "failed";
  return "idle";
}

/**
 * 研究页 Sources 列表项：
 * 点击卡片 → 展开 snippet 内容 + 反向联动报告 [n] 高亮；
 * 知识库来源提供"查看完整片段"弹窗；
 * 知网来源提供"获取全文并入库"：下载 PDF → 后台解析向量化 → 状态回写。
 */
export function SourceItemCard({
  source,
  selected,
  expanded,
  taskId,
  onToggle,
  onViewFull,
  onFulltextUpdated,
}: {
  source: SourceItem;
  selected: boolean;
  expanded: boolean;
  taskId: string | null;
  onToggle: () => void;
  onViewFull: (source: SourceItem) => void;
  onFulltextUpdated?: (meta: Partial<NonNullable<SourceItem["meta"]>>) => void;
}) {
  const kbLoc = parseKbUrl(source.url);
  const isCnki = source.type === "cnki";
  const recordRef = String(source.meta?.record_ref ?? "");

  const [ft, setFt] = useState<FulltextPhase>(() => initialFulltextPhase(source));
  const [ftMsg, setFtMsg] = useState(() => String(source.meta?.document_error ?? ""));
  const [ftChunks, setFtChunks] = useState(() => Number(source.meta?.chunk_count ?? 0));
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  // 父级 sources 刷新（重连快照/入库回写）后，用 meta 恢复卡片状态
  useEffect(() => {
    const st = source.meta?.document_status;
    if (st === "indexed") {
      setFt("indexed");
      setFtChunks(Number(source.meta?.chunk_count ?? 0));
      setFtMsg("");
    } else if (st === "failed") {
      setFt("failed");
      setFtMsg(String(source.meta?.document_error ?? ""));
    } else if (st === "pending" || st === "parsing" || st === "embedding") {
      setFt((prev) => (prev === "indexed" ? prev : "pending"));
    }
  }, [source.meta?.document_status, source.meta?.chunk_count, source.meta?.document_error]);

  /** 轮询轻量文档接口（不拉整份报告），直到 indexed/failed 或超时 */
  const pollDocument = (docId: number, attempt = 0) => {
    if (!mountedRef.current) return;
    if (attempt > 100) {
      setFtMsg("入库耗时较长，请稍后在知识库查看");
      return;
    }
    timerRef.current = setTimeout(async () => {
      if (!mountedRef.current) return;
      try {
        const d = await api.getDocument(docId);
        if (!mountedRef.current) return;
        if (d.status === "indexed") {
          setFt("indexed");
          setFtChunks(d.chunk_count);
          setFtMsg("");
          onFulltextUpdated?.({
            document_status: "indexed",
            document_id: docId,
            evidence_level: "fulltext",
            chunk_count: d.chunk_count,
          });
          return;
        }
        if (d.status === "failed") {
          const msg = d.error_msg || "解析入库失败";
          setFt("failed");
          setFtMsg(msg);
          onFulltextUpdated?.({ document_status: "failed", document_error: msg });
          return;
        }
      } catch {
        // 网络抖动不终止轮询
      }
      pollDocument(docId, attempt + 1);
    }, 3000);
  };

  // 卡片在入库期间被 Tab 切换/重渲染卸载后，重新挂载时按 meta.document_id 恢复轮询
  useEffect(() => {
    if (ft === "pending" && source.meta?.document_id && !timerRef.current) {
      pollDocument(Number(source.meta.document_id));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- pollDocument 每次渲染重建，仅需按状态/文档 id 触发
  }, [ft, source.meta?.document_id]);

  const handleFetchFulltext = async (e: MouseEvent) => {
    e.stopPropagation();
    if (!taskId || ft === "loading" || ft === "pending") return;

    setFt("loading");
    setFtMsg("通常需要 10–40 秒；知网限流时可能更久，请保持页面打开");
    try {
      const r = await api.fetchSourceFulltext(taskId, source.ref_no);
      if (!mountedRef.current) return;

      if (r.state === "indexed") {
        setFt("indexed");
        setFtChunks(r.chunk_count);
        setFtMsg("");
        onFulltextUpdated?.({
          document_status: "indexed",
          document_id: r.document_id ?? undefined,
          evidence_level: "fulltext",
          chunk_count: r.chunk_count,
        });
        return;
      }
      if (r.state === "failed") {
        setFt("failed");
        setFtMsg(r.reason ?? "获取失败");
        onFulltextUpdated?.({
          document_status: "failed",
          document_error: r.reason ?? "获取失败",
        });
        return;
      }

      setFt("pending");
      setFtMsg("");
      onFulltextUpdated?.({
        document_status: "pending",
        document_id: r.document_id ?? undefined,
      });
      if (r.document_id) pollDocument(r.document_id);
    } catch (err) {
      if (!mountedRef.current) return;
      const msg = err instanceof Error ? err.message : "请求失败";
      setFt("failed");
      setFtMsg(msg);
    }
  };

  const journalYear = [source.meta?.journal, source.meta?.year].filter(Boolean).join(" ");
  const evidenceLabel =
    ft === "indexed" || source.meta?.evidence_level === "fulltext"
      ? "全文"
      : source.meta?.evidence_level === "title_only"
        ? "题录"
        : "摘要";

  return (
    <div
      data-ref-no={source.ref_no}
      className={cn(
        "anim-rise-soft w-full max-w-full cursor-pointer rounded-xl border bg-card p-3 transition-all duration-300",
        selected && "border-primary ring-2 ring-primary/30",
        !selected && "hover:border-primary/40",
      )}
      onClick={onToggle}
    >
      <div className="flex items-start gap-2.5">
        <SourceTypeIcon type={source.type} />
        <div className="min-w-0 flex-1">
          <div className="line-clamp-2 break-words text-[13px] font-medium leading-snug text-foreground/90">
            {source.ref_no > 0 && (
              <span className="mr-1 font-semibold text-primary">[{source.ref_no}]</span>
            )}
            {source.title}
          </div>
          <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-1.5">
            <Badge variant="muted" className="max-w-[160px] truncate px-1.5 text-[11px] font-normal">
              {source.source_label || source.type}
            </Badge>
            {isCnki && journalYear && (
              <span className="max-w-[180px] truncate text-[11px] text-muted-foreground">
                · {journalYear}
              </span>
            )}
            {isCnki && (
              <Badge
                variant="muted"
                className={cn(
                  "px-1.5 text-[11px] font-normal",
                  evidenceLabel === "全文" && "bg-emerald-50 text-emerald-700",
                )}
              >
                {evidenceLabel}
              </Badge>
            )}
            <span className="max-w-[120px] truncate text-[11px] text-muted-foreground">
              · 相关度 {formatRelevance(source.relevance)}
            </span>
            {source.page_nos && source.page_nos.length > 0 && (
              <span className="max-w-[140px] truncate text-[11px] text-muted-foreground">
                · 第{source.page_nos.join("、")}页
              </span>
            )}
            <span className="ml-auto text-[11px] text-muted-foreground">
              {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            </span>
          </div>
        </div>
      </div>

      {expanded && (
        <div
          className="mt-2.5 animate-fade-in border-t border-border/60 pt-2.5"
          onClick={(e) => e.stopPropagation()}
        >
          {/* 原生滚动区：内容超长在展开区内滚动，收起按钮始终可达 */}
          <div className="max-h-[240px] w-full max-w-full overflow-y-auto whitespace-pre-wrap pr-1 text-[12.5px] leading-relaxed text-foreground/80">
            {source.snippet || "（该来源暂无内容摘要）"}
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-2">
            {kbLoc && (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 gap-1 text-xs text-primary hover:bg-accent"
                onClick={() => onViewFull(source)}
              >
                <Eye className="h-3.5 w-3.5" />
                查看完整片段
              </Button>
            )}

            {isCnki && taskId && (
              <>
                {ft === "idle" && recordRef && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 gap-1 text-xs"
                    onClick={handleFetchFulltext}
                  >
                    <Download className="h-3.5 w-3.5" />
                    获取全文并入库
                  </Button>
                )}
                {ft === "idle" && !recordRef && (
                  <span className="text-[11px] text-muted-foreground">
                    该来源为站内降级结果，无法下载全文
                  </span>
                )}
                {ft === "loading" && (
                  <Button variant="outline" size="sm" className="h-7 gap-1 text-xs" disabled>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    正在下载全文…
                  </Button>
                )}
                {ft === "pending" && (
                  <Button variant="outline" size="sm" className="h-7 gap-1 text-xs" disabled>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    正在解析入库…
                  </Button>
                )}
                {ft === "indexed" &&
                  (source.meta?.document_id ? (
                    <Link
                      href={`/knowledge-base?doc=${source.meta.document_id}`}
                      title="在知识库查看"
                      className="inline-flex items-center gap-1 rounded-md bg-emerald-50 px-2 py-1 text-[11px] text-emerald-700 transition-colors hover:bg-emerald-100"
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      全文已入库{ftChunks > 0 ? ` · ${ftChunks} 片段` : ""}
                    </Link>
                  ) : (
                    <Badge
                      variant="muted"
                      className="gap-1 bg-emerald-50 px-2 py-1 text-[11px] font-normal text-emerald-700"
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      全文已入库{ftChunks > 0 ? ` · ${ftChunks} 片段` : ""}
                    </Badge>
                  ))}
                {ft === "failed" && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 gap-1 text-xs text-rose-600 hover:text-rose-700"
                    onClick={handleFetchFulltext}
                  >
                    <RefreshCw className="h-3.5 w-3.5" />
                    重试获取全文
                  </Button>
                )}
                {ftMsg && (
                  <span
                    className={cn(
                      "max-w-full truncate text-[11px]",
                      ft === "failed" ? "text-rose-600" : "text-muted-foreground",
                    )}
                    title={ftMsg}
                  >
                    {ftMsg}
                  </span>
                )}
              </>
            )}

            {isCnki && source.url.startsWith("http") && (
              <a
                href={source.url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-7 items-center gap-1 text-xs text-muted-foreground hover:text-primary"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                知网页面
              </a>
            )}

            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 text-xs text-muted-foreground hover:bg-accent"
              onClick={onToggle}
            >
              <ChevronUp className="h-3.5 w-3.5" />
              收起
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
