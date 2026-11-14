"""研究任务生命周期：持久化队列 → worker 领取 → LangGraph → 取消/重试/删除。

任务状态：queued → running → done / failed / cancelled
- 队列在 DB（research_tasks.status），进程重启不会丢任务；
- 多 worker 用单条 UPDATE ... RETURNING 原子领取，不会重复执行；
- 事件由 EventBus 落 task_events，SSE 直接读库，多 worker 可服务同一任务。
"""
import asyncio
import time
import uuid
from datetime import timedelta

from sqlalchemy import delete as sql_delete, select, text

from app.agents import mock_data, nodes
from app.agents.events import EVENT_BUS
from app.agents.graph import get_research_graph
from app.core import trace
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.research import (
    Citation,
    Message,
    Report,
    ReportShare,
    ResearchStep,
    ResearchTask,
    STEP_DEFS,
    Source,
    TaskEvent,
    utcnow,
)

settings = get_settings()

_RUNNING: dict[str, asyncio.Task] = {}
_WORKERS: list[asyncio.Task] = []
_SHUTTING_DOWN = False
_LAST_REAP = 0.0


# ---------------- 创建 / 队列 ----------------

async def create_task(
    query: str, lang: str = "zh", tenant_id: str = "default", user_id: str = ""
) -> str:
    topic = mock_data.extract_topic(query)
    task_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(
            ResearchTask(
                id=task_id,
                title=mock_data.task_title(topic),
                query=query,
                lang=lang,
                status="queued",
                tenant_id=tenant_id,
                user_id=user_id,
                queued_at=utcnow(),
            )
        )
        # 先 flush 父行：SQLite 开启 foreign_keys=ON 后，同事务内先插子表会触发 FK 失败
        await db.flush()
        for idx, (key, label) in enumerate(STEP_DEFS):
            status = "paused" if key == "graph_build" else "pending"
            db.add(
                ResearchStep(
                    task_id=task_id, step_key=key, label=label,
                    order_index=idx, status=status,
                )
            )
        db.add(Message(task_id=task_id, role="user", content=query))
        await db.commit()
    return task_id


async def recover_tasks() -> None:
    """服务重启：running 任务重新排队；超过最大尝试次数则失败。"""
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(ResearchTask).where(ResearchTask.status == "running"))
        ).scalars().all()
        now = utcnow()
        for row in rows:
            # 只恢复“本轮生命周期内”遗留的任务；几小时前的 running 说明进程早已异常退出
            started = row.started_at
            if started is not None and started.tzinfo is None:
                from datetime import timezone

                started = started.replace(tzinfo=timezone.utc)
            stale = (
                started is None
                or (now - started).total_seconds() > float(settings.research_task_timeout_sec)
            )
            if stale:
                row.status = "failed"
                row.error = "服务重启且任务已超过超时时间"
                row.completed_at = now
                continue
            if int(row.attempt or 0) < settings.research_max_attempts:
                row.status = "queued"
                row.queued_at = utcnow()
                row.started_at = None
                row.cancel_requested = False
                row.error = "服务重启，已重新排队"
            else:
                row.status = "failed"
                row.error = "服务重启且超过最大重试次数"
                row.completed_at = utcnow()
        if rows:
            await db.commit()
            print(f"[kra] 已恢复 {len(rows)} 个运行中任务")


async def start_workers() -> None:
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = False
    if _WORKERS:
        return
    count = max(0, int(settings.research_worker_count))
    if count == 0:
        print("[kra] research_worker_count=0，worker 未启动（测试/只读模式）")
        return
    for i in range(count):
        _WORKERS.append(asyncio.create_task(_worker_loop(i)))
    print(f"[kra] 研究 worker 已启动：{len(_WORKERS)} 个")


async def stop_workers() -> None:
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True
    for worker in _WORKERS:
        worker.cancel()
    for worker in _WORKERS:
        try:
            await worker
        except (asyncio.CancelledError, Exception):
            pass
    _WORKERS.clear()
    for run in list(_RUNNING.values()):
        run.cancel()
    _RUNNING.clear()


