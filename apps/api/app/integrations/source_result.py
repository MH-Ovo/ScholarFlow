"""统一学术源返回值：SourceResult（status / items / detail）。

背景：
- arXiv 失败返回 []，Web 失败也返回 []，调用方无法区分“没搜到”和“源故障”；
- 知网已经有 CnkiSearchResult，但字段和状态值与其他源不统一。

约定：
- 只有 ok / no_hits / degraded 可以被上层当作“源可用”；
- rate_limited / timeout / auth_failed / unreachable / invalid_response / error
  必须带 detail，并允许上层跳过该源继续跑，而不是中断整个调研。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class SourceStatus(str, Enum):
    OK = "ok"
    NO_HITS = "no_hits"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    INVALID_RESPONSE = "invalid_response"
    ERROR = "error"

    @property
    def usable(self) -> bool:
        """源是否仍可参与后续融合（no_hits 是正常空结果）。"""
        return self in (SourceStatus.OK, SourceStatus.NO_HITS, SourceStatus.DEGRADED)


class SourceFault(Exception):
    """故障注入/适配器内部使用的受控异常，不应冒泡到调研主流程。"""

    def __init__(self, status: SourceStatus, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class SourceResult:
    source: str
    status: SourceStatus
    items: list[dict] = field(default_factory=list)
    detail: str = ""
    latency_ms: int = 0
    attempts: int = 1
    degraded: bool = False

    @property
    def ok(self) -> bool:
        return self.status == SourceStatus.OK

    @property
    def usable(self) -> bool:
        return self.status.usable

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status.value,
            "items": len(self.items),
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "degraded": self.degraded,
        }


class SourceAdapter(Protocol):
    """所有学术源适配器的最小协议。"""

    name: str

    async def search(self, query: str, max_results: int = 5) -> SourceResult: ...
