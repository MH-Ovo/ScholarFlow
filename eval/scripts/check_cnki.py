r"""知网连通性冒烟：跑 N 次检索，输出状态与耗时。

用途：
- 知网 A/B 之前先跑一次，确认 treatment 臂不会因站点限流整体失效；
- 排查 `unreachable` / `rate_limited` 是站点状态还是代码问题。

用法：
    cd apps/api
    ..\..\.venv\Scripts\python.exe ..\..\eval\scripts\check_cnki.py
    ..\..\.venv\Scripts\python.exe ..\..\eval\scripts\check_cnki.py --repeats 5

退出码：0 = 全部 ok/no_hits；1 = 出现 rate_limited/unreachable。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))

OK_STATUS = {"ok", "no_hits", "degraded"}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="检索增强生成 知识库")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()

    from app.integrations import cnki_mcp
    from app.integrations.cnki_client import search_cnki

    print(f"exe={cnki_mcp.resolve_exe()}")
    print(f"available={cnki_mcp.available()}")
    if not cnki_mcp.available():
        print("未找到 cnki-mcp 可执行文件")
        return 1

    bad = 0
    for i in range(1, max(1, args.repeats) + 1):
        started = time.time()
        result = await search_cnki(args.query, args.limit)
        elapsed = time.time() - started
        print(
            f"[{i}/{args.repeats}] status={result.status} "
            f"items={len(result.items)} elapsed={elapsed:.1f}s "
            f"detail={(result.detail or '')[:160]}",
            flush=True,
        )
        for it in result.items[:2]:
            print(f"  - {(it.get('title') or '')[:70]}", flush=True)
        bad += result.status not in OK_STATUS
        if i < args.repeats:
            await asyncio.sleep(2)
    print(f"OK={args.repeats - bad}/{args.repeats}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
