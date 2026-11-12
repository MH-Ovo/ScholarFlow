"""CNKI-Enhanced-MCP 的进程管理与任务轮询。

只负责"怎么跟 MCP 通信"，不含业务语义。

## 为什么用常驻 worker task

最初是"每次调用新建 stdio 会话"，结果单次检索 120s+ 超时——因为 MCP 进程随会话
存活，每次都要重启浏览器，冷启动 20–60 秒。

改成模块级长连接后，检索降到 4 秒。但直接在外层持有连接会踩到 anyio 的坑：

    RuntimeError: Attempted to exit cancel scope in a different task
    than it was entered in

`stdio_client` 内部用 anyio 的 task group，必须在**同一个任务**里进入和退出。
用 `AsyncExitStack` 在调用方任务持有连接，退出时跨任务就会崩。

所以改成 worker task 模式：一个常驻协程持有连接，所有调用通过队列转发。
连接的进入与退出都发生在该协程内，不再跨任务。

## 其他约束

- **串行调用**：知网对并发非常敏感（实测连续两次检索间隔 6 分钟仍被要求退避），
  队列天然串行，顺带保证两次调用之间的最小间隔。
- **进程会死**：worker 崩溃后下一次调用会自动重建。
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings

settings = get_settings()

# 两次调用之间的最小间隔（秒）。浏览器已常驻，不需要太长。
SEARCH_INTERVAL = 1.5
DOWNLOAD_INTERVAL = 2.0

# 单个操作的总超时（秒）。
# 检索正常 3–5 秒，给 60 秒足够；此前设 120 秒，一旦知网侧卡住会把整个调研
# 拖到 138 秒（实测），得不偿失。
SEARCH_TIMEOUT = 60.0
# 下载按体积放行：实测 1.2MB 约 13s、5MB 约 29s，大文件可能更久
DOWNLOAD_TIMEOUT = 300.0
ENRICH_TIMEOUT = 45.0

_EXE_CANDIDATE = Path("cnki-enhanced") / "CNKI-Enhanced-MCP-Windows-x64" / "cnki-mcp.exe"


def resolve_exe() -> str | None:
    """定位 cnki-mcp.exe：配置项 → LOCALAPPDATA 默认路径 → PATH。"""
    configured = (getattr(settings, "cnki_mcp_exe", "") or "").strip()
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p)

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cand = Path(local) / _EXE_CANDIDATE
        if cand.is_file():
            return str(cand)

    import shutil

    return shutil.which("cnki-mcp")


def resolve_data_dir() -> str:
    configured = (getattr(settings, "cnki_data_dir", "") or "").strip()
    if configured:
        return configured
    exe = resolve_exe()
    return str(Path(exe).parent / "data") if exe else ""


def available() -> bool:
    return resolve_exe() is not None


def find_downloaded_paper(record_ref: str) -> Path | None:
    """在 MCP 的 downloads 目录里找已下载过的 PDF。

    `download_paper` 的落盘文件名形如 `<标题>_<record_ref[4:12]>.pdf`；
    命中后直接复用本地文件，不再请求知网——既省掉 13–40 秒，也避开限流。
    返回最新的匹配文件；没有则返回 None。
    """
    ref = (record_ref or "").strip()
    if ref.startswith("rec_"):
        ref = ref[4:]
    suffix = ref[:8].lower()
    if not suffix:
        return None

    data_dir = resolve_data_dir()
    if not data_dir:
        return None
    downloads = Path(data_dir) / "downloads"
    if not downloads.is_dir():
        return None

    matches = [
        p for p in downloads.rglob("*.pdf")
        if p.is_file() and p.stat().st_size > 0
        and p.name.lower().endswith(f"_{suffix}.pdf")
    ]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


_stale_cleaned = False


def cleanup_stale_browsers() -> None:
    """清理持有 MCP profile 的残留浏览器进程（仅 Windows，进程内只做一次）。

    MCP 的浏览器用独立 profile（`<data>/browser/profile`）。父进程被杀或异常退出时
    浏览器子进程可能残留，下次启动会返回 `BROWSER_PROFILE_IN_USE`，而任务状态
    一直是 running——表现为"检索卡住 60 秒后超时"，很难看出真实原因（实测踩过）。

    只在进程内首次建连时清理一次，避免把正在使用的实例杀掉。
    """
    global _stale_cleaned
    if _stale_cleaned or sys.platform != "win32":
        return
    _stale_cleaned = True

    data_dir = resolve_data_dir()
    if not data_dir:
        return

    # 用安装目录名做特征匹配，比 data 这种通用名更安全
    marker = "cnki-enhanced"
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=20,
        )
    except Exception:
        pass  # 清理失败不影响主流程，最坏情况是本次调用报 profile 占用


def _parse(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"raw": obj}
    except json.JSONDecodeError:
        return {"raw": text}


class _Request:
    __slots__ = ("name", "args", "timeout", "future")

    def __init__(self, name: str, args: dict, timeout: float, future: asyncio.Future):
        self.name = name
        self.args = args
        self.timeout = timeout
        self.future = future


class CnkiWorker:
    """常驻协程持有 MCP 连接，串行处理请求。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_Request | None] | None = None
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._start_error: BaseException | None = None
        self._last_call: dict[str, float] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def alive(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self._ready is not None
            and self._ready.is_set()
            and self._start_error is None
        )

    async def _ensure_started(self) -> None:
        if self.alive:
            return
        cleanup_stale_browsers()
        await self._shutdown()

        self._queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._start_error = None
        self._task = asyncio.create_task(self._run())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=90)
        except asyncio.TimeoutError:
            await self._shutdown()
            raise RuntimeError("cnki_mcp_start_timeout") from None

        if self._start_error is not None:
            err = self._start_error
            await self._shutdown()
            raise RuntimeError(f"cnki_mcp_start_failed:{err}") from None

    async def _shutdown(self) -> None:
        """通知 worker 退出并等待其结束。连接在 worker 任务内关闭。"""
        if self._queue is not None and self._task is not None and not self._task.done():
            try:
                await self._queue.put(None)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:
                self._task.cancel()
                try:
                    await self._task
                except Exception:
                    pass
        self._task = None
        self._queue = None
        self._ready = None

    async def _run(self) -> None:
        """常驻协程：建立连接 → 处理队列 → 关闭连接。全在同一任务内。"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        exe = resolve_exe()
        if not exe:
            self._start_error = RuntimeError("cnki_mcp_not_found")
            assert self._ready is not None
            self._ready.set()
            return

        params = StdioServerParameters(
            command=exe,
            args=["serve"],
            env={**os.environ, "CNKI_DATA_DIR": resolve_data_dir()},
        )

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert self._ready is not None
                    self._ready.set()

                    assert self._queue is not None
                    while True:
                        req = await self._queue.get()
                        if req is None:
                            return
                        if req.future.cancelled():
                            continue
                        try:
                            obj = await self._execute(session, req)
                            if not req.future.done():
                                req.future.set_result(obj)
                        except Exception as exc:
                            if not req.future.done():
                                req.future.set_exception(exc)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._start_error = exc
            if self._ready is not None and not self._ready.is_set():
                self._ready.set()
            raise

    async def _execute(self, session, req: _Request) -> dict:
        """节流 → 调用 → 轮询到终态。"""
        kind = "download" if "download" in req.name else "search"
        interval = DOWNLOAD_INTERVAL if kind == "download" else SEARCH_INTERVAL
        elapsed = time.monotonic() - self._last_call.get(kind, 0.0)
        if interval - elapsed > 0:
            await asyncio.sleep(interval - elapsed)
        self._last_call[kind] = time.monotonic()

        result = await asyncio.wait_for(
            session.call_tool(req.name, req.args), timeout=req.timeout
        )
        obj = _parse("".join(c.text for c in (result.content or []) if hasattr(c, "text")))

        op_id = obj.get("operation_id")
        # skipped 是终态（on_verification=skip 时工具主动跳过人工验证资源）。
        # 不把它当终态会一直轮询到 300s 超时，把"需要验证"误报成"知网超时"。
        if not op_id or obj.get("status") in ("completed", "failed", "cancelled", "skipped"):
            return obj

        # awaiting_user = 知网要求人工过滑块。后台任务不可能满足，等下去只会
        # 白等到超时（实测卡满 60 秒才失败）。立刻抛出，让上层走降级。
        if obj.get("status") == "awaiting_user":
            raise RuntimeError("cnki_needs_verification")

        deadline = time.monotonic() + req.timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(1.2)
            try:
                st = await session.call_tool(
                    "operation_status",
                    {"operation_id": op_id, "wait_seconds": 15},
                )
            except Exception:
                await asyncio.sleep(2)
                continue
            obj = _parse("".join(c.text for c in (st.content or []) if hasattr(c, "text")))
            status = obj.get("status")
            if status in ("completed", "failed", "cancelled", "skipped"):
                return obj
            if status == "awaiting_user":
                raise RuntimeError("cnki_needs_verification")

        # 抛异常而不是返回 {"status": "timeout"}：上层把"没有 records"一律当空结果，
        # 返回伪状态会被误判成 "no_hits"，把故障伪装成正常（实测踩到过，耗时 125s
        # 却显示 done/hits=0）。
        raise RuntimeError(f"cnki_operation_timeout:{op_id}")

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        timeout: float = SEARCH_TIMEOUT,
    ) -> dict:
        await self._ensure_started()
        if self._queue is None:
            raise RuntimeError("cnki_mcp_not_started")

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Request(name, args, timeout, fut))
        try:
            return await asyncio.wait_for(fut, timeout=timeout + 30)
        except asyncio.TimeoutError:
            # 超时后连接状态不可信，直接重建
            await self._shutdown()
            raise RuntimeError(f"cnki_mcp_timeout:{name}") from None


# 模块级单例。FastAPI 单进程单事件循环可以复用；
# 事件循环变了（脚本里多次 asyncio.run）时自动重建。
_singleton: CnkiWorker | None = None


def get_worker() -> CnkiWorker:
    global _singleton
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _singleton is None or _singleton.loop is not loop:
        _singleton = CnkiWorker()
        _singleton.loop = loop
    return _singleton


async def call_tool(
    name: str,
    args: dict[str, Any],
    *,
    kind: str = "search",
    timeout: float = SEARCH_TIMEOUT,
) -> dict:
    """调用工具并等待完成。kind 只用于日志语义，节流由 worker 内部按工具名判定。"""
    if kind == "download" and timeout == SEARCH_TIMEOUT:
        timeout = DOWNLOAD_TIMEOUT
    return await get_worker().call(name, args, timeout=timeout)


async def warmup() -> dict:
    """预热：提前拉起 MCP 进程与浏览器，避免首次调研吃冷启动。"""
    if not available():
        return {"status": "unavailable", "detail": "未找到 cnki-mcp 可执行文件"}

    started = time.monotonic()
    try:
        await get_worker()._ensure_started()
        return {"status": "ok", "elapsed_ms": int((time.monotonic() - started) * 1000)}
    except Exception as exc:
        return {"status": "unavailable", "detail": f"{type(exc).__name__}: {str(exc)[:100]}"}


async def health() -> dict:
    if not available():
        return {"status": "unavailable", "detail": "未找到 cnki-mcp 可执行文件"}
    worker = get_worker()
    if worker.alive:
        return {"status": "ok", "detail": "long-lived worker"}
    return await warmup()
