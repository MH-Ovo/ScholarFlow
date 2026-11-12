"""学术源适配器：把不同源的返回值统一成 SourceResult。

当前覆盖：
- arxiv：search_papers_result
- web：search_web_result
- cnki：search_cnki（CnkiSearchResult → SourceResult）

后续新增源只需要实现 `SourceAdapter.search()` 并注册到 ADAPTERS；
LangGraph 侧仍可按现有 fanout 调用，不必改节点签名。
"""
import time

from app.integrations import cnki_client
from app.integrations.arxiv_client import search_papers_result
from app.integrations.fault_injection import inject_fault
from app.integrations.source_result import (
    SourceAdapter,
    SourceFault,
    SourceResult,
    SourceStatus,
)
from app.integrations.web_search import search_web_result

_CNKI_STATUS = {
    cnki_client.ST_OK: SourceStatus.OK,
    cnki_client.ST_NO_HITS: SourceStatus.NO_HITS,
    cnki_client.ST_RATE_LIMITED: SourceStatus.RATE_LIMITED,
    cnki_client.ST_UNREACHABLE: SourceStatus.UNREACHABLE,
    cnki_client.ST_DEGRADED: SourceStatus.DEGRADED,
}


def _fault_result(source: str, exc: SourceFault, started: float) -> SourceResult:
    return SourceResult(
        source,
        exc.status,
        detail=exc.detail,
        latency_ms=int((time.perf_counter() - started) * 1000),
        attempts=1,
        degraded=exc.status == SourceStatus.DEGRADED,
    )


class ArxivAdapter:
    name = "arxiv"

    async def search(self, query: str, max_results: int = 5) -> SourceResult:
        started = time.perf_counter()
        try:
            inject_fault(self.name)
        except SourceFault as exc:
            return _fault_result(self.name, exc, started)
        result = await search_papers_result(query, max_results)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result


class WebAdapter:
    name = "web"

    async def search(self, query: str, max_results: int = 5) -> SourceResult:
        started = time.perf_counter()
        try:
            inject_fault(self.name)
        except SourceFault as exc:
            return _fault_result(self.name, exc, started)
        result = await search_web_result(query, max_results)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result


class CnkiAdapter:
    name = "cnki"

    async def search(self, query: str, max_results: int = 5) -> SourceResult:
        started = time.perf_counter()
        try:
            inject_fault(self.name)
        except SourceFault as exc:
            return _fault_result(self.name, exc, started)
        res = await cnki_client.search_cnki(query, max_results=max_results)
        return SourceResult(
            self.name,
            _CNKI_STATUS.get(res.status, SourceStatus.ERROR),
            items=res.items,
            detail=res.detail,
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=res.status == cnki_client.ST_DEGRADED,
        )


ADAPTERS: dict[str, SourceAdapter] = {
    "arxiv": ArxivAdapter(),
    "web": WebAdapter(),
    "cnki": CnkiAdapter(),
}


def get_adapter(name: str) -> SourceAdapter:
    return ADAPTERS[name]
