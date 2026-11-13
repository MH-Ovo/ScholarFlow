/** 与后端 API 对齐的 TypeScript 契约 */

export type StepStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "paused"
  | "skipped"
  | "cancelled";
export type TaskStatus = "queued" | "running" | "done" | "failed" | "cancelled";
export type SourceType = "enterprise" | "paper" | "web" | "news" | "patent" | "report" | "cnki";

export interface StepInfo {
  step_key: string;
  label: string;
  order_index: number;
  status: StepStatus;
  meta: Record<string, unknown>;
}

export interface SourceMeta {
  authors?: string;
  journal?: string;
  year?: number | null;
  doi?: string;
  record_ref?: string;
  evidence_level?: string; // abstract / fulltext / title_only
  fulltext_available?: string; // yes / no / unknown
  document_id?: number;
  document_status?: string; // pending / parsing / embedding / indexed / failed
  chunk_count?: number;
  document_error?: string;
  [key: string]: unknown;
}

export interface SourceItem {
  ref_no: number;
  type: SourceType;
  title: string;
  url: string;
  snippet: string;
  relevance: number;
  source_label: string;
  page_nos?: number[];
  meta?: SourceMeta;
}

/** 知网来源全文获取/入库状态（POST /research/{id}/sources/{ref}/fulltext） */
export interface SourceFulltext {
  state: "pending" | "in_progress" | "indexed" | "failed";
  document_id: number | null;
  document_name: string;
  size_bytes: number;
  chunk_count: number;
  reason: string | null;
}

export interface GraphNode {
  id: string;
  label: string;
  group: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  label: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface RunStats {
  duration_sec: number;
  sources_count: number;
  docs_processed: number;
  relevance_avg: number;
  citations_count?: number;
  degraded_stages?: string[];
  degraded_reasons?: Record<string, string>;
  report_degraded?: boolean;
  report_degraded_reason?: string;
}

export interface ReportInfo {
  id: number;
  title: string;
  summary: string;
  markdown: string;
  version: number;
  degraded?: boolean;
  degraded_reason?: string;
}

export interface ChatMessage {
  role: "user" | "agent";
  content: string;
}

export interface ClaimItem {
  claim_text: string;
  ref_no: number;
  supported: boolean | null;
}

export interface TaskDetail {
  id: string;
  title: string;
  query: string;
  status: TaskStatus;
  steps: StepInfo[];
  sources: SourceItem[];
  report: ReportInfo | null;
  reports: ReportInfo[];
  graph: GraphData;
  stats: Partial<RunStats>;
  messages: ChatMessage[];
  claims?: ClaimItem[];
}

export interface TaskSummary {
  id: string;
  title: string;
  query: string;
  status: TaskStatus;
  created_at: string;
}

export interface SourceStatItem {
  category: string;
  label: string;
  count: number;
}

export interface AnalyticsSummary {
  total_research: number;
  knowledge_sources: number;
  documents_processed: number;
  accuracy_rate: number;
  latency?: { avg_sec: number; p95_sec: number; samples: number };
  llm?: { tokens_in: number; tokens_out: number; calls: number; cost_usd: number };
}

/** SSE 事件载荷 */
export interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

/** 研究过程时间线条目（C1：Agent 思考直播） */
export type TimelineKind =
  | "router"
  | "step_started"
  | "step_completed"
  | "step_failed"
  | "step_skipped"
  | "sources"
  | "grade"
  | "rewrite"
  | "report_done"
  | "degraded"
  | "cancelled"
  | "error";

export interface TimelineEntry {
  id: number;
  ts: number;
  kind: TimelineKind;
  text: string;
}

/** 知识库文档 */
export interface KbDocument {
  id: number;
  name: string;
  doc_type: string;
  size_bytes: number;
  status: string; // pending/parsing/embedding/indexed/failed
  error_msg: string;
  chunk_count: number;
  expected_chunks: number;
  is_partial: boolean;
  created_at: string;
}

export interface KbDocumentPage {
  items: KbDocument[];
  total: number;
  page: number;
  page_size: number;
}

export interface KbChunk {
  id: number;
  chunk_index: number;
  text: string;
}

export interface KbDetail extends KbDocument {
  chunks: KbChunk[];
}

export interface KbStats {
  documents: number;
  chunks: number;
  indexed: number;
  processing: number;
  failed: number;
  vector_store_ready: boolean;
}

/** 单个知识库片段全文（来源溯源查看） */
export interface KbChunkDetail {
  document_id: number;
  document_name: string;
  chunk_index: number;
  text: string;
}