async def _worker_loop(index: int) -> None:
    while True:
        try:
            await _maybe_reap()
        except Exception:
            pass
        try:
            claimed = await _claim()
        except Exception as exc:  # 队列故障不能杀死 worker
            print(f"[kra][worker-{index}] claim 失败：{type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)
            continue
        if claimed is None:
            await asyncio.sleep(0.5)
            continue
        await _run_claimed(claimed)


async def _maybe_reap() -> None:
    """空闲 worker 每 60s 回收一次“进程崩溃后遗留的 running”任务。"""
    global _LAST_REAP
    now = time.monotonic()
    if now - _LAST_REAP < 60:
        return
    _LAST_REAP = now
    cutoff = utcnow() - timedelta(
        seconds=float(settings.research_task_timeout_sec)
    )
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ResearchTask).where(
                    ResearchTask.status == "running",
                    ResearchTask.started_at.is_not(None),
                    ResearchTask.started_at < cutoff,
                )
            )
        ).scalars().all()
        for row in rows:
            if row.cancel_requested:
                row.status = "cancelled"
                row.error = "取消请求超时"
                row.completed_at = utcnow()
            elif int(row.attempt or 0) < settings.research_max_attempts:
                row.status = "queued"
                row.queued_at = utcnow()
                row.started_at = None
                row.error = "worker 心跳超时，已重新排队"
            else:
                row.status = "failed"
                row.error = "worker 心跳超时且超过最大重试次数"
                row.completed_at = utcnow()
        if rows:
            await db.commit()


async def _claim() -> dict | None:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                text(
                    """
                    UPDATE research_tasks
                    SET status = 'running',
                        started_at = :now,
                        attempt = attempt + 1,
                        error = ''
                    WHERE id = (
                        SELECT id FROM research_tasks
                        WHERE status = 'queued'
                        ORDER BY COALESCE(queued_at, created_at) ASC
                        LIMIT 1
                    )
                    RETURNING id, query, lang, tenant_id, user_id, attempt
                    """
                ),
                {"now": utcnow()},
            )
        ).mappings().first()
        await db.commit()
    return dict(row) if row else None


# ---------------- 执行 ----------------

async def _run_claimed(row: dict) -> None:
    task_id = row["id"]
    token = trace.set_task(task_id)
    trace.start_usage()
    started = time.time()
    status = "done"
    EVENT_BUS.emit(task_id, "task_started", {"attempt": row.get("attempt", 1)})
    try:
        # 多轮历史：取本任务已完成轮次的 user/agent 消息；去掉本轮刚写入的用户提问
        async with SessionLocal() as db:
            msg_rows = (
                await db.execute(
                    select(Message)
                    .where(Message.task_id == task_id)
                    .order_by(Message.created_at)
                )
            ).scalars().all()
        history = [{"role": m.role, "content": m.content} for m in msg_rows]
        if history and history[-1]["role"] == "user" and history[-1]["content"] == row["query"]:
            history = history[:-1]
        history = history[-8:]

        graph = get_research_graph()
        run = asyncio.create_task(
            graph.ainvoke(
                {
                    "task_id": task_id,
                    "query": row["query"],
                    "original_query": row["query"],
                    "lang": row.get("lang") or "zh",
                    "tenant_id": row.get("tenant_id") or settings.default_tenant_id,
                    "history": history,
                    "metrics": {"t0": started},
                    "errors": [],
                }
            )
        )
        _RUNNING[task_id] = run
        await _supervise(run, task_id)
    except asyncio.CancelledError:
        if _SHUTTING_DOWN:
            # 服务优雅关闭：重新排队，下次启动继续（不是用户取消）
            status = "queued"
            await _requeue_on_shutdown(task_id)
        else:
            status = "cancelled"
            await _finish(task_id, "cancelled", "用户取消")
            await _close_steps(task_id, "cancelled")
            EVENT_BUS.emit(task_id, "cancelled", {"message": "任务已取消"})
    except asyncio.TimeoutError:
        status = "failed"
        message = f"任务超时（>{int(settings.research_task_timeout_sec)}s）"
        await _finish(task_id, "failed", message)
        await _close_steps(task_id, "failed", message)
        await nodes.on_error(task_id, message)
    except Exception as exc:  # 任何节点异常都收敛为失败任务，不中断 worker
        status = "failed"
        message = f"{type(exc).__name__}: {exc}"
        await _finish(task_id, "failed", message)
        await _close_steps(task_id, "failed", message)
        await nodes.on_error(task_id, message)
    finally:
        run = _RUNNING.pop(task_id, None)
        if run is not None and not run.done():
            run.cancel()
            try:
                await asyncio.gather(run, return_exceptions=True)
            except asyncio.CancelledError:
                pass
        trace.record_task(task_id, status, row["query"], (time.time() - started) * 1000)
        trace.reset_task(token)
        if not _SHUTTING_DOWN:
            EVENT_BUS.emit(task_id, "stream_end", {})
        await _ensure_terminal(task_id, status)


