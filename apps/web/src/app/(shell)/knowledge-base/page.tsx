"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Clock3, FileWarning, Layers, Loader2, Search, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ChunkPreviewDialog } from "@/components/kb/ChunkPreviewDialog";
import { DocumentTable } from "@/components/kb/DocumentTable";
import { UploadZone } from "@/components/kb/UploadZone";
import { Button } from "@/components/ui/button";
import { useReveal } from "@/hooks/use-reveal";
import { api } from "@/lib/api";
import type { KbDocument } from "@/types";

const PAGE_SIZE = 20;

const STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  { value: "indexed", label: "已索引" },
  { value: "pending", label: "待处理" },
  { value: "parsing", label: "解析中" },
  { value: "embedding", label: "向量化中" },
  { value: "failed", label: "失败" },
];

const TYPE_OPTIONS = [
  { value: "", label: "全部类型" },
  { value: "pdf", label: "PDF" },
  { value: "docx", label: "DOCX" },
  { value: "md", label: "Markdown" },
  { value: "txt", label: "TXT" },
];

/** Knowledge Base：本地知识库管理（上传→解析→切片→向量化→检索） */
export default function KnowledgeBasePage() {
  const queryClient = useQueryClient();
  const revealRef = useReveal<HTMLDivElement>();
  const [previewDoc, setPreviewDoc] = useState<KbDocument | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const targetDocId = useRef<number | null>(null);

  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [status, setStatus] = useState("");
  const [docType, setDocType] = useState("");
  const [page, setPage] = useState(1);

  // 从研究报告的「全文已入库」跳转：/knowledge-base?doc={id} → 直接按 id 打开预览
  useEffect(() => {
    const raw = new URLSearchParams(window.location.search).get("doc");
    const id = raw ? Number(raw) : NaN;
    if (Number.isFinite(id)) {
      api
        .getDocument(id)
        .then((doc) => {
          setPreviewDoc(doc);
          setPreviewOpen(true);
          setHighlightId(id);
        })
        .catch(() => {});
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQ(q.trim());
      setPage(1);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [q]);

  useEffect(() => {
    setPage(1);
  }, [status, docType]);

  const { data: pageData, isPending: docsPending } = useQuery({
    queryKey: ["kb-documents", debouncedQ, status, docType, page],
    queryFn: () =>
      api.listDocuments({
        q: debouncedQ,
        status,
        doc_type: docType,
        page,
        page_size: PAGE_SIZE,
      }),
    // 有处理中任务时高频轮询状态
    refetchInterval: (query) =>
      (query.state.data?.items ?? []).some((d) =>
        ["pending", "parsing", "embedding"].includes(d.status),
      )
        ? 3000
        : 15000,
  });
  const docs = pageData?.items ?? [];
  const total = pageData?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const { data: stats } = useQuery({
    queryKey: ["kb-stats"],
    queryFn: api.kbStats,
    refetchInterval: 10000,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["kb-documents"] });

  const uploadMutation = useMutation({
    mutationFn: api.uploadDocuments,
    onSuccess: () => {
      setPage(1);
      invalidate();
    },
    onError: (e: Error) => window.alert(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: api.deleteDocument,
    onSuccess: invalidate,
  });

  const retryMutation = useMutation({
    mutationFn: api.retryDocument,
    onSuccess: invalidate,
    onError: (e: Error) => window.alert(e.message),
  });

  const reindexMutation = useMutation({
    mutationFn: api.reindexDocument,
    onSuccess: invalidate,
    onError: (e: Error) => window.alert(e.message),
  });

  const onDelete = (doc: KbDocument) => {
    if (window.confirm(`确认删除「${doc.name}」？其 ${doc.chunk_count} 个切片将一并从向量库移除。`)) {
      deleteMutation.mutate(doc.id);
    }
  };

  const onReindex = (doc: KbDocument) => {
    if (
      window.confirm(
        `重新索引「${doc.name}」？将重新解析原文件并重建全部切片与向量${doc.is_partial ? "（当前仅索引部分切片）" : ""}。`,
      )
    ) {
      reindexMutation.mutate(doc.id);
    }
  };

  const statCards = [
    { icon: Layers, label: "文档总数", value: stats?.documents ?? 0 },
    { icon: CheckCircle2, label: "已索引", value: stats?.indexed ?? 0, tint: "text-emerald-600" },
    { icon: Clock3, label: "处理中", value: stats?.processing ?? 0, tint: "text-primary" },
    { icon: XCircle, label: "失败", value: stats?.failed ?? 0, tint: "text-rose-600" },
    { icon: Layers, label: "向量切片", value: stats?.chunks ?? 0 },
  ];

  return (
    /* 原生滚动（非 Radix ScrollArea）：其视口 display:table 的内在宽度会被宽表格撑开，
     * 在窄屏把整页内容拉宽到表格宽度；原生容器让表格只在 overflow-x-auto 内部横滚 */
    <div className="min-w-0 flex-1 overflow-y-auto">
      <div ref={revealRef} className="mx-auto max-w-[1000px] px-5 py-8 md:px-8">
        <h1 className="text-xl font-bold">Knowledge Base</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          本地知识库：上传文档后自动完成解析、语义切片与 bge-m3 向量化，研究报告的"查询本地知识库"
          步骤将检索这些真实内部资料
        </p>

        {stats && !stats.vector_store_ready && (
          <div className="mt-4 flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
            <FileWarning className="h-4 w-4" />
            Qdrant 向量库不可达（docker compose up -d qdrant），文档可上传但无法索引
          </div>
        )}

        {/* 统计条（移动端 2 列 → 平板 3 列 → 桌面 5 列） */}
        <div className="reveal mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {statCards.map(({ icon: Icon, label, value, tint }) => (
            <div key={label} className="rounded-xl border bg-card p-3.5">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Icon className={`h-3.5 w-3.5 ${tint ?? ""}`} />
                {label}
              </div>
              <div className="mt-1.5 text-xl font-bold tabular-nums">{value}</div>
            </div>
          ))}
        </div>

        {/* 上传区 */}
        <div className="reveal mt-5" style={{ "--d": "80ms" } as React.CSSProperties}>
          <UploadZone uploading={uploadMutation.isPending} onUpload={(files) => uploadMutation.mutate(files)} />
        </div>

        {/* 搜索与筛选 */}
        <div className="mt-5 flex flex-wrap items-center gap-2">
          <div className="relative min-w-[200px] flex-1 sm:max-w-[320px]">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索文件名或错误信息"
              className="h-9 w-full rounded-lg border bg-card pl-8 pr-3 text-sm outline-none focus:ring-2 focus:ring-ring/40"
            />
          </div>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="h-9 rounded-lg border bg-card px-2.5 text-sm outline-none focus:ring-2 focus:ring-ring/40"
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <select
            value={docType}
            onChange={(e) => setDocType(e.target.value)}
            className="h-9 rounded-lg border bg-card px-2.5 text-sm outline-none focus:ring-2 focus:ring-ring/40"
          >
            {TYPE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>

        {/* 文档表格 */}
        <div className="mt-3">
          {docsPending ? (
            <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 加载文档列表...
            </div>
          ) : (
            <DocumentTable
              documents={docs}
              highlightedId={highlightId}
              onPreview={(d) => {
                setPreviewDoc(d);
                setPreviewOpen(true);
              }}
              onDelete={onDelete}
              onRetry={(d) => retryMutation.mutate(d.id)}
              onReindex={onReindex}
            />
          )}
        </div>

        {/* 分页 */}
        <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
          <span>
            共 {total} 篇{totalHint(debouncedQ, status, docType)}
          </span>
          <div className="flex items-center gap-2">
            <span className="tabular-nums">
              {page} / {pages}
            </span>
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2 text-xs"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              上一页
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2 text-xs"
              disabled={page >= pages}
              onClick={() => setPage((p) => Math.min(pages, p + 1))}
            >
              下一页
            </Button>
          </div>
        </div>
      </div>

      <ChunkPreviewDialog doc={previewDoc} open={previewOpen} onOpenChange={setPreviewOpen} />
    </div>
  );
}

function totalHint(q: string, status: string, docType: string) {
  return q || status || docType ? "（已筛选）" : "";
}
