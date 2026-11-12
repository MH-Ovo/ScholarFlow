r"""LLM 连通性冒烟：不发调研任务，只发一条 hello。

用法：
    cd apps/api
    ..\..\.venv\Scripts\python.exe ..\..\eval\scripts\check_llm.py
    ..\..\.venv\Scripts\python.exe ..\..\eval\scripts\check_llm.py --model SenseChat-Turbo

读取顺序：LLM_* → DEEPSEEK_*（回退）。不会打印 API key。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "api"))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="")
    args = parser.parse_args()

    from langchain_openai import ChatOpenAI
    from app.core.config import get_settings

    s = get_settings()
    model = args.model or s.chat_model
    print(f"provider={s.chat_provider}")
    print(f"base_url={s.chat_base_url}")
    print(f"model={model}")
    print(f"api_key_set={bool(s.chat_api_key)} (不会打印内容)")

    if not s.chat_api_key:
        print("缺少 LLM_API_KEY / DEEPSEEK_API_KEY")
        return 2

    chat = ChatOpenAI(
        model=model,
        api_key=s.chat_api_key,
        base_url=s.chat_base_url,
        temperature=0.1,
        max_retries=1,
    )
    started = time.time()
    try:
        resp = await chat.ainvoke("用一句话回答：你好，请回复“连通成功”。")
        print(f"elapsed={time.time() - started:.1f}s")
        print("content=", str(resp.content)[:300])
        usage = getattr(resp, "usage_metadata", None)
        print("usage=", usage)
        return 0
    except Exception as exc:
        print(f"FAILED {type(exc).__name__}: {str(exc)[:500]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
