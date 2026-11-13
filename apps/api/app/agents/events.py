"""任务事件总线：DB 持久化 + 批量写入，SSE 按 (task_id, id) 轮询读取。

为什么不再用内存 deque/Queue：
- 进程重启后事件丢失，重连无法重放；
- 多 worker 时 SSE 可能落到没有该任务的进程，实时事件永远收不到。

当前实现把 task_events 当作唯一事实源：emit 只入队，后台 writer 批量落库；
SSE 每 200ms 按 last_id 增量查询。单机 SQLite（WAL）与多 worker 均可用。
"""
import asyncio
import time
from typing import Any

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.research import TaskEvent

settings = get_settings()

_BATCH_SIZE = 200
_FLUSH_INTERVAL = 0.05


class EventBus:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._writer: asyncio.Task | None = None
        self._stopping = False

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self._writer is not None:
            return
        self._queue = asyncio.Queue()
        self._stopping = False
        self._writer = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._writer is not None:
            self._writer.cancel()
            try:
                await self._writer
            except (asyncio.CancelledError, Exception):
                pass
            self._writer = None
        await self.flush()

    # ---------- 写 ----------
    def emit(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        if self._queue is None:
            # 未启动（脚本/单测直接调节点）时退回同步写入，保证事件不丢
            _write_sync(task_id, event, data)
            return
        self._queue.put_nowait((task_id, {"event": event, "data": data}))

    async def _writer_loop(self) -> None:
        assert self._queue is not None
        while not self._stopping:
            batch: list[tuple[str, dict[str, Any]]] = []
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), timeout=0.5))
                while len(batch) < _BATCH_SIZE:
                    batch.append(self._queue.get_nowait())
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                pass
            if not batch:
                continue
            try:
                async with SessionLocal() as db:
                    for task_id, payload in batch:
                        db.add(
                            TaskEvent(
                                task_id=task_id,
                                event=payload["event"],
                                data_json=payload["data"],
                            )
                        )
                    await db.commit()
            except Exception:
                # 事件落库失败不能拖垮研究主流程；退避后继续
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(_FLUSH_INTERVAL)

    async def flush(self) -> None:
        """把队列里的事件写完（测试/优雅关闭用）。"""
        if self._queue is None:
            return
        while not self._queue.empty():
            batch: list[tuple[str, dict[str, Any]]] = []
            while not self._queue.empty() and len(batch) < _BATCH_SIZE:
                batch.append(self._queue.get_nowait())
            async with SessionLocal() as db:
                for task_id, payload in batch:
                    db.add(
                        TaskEvent(
                            task_id=task_id,
                            event=payload["event"],
                            data_json=payload["data"],
                        )
                    )
                await db.commit()

    # ---------- 读 ----------
    async def history(self, task_id: str) -> list[dict[str, Any]]:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id)
                    .order_by(TaskEvent.id)
                )
            ).scalars().all()
        return [_row(r) for r in rows]

    async def read_after(
        self, task_id: str, last_id: int, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id, TaskEvent.id > last_id)
                    .order_by(TaskEvent.id)
                    .limit(limit)
                )
            ).scalars().all()
        return [_row(r) for r in rows]

    async def has_event(self, task_id: str, event: str) -> bool:
        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(TaskEvent.id)
                    .where(TaskEvent.task_id == task_id, TaskEvent.event == event)
                    .limit(1)
                )
            ).first()
        return row is not None

    async def reset(self, task_id: str) -> None:
        """追问重跑前清空历史事件。"""
        if self._queue is not None:
            # 先排空该任务尚未落库的事件，避免 reset 后又被 writer 写回来
            kept: list[tuple[str, dict[str, Any]]] = []
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item[0] != task_id:
                    kept.append(item)
            for item in kept:
                self._queue.put_nowait(item)
        async with SessionLocal() as db:
            await db.execute(delete(TaskEvent).where(TaskEvent.task_id == task_id))
            await db.commit()

    async def close(self, task_id: str) -> None:
        await self.reset(task_id)


def _row(row: TaskEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "event": row.event,
        "data": row.data_json or {},
        "ts": row.created_at.isoformat() if row.created_at else "",
    }


def _write_sync(task_id: str, event: str, data: dict[str, Any]) -> None:
    """无事件循环时的兜底：直接在调用线程写入（仅脚本/单测会走到）。"""
    import sqlite3

    from app.db.session import SYNC_DATABASE_URL

    if not SYNC_DATABASE_URL.startswith("sqlite:///"):
        return
    path = SYNC_DATABASE_URL.replace("sqlite:///", "", 1)
    try:
        con = sqlite3.connect(path, timeout=5)
        con.execute(
            "INSERT INTO task_events (task_id, event, data_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, event, __import__("json").dumps(data, ensure_ascii=False),
             time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        con.commit()
        con.close()
    except Exception:
        pass


EVENT_BUS = EventBus()