async def _supervise(run: asyncio.Task, task_id: str) -> None:
    deadline = time.monotonic() + float(settings.research_task_timeout_sec)
    while True:
        done, _ = await asyncio.wait({run}, timeout=1.0)
        if done:
            return run.result()
        if time.monotonic() > deadline:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            raise asyncio.TimeoutError
        async with SessionLocal() as db:
            row = await db.get(ResearchTask, task_id)
            if row is not None and row.cancel_requested:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                raise asyncio.CancelledError


async def _finish(task_id: str, status: str, error: str = "") -> None:
    async with SessionLocal() as db:
        row = await db.get(ResearchTask, task_id)
        if row is None:
            return
        row.status = status
        row.error = error[:2000]
        row.completed_at = utcnow()
        if error:
            row.stats_json = {**(row.stats_json or {}), "error": error}
        await db.commit()


async def _requeue_on_shutdown(task_id: str) -> None:
    async with SessionLocal() as db:
        row = await db.get(ResearchTask, task_id)
        if row is None:
            return
        row.status = "queued"
        row.queued_at = utcnow()
        row.started_at = None
        row.cancel_requested = False
        row.error = "服务关闭，已重新排队"
        await db.commit()
    await _close_steps(task_id, "pending", "服务关闭，待重新执行")


async def _ensure_terminal(task_id: str, status: str) -> None:
    """graph 正常返回但没落状态（例如 chat 早退）时补 done。"""
    if status != "done":
        return
    async with SessionLocal() as db:
        row = await db.get(ResearchTask, task_id)
        if row is not None and row.status == "running":
            row.status = "done"
            row.completed_at = row.completed_at or utcnow()
            await db.commit()


async def _close_steps(task_id: str, status: str, error: str = "") -> None:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ResearchStep).where(
                    ResearchStep.task_id == task_id,
                    ResearchStep.status.in_(["running", "pending"]),
                )
            )
        ).scalars().all()
        for row in rows:
            row.status = status
            row.finished_at = utcnow()
            row.meta_json = {**(row.meta_json or {}), "error": error} if error else row.meta_json
            EVENT_BUS.emit(
                task_id,
                "step_failed" if status == "failed" else "step_skipped",
                {"step": row.step_key, "label": row.label, "error": error},
            )
        await db.commit()


# ---------------- 追问 / 取消 / 重试 / 删除 ----------------

async def _delete_task_citations(db, task_id: str) -> None:
    """删除任务所有报告的 citation。

    追问/重试会删掉旧 sources 并重建；citation.source_id 有外键，必须先清理，
    否则 SQLite/PG 都会报 FOREIGN KEY constraint failed。
    历史报告正文仍保留（多轮展示），但旧 claim 明细不再展示。
    """
    report_ids = (
        await db.execute(select(Report.id).where(Report.task_id == task_id))
    ).scalars().all()
    if report_ids:
        await db.execute(sql_delete(Citation).where(Citation.report_id.in_(report_ids)))


