import type {
  AnalyticsSummary,
  KbChunkDetail,
  KbDetail,
  KbDocumentPage,
  KbStats,
  SharedReport,
  SourceFulltext,
  SourceStatItem,
  TaskDetail,
  TaskSummary,
} from "@/types";

const BASE = "/api/v1";

// SSE 直连后端：Next dev 代理会缓冲 SSE 流（实测 317 个 token 全部在 26.8s 时
// 一簇到达、且丢事件导致报告缺字）。NEXT_PUBLIC_SSE_BASE 留空则同源走代理。
const SSE_BASE = process.env.NEXT_PUBLIC_SSE_BASE ?? "";

const TOKEN_KEY = "kra-auth-token";

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string) {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function unauthorized() {
  setToken("");
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("auth:required"));
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (resp.status === 401) {
    unauthorized();
    throw new Error("未认证，请先输入访问密钥");
  }
  if (!resp.ok) {
    throw new Error(`API ${resp.status}: ${await resp.text()}`);
  }
  return resp.json() as Promise<T>;
}

async function uploadError(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: string };
    if (body?.detail) return `上传失败: ${body.detail}`;
  } catch {
    // 非 JSON 响应，走状态码
  }
  return `上传失败: ${resp.status}`;
}

export type StreamEvent = { event: string; data: Record<string, unknown> };

/** fetch 版 SSE：支持 Authorization header 与 AbortController（EventSource 不支持）。 */
async function streamResearch(
  taskId: string,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${SSE_BASE}${BASE}/research/${taskId}/stream`, {
    headers: { Accept: "text/event-stream", ...authHeaders() },
    signal,
  });
  if (resp.status === 401) {
    unauthorized();
    throw new Error("未认证");
  }
  if (!resp.ok || !resp.body) throw new Error(`SSE ${resp.status}`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx = buffer.indexOf("\n\n");
    while (idx >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "message";
      const dataLines: string[] = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) {
        try {
          onEvent({ event, data: JSON.parse(dataLines.join("\n")) });
        } catch {
          /* 忽略心跳/注释行 */
        }
      }
      idx = buffer.indexOf("\n\n");
    }
  }
}

export const api = {
  login: (apiKey: string) =>
    request<{ token: string; user_id: string; tenant_id: string; expires_in: number }>(
      "/auth/login",
      { method: "POST", body: JSON.stringify({ api_key: apiKey }) },
    ),

  me: () =>
    request<{ user_id: string; tenant_id: string; via: string; auth_enabled: boolean }>(
      "/auth/me",
    ),

  logout: () => {
    setToken("");
    return request<{ ok: boolean }>("/auth/logout", { method: "POST" }).catch(() => ({ ok: true }));
  },

  createResearch: (query: string, lang: string = "zh") =>
    request<{ task_id: string; title: string }>("/research", {
      method: "POST",
      body: JSON.stringify({ query, lang }),
    }),

  listResearch: (limit = 20) => request<TaskSummary[]>(`/research?limit=${limit}`),

  getResearch: (taskId: string) => request<TaskDetail>(`/research/${taskId}`),

  followup: (taskId: string, query: string, lang: string = "zh") =>
    request<{ ok: boolean }>(`/research/${taskId}/followup`, {
      method: "POST",
      body: JSON.stringify({ query, lang }),
    }),

  cancelResearch: (taskId: string) =>
    request<{ result: string }>(`/research/${taskId}/cancel`, { method: "POST" }),

  retryResearch: (taskId: string, force = false) =>
    request<{ result: string }>(
      `/research/${taskId}/retry${force ? "?force=true" : ""}`,
      { method: "POST" },
    ),

  deleteResearch: (taskId: string) =>
    request<{ ok: boolean }>(`/research/${taskId}`, { method: "DELETE" }),

  streamResearch,

  /** 知网来源：下载全文并登记入库（同步下载，解析/向量化在后台） */
  fetchSourceFulltext: (taskId: string, refNo: number) =>
    request<SourceFulltext>(`/research/${taskId}/sources/${refNo}/fulltext`, {
      method: "POST",
    }),

  exportReport: async (
    taskId: string,
    format: "md" | "docx" | "pdf" = "md",
  ): Promise<Blob> => {
    const resp = await fetch(`${BASE}/research/${taskId}/export?format=${format}`, {
      headers: authHeaders(),
    });
    if (resp.status === 401) {
      unauthorized();
      throw new Error("未认证");
    }
    if (!resp.ok) throw new Error(`导出失败: ${resp.status}`);
    return resp.blob();
  },

  createShare: (taskId: string) =>
    request<{ token: string; path: string; expires_at: string | null }>(
      `/research/${taskId}/share`,
      { method: "POST" },
    ),

  getShared: (token: string) => request<SharedReport>(`/share/${token}`),

  sourceStats: () => request<{ items: SourceStatItem[] }>("/sources/stats"),

  analyticsSummary: () => request<AnalyticsSummary>("/analytics/summary"),

  exportUrl: (taskId: string) => `${BASE}/research/${taskId}/export`,

  streamUrl: (taskId: string) => `${SSE_BASE}${BASE}/research/${taskId}/stream`,

  // ---------- 知识库 ----------
  uploadDocuments: async (files: File[]) => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    const resp = await fetch(`${BASE}/documents`, {
      method: "POST",
      body: form,
      headers: authHeaders(),
    });
    if (resp.status === 401) {
      unauthorized();
      throw new Error("未认证，请先输入访问密钥");
    }
    if (!resp.ok) throw new Error(await uploadError(resp));
    return resp.json() as Promise<{
      items: { id: number; name: string; duplicate?: boolean }[];
    }>;
  },

  listDocuments: (params?: {
    q?: string;
    status?: string;
    doc_type?: string;
    page?: number;
    page_size?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.q) qs.set("q", params.q);
    if (params?.status) qs.set("status", params.status);
    if (params?.doc_type) qs.set("doc_type", params.doc_type);
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<KbDocumentPage>(`/documents${suffix}`);
  },

  getDocument: (id: number) => request<KbDetail>(`/documents/${id}`),

  retryDocument: (id: number) =>
    request<{ ok: boolean }>(`/documents/${id}/retry`, { method: "POST" }),

  reindexDocument: (id: number) =>
    request<{ ok: boolean }>(`/documents/${id}/reindex`, { method: "POST" }),

  deleteDocument: (id: number) =>
    request<{ ok: boolean }>(`/documents/${id}`, { method: "DELETE" }),

  kbStats: () => request<KbStats>("/kb/stats"),

  kbChunk: (documentId: number, chunkIndex: number) =>
    request<KbChunkDetail>(`/kb/chunk?document_id=${documentId}&chunk_index=${chunkIndex}`),
};
