"""轻量可观测层：本地 JSONL trace（默认）+ 可选 Langfuse 导出。

设计原则：
- 应用进程只写本地 JSONL，不依赖 Langfuse 网络；避免观测拖慢调研。
- `eval/scripts/export_langfuse.py` 读取 JSONL 后批量上报 Langfuse。
- 没有 Langfuse key 时，本地 trace 仍然完整记录节点耗时/token，便于排查与答辩展示。

记录两类事件：
- node: 节点开始/完成/失败/跳过（duration_ms 来自节点 meta）
- llm:  报告流式生成 / 结构化抽取（耗时、输入/输出 token 估算）
"""
import json
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken

TRACE_DIR = Path(__file__).resolve().parents[4] / "eval" / "results" / "traces"
_current_task: ContextVar[str | None] = ContextVar("trace_task_id", default=None)
_lock = threading.Lock()
_encoder = None


def set_task(task_id: str):
    return _current_task.set(task_id)


def reset_task(token) -> None:
    _current_task.reset(token)


def current_task() -> str | None:
    return _current_task.get()


def _tokens(text: str) -> int:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    try:
        return len(_encoder.encode(text or ""))
    except Exception:
        return 0


def record(event: str, **fields: Any) -> None:
    task_id = fields.pop("task_id", None) or current_task() or "unknown"
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "event": event,
        **fields,
    }
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        path = TRACE_DIR / f"{datetime.now().strftime('%Y%m%d')}.jsonl"
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # 观测失败不能影响主流程
        pass


def record_node(
    task_id: str,
    node: str,
    status: str,
    duration_ms: float | None = None,
    meta: dict | None = None,
) -> None:
    if status not in ("done", "failed", "skipped"):
        return
    record(
        "node",
        task_id=task_id,
        node=node,
        status=status,
        duration_ms=round(float(duration_ms), 1) if duration_ms is not None else None,
        meta=meta or {},
    )


def record_llm(
    kind: str,
    model: str,
    prompt: str,
    output: str,
    duration_ms: float,
    usage: dict | None = None,
    error: str | None = None,
) -> None:
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or _tokens(prompt))
    output_tokens = int(usage.get("output_tokens") or _tokens(output))
    record(
        "llm",
        kind=kind,
        model=model,
        duration_ms=round(float(duration_ms), 1),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_chars=len(prompt or ""),
        output_chars=len(output or ""),
        error=error,
    )


def record_task(task_id: str, status: str, query: str, duration_ms: float) -> None:
    record("task", task_id=task_id, status=status, query=query, duration_ms=round(duration_ms, 1))
