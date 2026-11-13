"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Quote } from "lucide-react";
import { api } from "@/lib/api";
import type { ClaimItem } from "@/types";

/** 逐句引用核查：每句带 [n] 的论断及其核查结果（无依据的句子用红色标出） */
export function ClaimPanel({
  taskId,
  enabled,
  refreshKey,
}: {
  taskId: string | null;
  enabled: boolean;
  refreshKey: number;
}) {
  const { data } = useQuery({
    queryKey: ["claims", taskId, refreshKey],
    queryFn: () => api.getResearch(taskId as string).then((d) => d.claims ?? []),
    enabled: Boolean(taskId) && enabled,
    staleTime: 30_000,
  });

  const claims = data ?? [];
  if (!claims.length) return null;

  // 同一句可能有多条 ref_no 记录，按 claim_text 合并
  const grouped = new Map<string, ClaimItem & { refs: number[] }>();
  for (const c of claims) {
    const row = grouped.get(c.claim_text);
    if (row) {
      if (!row.refs.includes(c.ref_no)) row.refs.push(c.ref_no);
      if (c.supported === false) row.supported = false;
    } else {
      grouped.set(c.claim_text, { ...c, refs: [c.ref_no] });
    }
  }
  const rows = Array.from(grouped.values());
  const unsupported = rows.filter((r) => r.supported === false).length;
  const checked = rows.filter((r) => r.supported !== null).length;

  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-sm font-semibold">
          <Quote className="h-4 w-4 text-primary" />
          逐句引用
        </div>
        <span className="text-xs text-muted-foreground">
          {rows.length} 句 · 已核查 {checked}
          {unsupported > 0 && (
            <>
              {" · "}
              <span className="font-medium text-rose-600">无依据 {unsupported}</span>
            </>
          )}
        </span>
      </div>
      <ul className="mt-2.5 space-y-1.5">
        {rows.map((c) => (
          <li key={c.claim_text} className="flex gap-2 text-xs leading-relaxed">
            {c.supported === false ? (
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-600" />
            ) : c.supported ? (
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600" />
            ) : (
              <span className="mt-0.5 h-3.5 w-3.5 shrink-0 rounded-full border border-muted-foreground/40" />
            )}
            <span className="min-w-0 flex-1 text-muted-foreground">
              {c.claim_text}
              <span className="ml-1 text-primary">
                {c.refs.map((n) => `[${n}]`).join("")}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
