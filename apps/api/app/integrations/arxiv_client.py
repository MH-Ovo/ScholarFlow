"""arXiv 论文检索（免费公开 API，无需 Key）。

对外提供两层：
- `search_papers_result()`：统一 SourceResult，区分 no_hits / timeout / rate_limited / auth_failed / error
- `search_papers()`：兼容旧调用，只返回 items
"""
import re
import time
import xml.etree.ElementTree as ET

import httpx

from app.integrations.source_result import SourceResult, SourceStatus

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"


def _http_status(exc: httpx.HTTPStatusError) -> SourceStatus:
    code = exc.response.status_code
    if code == 429:
        return SourceStatus.RATE_LIMITED
    if code in (401, 403):
        return SourceStatus.AUTH_FAILED
    if code >= 500:
        return SourceStatus.UNREACHABLE
    return SourceStatus.ERROR


async def search_papers_result(query: str, max_results: int = 5) -> SourceResult:
    started = time.perf_counter()
    # arXiv 对英文检索友好；中文查询退化为关键词英文映射失败时也能容错
    q = re.sub(r"\s+", " ", query).strip() or "artificial intelligence"
    params = {
        "search_query": f"all:{q}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(ARXIV_API, params=params)
            resp.raise_for_status()
    except httpx.TimeoutException as exc:
        return SourceResult(
            "arxiv", SourceStatus.TIMEOUT, detail=f"timeout: {str(exc)[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except httpx.HTTPStatusError as exc:
        return SourceResult(
            "arxiv", _http_status(exc),
            detail=f"http_{exc.response.status_code}: {exc.response.text[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except httpx.RequestError as exc:
        return SourceResult(
            "arxiv", SourceStatus.UNREACHABLE, detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        return SourceResult(
            "arxiv", SourceStatus.INVALID_RESPONSE, detail=f"invalid_xml: {str(exc)[:120]}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    results: list[dict] = []
    for entry in root.findall(f"{ATOM}entry"):
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        summary = " ".join((entry.findtext(f"{ATOM}summary") or "").split())
        link = entry.findtext(f"{ATOM}id") or ""
        published = (entry.findtext(f"{ATOM}published") or "")[:10]
        arxiv_id = link.rsplit("/", 1)[-1] if link else ""
        if not title:
            continue
        results.append(
            {
                "title": f"arXiv:{arxiv_id} {title}" if arxiv_id else title,
                "url": link,
                "snippet": summary[:260],
                "meta": {"published": published, "arxiv_id": arxiv_id},
            }
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    if not results:
        return SourceResult("arxiv", SourceStatus.NO_HITS, detail="empty_result", latency_ms=elapsed)
    return SourceResult("arxiv", SourceStatus.OK, items=results, latency_ms=elapsed)


async def search_papers(query: str, max_results: int = 5) -> list[dict]:
    """兼容旧调用；错误仍返回 []，但需要状态时请用 search_papers_result。"""
    return (await search_papers_result(query, max_results)).items