async def followup(
    task_id: str, query: str, lang: str = "zh", tenant_id: str = "default"
) -> str:
    """追问：追加用户消息，重置步骤/来源后重新排队。

    返回 "ok" | "not_found" | "running"。
    """
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != tenant_id:
            return "not_found"
        if task.status in ("queued", "running"):
            return "running"
        task.status = "queued"
        task.queued_at = utcnow()
        task.started_at = None
        task.completed_at = None
        task.cancel_requested = False
        task.error = ""
        task.lang = lang
        task.query = query
        db.add(Message(task_id=task_id, role="user", content=query))
        steps = (
            await db.execute(select(ResearchStep).where(ResearchStep.task_id == task_id))
        ).scalars().all()
        for s in steps:
            s.status = "paused" if s.step_key == "graph_build" else "pending"
            s.started_at = None
            s.finished_at = None
            s.meta_json = {}
        await _delete_task_citations(db, task_id)
        await db.execute(sql_delete(Source).where(Source.task_id == task_id))
        await db.commit()
    await EVENT_BUS.reset(task_id)
    return "ok"


async def cancel(task_id: str, tenant_id: str = "default") -> str:
    """cancelling / cancelled / not_found / not_cancellable"""
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != tenant_id:
            return "not_found"
        if task.status == "queued":
            task.status = "cancelled"
            task.completed_at = utcnow()
            await db.commit()
            EVENT_BUS.emit(task_id, "cancelled", {"message": "任务已取消"})
            EVENT_BUS.emit(task_id, "stream_end", {})
            return "cancelled"
        if task.status == "running":
            task.cancel_requested = True
            await db.commit()
            return "cancelling"
        return "not_cancellable"


async def retry(
    task_id: str, tenant_id: str = "default", force: bool = False
) -> str:
    """ok / not_found / not_retryable / max_attempts

    force=True 允许对已完成的降级报告重新生成（手动重试）。
    """
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != tenant_id:
            return "not_found"
        if task.status not in ("failed", "cancelled") and not (
            force and task.status == "done"
        ):
            return "not_retryable"
        if not force and int(task.attempt or 0) >= settings.research_max_attempts:
            return "max_attempts"
        if force:
            task.attempt = 0
        task.status = "queued"
        task.queued_at = utcnow()
        task.started_at = None
        task.completed_at = None
        task.cancel_requested = False
        task.error = ""
        steps = (
            await db.execute(select(ResearchStep).where(ResearchStep.task_id == task_id))
        ).scalars().all()
        for s in steps:
            s.status = "paused" if s.step_key == "graph_build" else "pending"
            s.started_at = None
            s.finished_at = None
            s.meta_json = {}
        await _delete_task_citations(db, task_id)
        await db.execute(sql_delete(Source).where(Source.task_id == task_id))
        await db.commit()
    await EVENT_BUS.reset(task_id)
    return "ok"


async def delete_task(task_id: str, tenant_id: str = "default") -> str:
    """ok / not_found / running"""
    async with SessionLocal() as db:
        task = await db.get(ResearchTask, task_id)
        if task is None or task.tenant_id != tenant_id:
            return "not_found"
        if task.status == "running":
            return "running"
        report_ids = (
            await db.execute(select(Report.id).where(Report.task_id == task_id))
        ).scalars().all()
        if report_ids:
            await db.execute(sql_delete(Citation).where(Citation.report_id.in_(report_ids)))
        await db.execute(sql_delete(ReportShare).where(ReportShare.task_id == task_id))
        await db.execute(sql_delete(Report).where(Report.task_id == task_id))
        await db.execute(sql_delete(Source).where(Source.task_id == task_id))
        await db.execute(sql_delete(ResearchStep).where(ResearchStep.task_id == task_id))
        await db.execute(sql_delete(Message).where(Message.task_id == task_id))
        await db.execute(sql_delete(TaskEvent).where(TaskEvent.task_id == task_id))
        await db.execute(sql_delete(ResearchTask).where(ResearchTask.id == task_id))
        await db.commit()
    return "ok"
