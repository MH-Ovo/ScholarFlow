"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RotateCcw, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useReveal } from "@/hooks/use-reveal";
import { api } from "@/lib/api";
import { relativeTime } from "@/lib/format";

const STATUS_LABEL: Record<string, { text: string; variant: "default" | "secondary" | "destructive" }> = {
  queued: { text: "排队中", variant: "default" },
  running: { text: "进行中", variant: "default" },
  done: { text: "已完成", variant: "secondary" },
  failed: { text: "失败", variant: "destructive" },
  cancelled: { text: "已取消", variant: "destructive" },
};

/** Library：历史研究报告列表 */
export default function LibraryPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { data: tasks, isPending } = useQuery({
    queryKey: ["library"],
    queryFn: () => api.listResearch(50),
    refetchInterval: 10000,
  });
  const revealRef = useReveal<HTMLDivElement>([tasks?.length]);

  const remove = useMutation({
    mutationFn: api.deleteResearch,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["library"] }),
    onError: (e: Error) => window.alert(e.message),
  });
  const retry = useMutation({
    mutationFn: (id: string) => api.retryResearch(id),
    onSuccess: (_r, id) => router.push(`/research/${id}`),
    onError: (e: Error) => window.alert(e.message),
  });

  const onDelete = (id: string, title: string, status: string) => {
    if (status === "running") {
      window.alert("任务正在运行，请先在研究页停止后再删除。");
      return;
    }
    if (window.confirm(`确认删除「${title || id}」？报告、来源与事件将一并删除。`)) {
      remove.mutate(id);
    }
  };

  return (
    <ScrollArea className="min-w-0 flex-1">
      <div ref={revealRef} className="mx-auto max-w-[860px] px-5 py-8 md:px-8">
        <h1 className="text-xl font-bold">Library</h1>
        <p className="mt-1 text-sm text-muted-foreground">全部研究报告</p>
        <div className="mt-6 space-y-2.5">
          {isPending && (
            <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 加载中...
            </div>
          )}
          {(tasks ?? []).map((t, i) => {
            const status = STATUS_LABEL[t.status] ?? STATUS_LABEL.running;
            return (
              <Card
                key={t.id}
                className="reveal cursor-pointer transition-all duration-300 hover:-translate-y-0.5 hover:border-primary/50 hover:shadow-md focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                style={{ "--d": `${Math.min(i, 8) * 50}ms` } as React.CSSProperties}
                onClick={() => router.push(`/research/${t.id}`)}
              >
                <CardContent className="flex items-center justify-between gap-3 p-4">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium" title={t.title || t.query}>
                      {t.title || t.query}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {relativeTime(t.created_at)}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    {(t.status === "failed" || t.status === "cancelled") && (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-muted-foreground"
                        title="重试"
                        onClick={(e) => {
                          e.stopPropagation();
                          retry.mutate(t.id);
                        }}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-muted-foreground hover:text-rose-600"
                      title="删除"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDelete(t.id, t.title || t.query, t.status);
                      }}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                    <Badge variant={status.variant}>{status.text}</Badge>
                  </div>
                </CardContent>
              </Card>
            );
          })}
          {tasks && tasks.length === 0 && (
            <div className="py-16 text-center text-sm text-muted-foreground">
              还没有研究报告，从首页发起你的第一个研究吧
            </div>
          )}
        </div>
      </div>
    </ScrollArea>
  );
}
