"""Playwright 冒烟：启动临时后端 + Next dev，检查首页/知识库/资料库可渲染。

不依赖 Qdrant / LLM：RESEARCH_WORKER_COUNT=0、WARMUP_MODELS=false、AUTH_ENABLED=false。
用法（仓库根目录）：
    .venv\\Scripts\\python.exe eval\\scripts\\ui_smoke.py
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "apps" / "api"
WEB_DIR = ROOT / "apps" / "web"
API_PORT = 8902
WEB_PORT = 5174


def _wait_port(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    raise RuntimeError(f"端口 {port} 在 {timeout}s 内未就绪")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="scholarflow-ui-"))
    db_path = (tmp / "ui.db").as_posix()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "AUTH_ENABLED": "true",
        "AUTH_LOCALHOST_BYPASS": "false",
        "API_KEYS": "ui-key:default:ui",
        "RESEARCH_WORKER_COUNT": "0",
        "WARMUP_MODELS": "false",
        "CNKI_ENABLED": "false",
        "RERANK_ENABLED": "false",
        "QDRANT_URL": "http://127.0.0.1:1",
    }
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(API_PORT), "--log-level", "warning"],
        cwd=str(API_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        _stop(api)
        print("npm 未找到，无法启动前端")
        return 2
    web = subprocess.Popen(
        [npm, "run", "dev", "--", "-p", str(WEB_PORT)],
        cwd=str(WEB_DIR),
        env={
            **env,
            "API_PROXY_TARGET": f"http://127.0.0.1:{API_PORT}",
            "NEXT_PUBLIC_SSE_BASE": "",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=(os.name == "nt"),
    )
    try:
        _wait_port(API_PORT)
        _wait_port(WEB_PORT, timeout=90)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(base_url=f"http://127.0.0.1:{WEB_PORT}")
            page.goto("/", wait_until="networkidle")
            # 远程/禁用回环放行时先过访问密钥门
            if page.get_by_placeholder("访问密钥").count():
                page.get_by_placeholder("访问密钥").fill("ui-key")
                page.get_by_role("button", name="进入").click()
                page.wait_for_load_state("networkidle")
            page.get_by_text("问一个问题").first.wait_for(timeout=15000)
            assert page.get_by_text("Knowledge Base").first.is_visible()
            assert page.get_by_text("Library").first.is_visible()

            page.goto("/knowledge-base", wait_until="networkidle")
            page.get_by_text("点击或拖拽上传文档").wait_for(timeout=15000)

            page.goto("/library", wait_until="networkidle")
            page.get_by_text("全部研究报告").wait_for(timeout=15000)

            browser.close()
        print("UI_SMOKE_OK")
        return 0
    finally:
        _stop(web)
        _stop(api)


if __name__ == "__main__":
    raise SystemExit(main())
