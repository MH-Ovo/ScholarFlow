"use client";

import {
  Check,
  ChevronDown,
  Copy,
  Download,
  FileDown,
  FileText,
  Loader2,
  Share2,
} from "lucide-react";
import { memo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api";

type ExportFormat = "md" | "docx" | "pdf";

const EXT: Record<ExportFormat, string> = { md: "md", docx: "docx", pdf: "pdf" };

/** 研究页标题栏：报告标题 + 复制 + 分享只读链接 + 导出（Markdown/Word/PDF） */
function ReportHeaderInner({
  title,
  taskId,
  canExport,
  markdown,
}: {
  title: string;
  taskId: string | null;
  canExport: boolean;
  markdown: string;
}) {
  const [busy, setBusy] = useState<"" | "export" | "share">("");
  const [hint, setHint] = useState<"" | "copied" | "shared">("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flash = (value: "copied" | "shared") => {
    setHint(value);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setHint(""), 2200);
  };

  const copyText = async (text: string): Promise<boolean> => {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 非安全上下文/权限被拒：退回 execCommand
      try {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        const ok = document.execCommand("copy");
        area.remove();
        return ok;
      } catch {
        return false;
      }
    }
  };

  const download = async (format: ExportFormat) => {
    if (!taskId || busy) return;
    setBusy("export");
    try {
      const blob = await api.exportReport(taskId, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `report-${taskId.slice(0, 8)}.${EXT[format]}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "导出失败");
    } finally {
      setBusy("");
    }
  };

  const copy = async () => {
    if (!markdown) {
      window.alert("报告还没生成");
      return;
    }
    if (await copyText(markdown)) flash("copied");
    else window.alert("复制失败，请手动选中报告正文复制");
  };

  const share = async () => {
    if (!taskId || busy) return;
    setBusy("share");
    try {
      const res = await api.createShare(taskId);
      const url = `${window.location.origin}${res.path}`;
      if (await copyText(url)) flash("shared");
      else window.prompt("复制分享链接", url);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "生成分享链接失败");
    } finally {
      setBusy("");
    }
  };

  const disabled = !canExport || !taskId;

  return (
    <div className="flex items-center justify-between gap-3 border-b bg-background/95 px-4 py-3 md:px-6">
      <h1 className="truncate text-[15px] font-semibold" title={title || "研究报告"}>
        {title || "研究报告"}
      </h1>
      <div className="flex shrink-0 items-center gap-1.5">
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1.5 text-xs"
          disabled={disabled}
          onClick={copy}
          title="复制报告正文"
        >
          {hint === "copied" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          <span className="hidden sm:inline">{hint === "copied" ? "已复制" : "复制"}</span>
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1.5 text-xs"
          disabled={disabled || busy === "share"}
          onClick={share}
          title="生成只读分享链接"
        >
          {busy === "share" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : hint === "shared" ? (
            <Check className="h-3.5 w-3.5" />
          ) : (
            <Share2 className="h-3.5 w-3.5" />
          )}
          <span className="hidden sm:inline">
            {hint === "shared" ? "链接已复制" : "分享"}
          </span>
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs"
              disabled={disabled || busy === "export"}
            >
              {busy === "export" ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Download className="h-3.5 w-3.5" />
              )}
              <span className="hidden sm:inline">导出</span>
              <ChevronDown className="h-3 w-3 opacity-60" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => download("md")}>
              <FileText />
              Markdown（.md）
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => download("docx")}>
              <FileDown />
              Word（.docx）
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => download("pdf")}>
              <FileDown />
              PDF（.pdf）
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}

/** memo：标题栏不随流式 token 变化重渲染 */
export const ReportHeader = memo(ReportHeaderInner);
