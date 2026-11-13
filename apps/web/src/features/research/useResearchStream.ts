"use client";

import { useEffect } from "react";
import { api } from "@/lib/api";
import { useResearchStore } from "./researchStore";

/** 完成时自动校准：用 DB 完整报告替换流式 buffer（修复流式 token 丢失导致的缺字/不完整） */
function calibrateReport(taskId: string) {
  api
    .getResearch(taskId)
    .then((d) => {
      const store = useResearchStore.getState();
      const dbMd = d.report?.markdown ?? "";
      if (!dbMd) return;
      if (dbMd.length > store.reportBuffer.length) {
        store.applySnapshot(d, true);
      }
    })
    .catch(() => undefined);
}

/**
 * 订阅研究任务 SSE：
 * - fetch + ReadableStream（EventSource 不能带 Authorization 头）
 * - 连接建立后先拉 DB 快照，再接收增量事件
 * - 断线指数退避重连；服务端已提供 DB 事件重放，不会漏事件
 * - 任务终态或 stream_end 后退出
 */
export function useResearchStream() {
  const taskId = useResearchStore((s) => s.taskId);
  const phase = useResearchStore((s) => s.phase);
  const runNonce = useResearchStore((s) => s.runNonce);

  useEffect(() => {
    if (!taskId || phase !== "streaming") return;
    let closed = false;
    const ctrl = new AbortController();

    const handle = (event: string, data: Record<string, unknown>) => {
      useResearchStore.getState().applyEvent(event, data);
      if (event === "report_completed" || event === "stream_end") {
        calibrateReport(taskId);
      }
    };

    const run = async () => {
      let attempt = 0;
      while (!closed) {
        try {
          const snapshot = await api.getResearch(taskId);
          if (closed) return;
          const store = useResearchStore.getState();
          if (store.phase !== "streaming") return;
          store.applySnapshot(snapshot);

          await api.streamResearch(taskId, (e) => handle(e.event, e.data), ctrl.signal);
          if (closed) return;
          // 服务端正常关闭但没给终态事件：补一次快照兜底
          const d = await api.getResearch(taskId);
          if (closed) return;
          const st = useResearchStore.getState();
          if (st.phase === "streaming") {
            if (d.status === "done") {
              st.applySnapshot(d, true);
              st.applyEvent("stream_end", {});
            } else if (d.status === "failed") {
              st.applyEvent("error", { message: (d.stats as Record<string, unknown>)?.error ?? "任务失败" });
            } else if (d.status === "cancelled") {
              st.applyEvent("cancelled", {});
            }
          }
          return;
        } catch (e) {
          if (closed) return;
          const msg = e instanceof Error ? e.message : String(e);
          if (msg.includes("未认证")) return; // AuthGate 会接管
          attempt += 1;
          if (attempt > 6) {
            useResearchStore.getState().setInterrupted();
            return;
          }
          await new Promise((r) => setTimeout(r, Math.min(8000, 1200 * attempt)));
        }
      }
    };

    run();
    return () => {
      closed = true;
      ctrl.abort();
    };
  }, [taskId, phase, runNonce]);
}
