"""API 请求/响应契约。"""
from pydantic import BaseModel, Field


class CreateResearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    lang: str = Field(default="zh", pattern="^(zh|en)$")  # 报告语言偏好（个性化设置）


class FollowupRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    lang: str = Field(default="zh", pattern="^(zh|en)$")


class CreateResearchResponse(BaseModel):
    task_id: str
    title: str


class StepOut(BaseModel):
    step_key: str
    label: str
    order_index: int
    status: str
    meta: dict = {}


class SourceOut(BaseModel):
    ref_no: int
    type: str
    title: str
    url: str
    snippet: str
    relevance: float
    source_label: str
    page_nos: list[int] = []
    meta: dict = {}


class SourceFulltextOut(BaseModel):
    """知网来源全文获取/入库状态。"""
    state: str  # pending / in_progress / indexed / failed
    document_id: int | None = None
    document_name: str = ""
    size_bytes: int = 0
    chunk_count: int = 0
    reason: str | None = None


class ReportOut(BaseModel):
    id: int
    title: str
    summary: str
    markdown: str
    version: int
    degraded: bool = False
    degraded_reason: str = ""


class TaskSummaryOut(BaseModel):
    id: str
    title: str
    query: str
    status: str
    created_at: str


class ClaimOut(BaseModel):
    claim_text: str
    ref_no: int
    supported: bool | None = None


class TaskDetailOut(BaseModel):
    id: str
    title: str
    query: str
    status: str
    steps: list[StepOut]
    sources: list[SourceOut]
    report: ReportOut | None
    reports: list[ReportOut] = []  # 多轮对话历史报告（按 version 升序）
    graph: dict
    stats: dict
    messages: list[dict]
    claims: list[ClaimOut] = []
