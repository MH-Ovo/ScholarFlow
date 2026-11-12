"""网页搜索：DeepSeek 自研 Search API（只使用 DEEPSEEK_API_KEY）。

对话模型可以换成 SenseNova/Qwen/GLM/Ollama，但搜索仍固定走 DeepSeek：
1. 复用 DEEPSEEK_API_KEY，与对话模型解耦；
2. 自研索引对中文站点召回更好，与本项目的中文学术调研场景匹配；
3. 返回结构带 authority（0-4 权威性）与 date（发布时间）。

对外提供两层：
- `search_web_result()`：统一 SourceResult，区分 no_hits / timeout / rate_limited / auth_failed / error
- `search_web()`：兼容旧调用，只返回 items
"""
import time

import httpx

from app.core.config import get_settings
from app.integrations.source_result import SourceResult, SourceStatus

settings = get_settings()

DEEPSEEK_SEARCH_API = "https://api.deepseek.com/v1/tools/search"

# summary 字段实测可达 4000+ 字符。全部塞进上下文会挤占报告生成的 token 预算，
# 这里截断到兼顾信息量与成本的折中长度（Tavily 版原为 260）。
SUMMARY_MAX_CHARS = 700


def _http_status(code: int) -> SourceStatus:
    if code == 429:
        return SourceStatus.RATE_LIMITED
    if code in (401, 403):
        return SourceStatus.AUTH_FAILED
    if code >= 500:
        return SourceStatus.UNREACHABLE
    return SourceStatus.ERROR


async def search_web_result(query: str, max_results: int = 5) -> SourceResult:
    started = time.perf_counter()
    # 搜索只依赖 DeepSeek key；不能用 llm_enabled 判断，
    # 否则换成 SenseNova/其他对话模型后会误判为“搜索可用”。
    if not settings.deepseek_api_key.strip():
        return SourceResult(
            "web", SourceStatus.AUTH_FAILED,
            detail="missing DEEPSEEK_API_KEY",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    payload = {
        "query": query,
        "count": max(1, min(max_results, 50)),  # 接口上限 50
        "need_content": False,                   # 正文留给 Fetch 接口按需抓取
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                DEEPSEEK_SEARCH_API,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
    except httpx.TimeoutException as exc:
        return SourceResult(
            "web", SourceStatus.TIMEOUT, detail=f"timeout: {str(exc)[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except httpx.HTTPStatusError as exc:
        return SourceResult(
            "web", _http_status(exc.response.status_code),
            detail=f"http_{exc.response.status_code}: {exc.response.text[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except httpx.RequestError as exc:
        return SourceResult(
            "web", SourceStatus.UNREACHABLE, detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        data = resp.json()
    except ValueError as exc:
        return SourceResult(
            "web", SourceStatus.INVALID_RESPONSE, detail=f"invalid_json: {str(exc)[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    results: list[dict] = []
    for item in data.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                # summary 比 snippet 信息量大，优先用它
                "snippet": (item.get("summary") or item.get("snippet") or "")[:SUMMARY_MAX_CHARS],
                "meta": {
                    "score": item.get("rerank_score", 0.0),
                    "authority": item.get("authority", -1),
                    "date": item.get("date"),
                    "site_name": item.get("site_name"),
                },
            }
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    if not results:
        return SourceResult("web", SourceStatus.NO_HITS, detail="empty_result", latency_ms=elapsed)
    return SourceResult("web", SourceStatus.OK, items=results, latency_ms=elapsed)


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """兼容旧调用；错误仍返回 []，但需要状态时请用 search_web_result。"""
    return (await search_web_result(query, max_results)).items
