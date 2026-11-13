"use client";

import { Loader2, Download } from "lucide-react";
import { memo, useState } from "react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";

/** 研究页标题栏：报告标题 + 导出（分享/更多在功能上线前不渲染，避免死按钮） */
function ReportHeaderInner({
  title,
  taskId,
  canExport,
}: {
  title: string;
  taskId: string | null;
  canExport: boolean;
}) {
  const [busy, setBusy] = useState(false);

  const download = async () => {
    if (!taskId || busy) return;
    setBusy(true);
    try {
      const blob = await api.exportReport(taskId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `report-${taskId.slice(0, 8)}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "导出失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center justify-between gap-3 border-b bg-background/95 px-4 py-3 md:px-6">
      <h1 className="truncate text-[15px] font-semibold" title={title || "研究报告"}>
        {title || "研究报告"}
      </h1>
      <Button
        variant="outline"
        size="sm"
        className="h-8 shrink-0 gap-1.5 text-xs"
        disabled={!canExport || !taskId || busy}
        onClick={download}
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
        导出报告
      </Button>
    </div>
  );
}

/** memo：标题栏不随流式 token 变化重渲染 */
export const ReportHeader = memo(ReportHeaderInner);
