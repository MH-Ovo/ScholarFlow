"use client";

import { BookCheck, Database, FileCheck2, Info, Target } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useCountUp } from "@/hooks/use-count-up";
import { formatNumber } from "@/lib/format";
import type { AnalyticsSummary } from "@/types";

function formatCompact(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function BigStat({
  value,
  label,
  suffix = "",
}: {
  value: number;
  label: string;
  suffix?: string;
}) {
  const { ref, value: animated } = useCountUp(value);
  return (
    <div ref={ref} className="px-1 py-2.5">
      {/* 数据是最有说服力的内容：40px 等宽数字占据视觉重量 */}
      <div className="text-[36px] font-bold leading-none tracking-tight tabular-nums">
        {formatNumber(animated)}
        {suffix && <span className="ml-0.5 text-[20px] font-semibold text-muted-foreground">{suffix}</span>}
      </div>
      <div className="mt-2 text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </div>
    </div>
  );
}

/** 首页「Research Statistics」全局统计：2×2 大数字网格（累计口径） */
export function ResearchStatsPanel({ summary }: { summary: AnalyticsSummary | undefined }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex min-w-0 items-center gap-1.5 text-sm font-semibold">
          <span className="truncate">Research Statistics</span>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="shrink-0 cursor-help">
                <Info className="h-3.5 w-3.5 text-muted-foreground/70" />
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-[280px]">
              累计统计：Total Research=历史研究总次数；Knowledge Sources=累计检索来源数；
              Documents Hit=累计命中文档数；Accuracy Rate=历史平均相关度。
            </TooltipContent>
          </Tooltip>
        </div>
        <div className="mt-1 grid grid-cols-2 gap-x-2">
          <BigStat value={summary?.total_research ?? 0} label="Total Research" />
          <BigStat value={summary?.knowledge_sources ?? 0} label="Sources" />
          <BigStat value={summary?.documents_processed ?? 0} label="Docs Hit" />
          <BigStat value={summary?.accuracy_rate ?? 0} label="Accuracy" suffix="%" />
        </div>
        <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 border-t pt-2.5 text-[11px] text-muted-foreground">
          <span title="已完成任务的平均耗时">
            平均耗时 <b className="text-foreground">{summary?.latency?.avg_sec ?? 0}s</b>
          </span>
          <span title="已完成任务的 P95 耗时">
            P95 <b className="text-foreground">{summary?.latency?.p95_sec ?? 0}s</b>
          </span>
          <span title="累计 LLM token（输入/输出）">
            Tokens{" "}
            <b className="text-foreground">
              {formatCompact(summary?.llm?.tokens_in ?? 0)}/{formatCompact(summary?.llm?.tokens_out ?? 0)}
            </b>
          </span>
          <span title="按配置单价估算，未配置单价时为 0">
            成本 <b className="text-foreground">${(summary?.llm?.cost_usd ?? 0).toFixed(2)}</b>
          </span>
        </div>
      </CardContent>
    </Card>
  );
}
