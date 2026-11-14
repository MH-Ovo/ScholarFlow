"""ORM 模型：研究任务 / 步骤 / 来源 / 引用 / 报告 / 消息 / 来源统计 / 知识库文档。"""
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 与效果图2 的 5 张步骤卡片一一对应
STEP_DEFS: list[tuple[str, str]] = [
    ("kb_search", "查询本地知识库"),
    ("paper_search", "检索学术论文"),
    ("web_search", "搜索网页信息"),
    ("cnki_search", "检索知网文献"),
    ("graph_build", "建立知识关系图谱"),
    ("report_write", "生成分析报告"),
]


class ResearchTask(Base):
    __tablename__ = "research_tasks"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200), default="")
    query: Mapped[str] = mapped_column(sa.Text, default="")
    mode: Mapped[str] = mapped_column(sa.String(20), default="deep")
    status: Mapped[str] = mapped_column(sa.String(20), default="queued", index=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), default="default", index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), default="")
    lang: Mapped[str] = mapped_column(sa.String(8), default="zh")
    attempt: Mapped[int] = mapped_column(sa.Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    error: Mapped[str] = mapped_column(sa.Text, default="")
    duration_sec: Mapped[float] = mapped_column(sa.Float, default=0.0)
    stats_json: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    graph_json: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    queued_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class ResearchStep(Base):
    __tablename__ = "research_steps"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("research_tasks.id"), index=True
    )
    step_key: Mapped[str] = mapped_column(sa.String(40))
    label: Mapped[str] = mapped_column(sa.String(60))
    order_index: Mapped[int] = mapped_column(sa.Integer, default=0)
    status: Mapped[str] = mapped_column(sa.String(20), default="pending")  # pending/running/done/failed
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    meta_json: Mapped[dict] = mapped_column(sa.JSON, default=dict)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("research_tasks.id"), index=True
    )
    ref_no: Mapped[int] = mapped_column(sa.Integer, default=0)  # 报告中的 [n]
    type: Mapped[str] = mapped_column(sa.String(20), index=True)  # enterprise/paper/web/news/patent/report
    title: Mapped[str] = mapped_column(sa.String(300), default="")
    url: Mapped[str] = mapped_column(sa.String(600), default="")
    snippet: Mapped[str] = mapped_column(sa.Text, default="")
    relevance: Mapped[float] = mapped_column(sa.Float, default=0.0)  # 0~1
    source_label: Mapped[str] = mapped_column(sa.String(60), default="")  # 本地知识库/学术论文/网页
    meta_json: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("reports.id"), index=True)
    ref_no: Mapped[int] = mapped_column(sa.Integer)
    source_id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("sources.id"))
    claim_text: Mapped[str] = mapped_column(sa.Text, default="")
    # 逐句核查结果：True=有依据 / False=核查判为无依据 / None=未核查（降级或旧数据）
    supported: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("research_tasks.id"), index=True
    )
    title: Mapped[str] = mapped_column(sa.String(200), default="")
    summary: Mapped[str] = mapped_column(sa.Text, default="")
    markdown: Mapped[str] = mapped_column(sa.Text, default="")
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), default="default", index=True)
    degraded: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    degraded_reason: Mapped[str] = mapped_column(sa.String(300), default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class ReportShare(Base):
    """报告只读分享：token 公开可读，绑定分享时的报告版本，可设过期时间。"""

    __tablename__ = "report_shares"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    task_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("research_tasks.id"), index=True
    )
    report_id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("reports.id"))
    tenant_id: Mapped[str] = mapped_column(sa.String(64), default="default", index=True)
    revoked: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("research_tasks.id"), index=True
    )
    role: Mapped[str] = mapped_column(sa.String(10))  # user / agent
    content: Mapped[str] = mapped_column(sa.Text, default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Document(Base):
    """本地知识库文档：上传 → 解析 → 切片 → 向量化 → 检索。"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.String(300), default="")       # 原始文件名（展示）
    doc_type: Mapped[str] = mapped_column(sa.String(10), default="txt")  # pdf/docx/md/txt
    size_bytes: Mapped[int] = mapped_column(sa.Integer, default=0)
    file_path: Mapped[str] = mapped_column(sa.String(500), default="")  # uploads 相对路径
    status: Mapped[str] = mapped_column(sa.String(20), default="pending")  # pending/parsing/embedding/indexed/failed
    tenant_id: Mapped[str] = mapped_column(sa.String(64), default="default", index=True)
    content_hash: Mapped[str] = mapped_column(sa.String(64), default="", index=True)
    error_msg: Mapped[str] = mapped_column(sa.String(300), default="")
    chunk_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    # 解析后实际切片数（截断前）；is_partial=True 表示只索引了前 N 片
    expected_chunks: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_partial: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("documents.id"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(sa.Integer, default=0)
    text: Mapped[str] = mapped_column(sa.Text, default="")
    parent_text: Mapped[str] = mapped_column(sa.Text, default="")  # Parent-Child：章节级上下文
    embedding_json: Mapped[list | None] = mapped_column(sa.JSON, nullable=True)


class TaskEvent(Base):
    """任务事件持久化：SSE 重放与多 worker 之间的唯一事实源。

    写入走 EventBus 的批量队列，读取按 (task_id, id) 索引轮询。
    """

    __tablename__ = "task_events"
    __table_args__ = (sa.Index("ix_task_events_task_id_id", "task_id", "id"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("research_tasks.id"), index=True
    )
    event: Mapped[str] = mapped_column(sa.String(40), default="")
    data_json: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
