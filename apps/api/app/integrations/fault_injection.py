"""单源故障注入（仅评测/演练用，默认关闭）。

用法：
    FAULT_INJECT_SOURCE=arxiv,web
    FAULT_INJECT_MODE=timeout|rate_limit|auth|error|empty|unreachable
    FAULT_INJECT_RATE=1.0

适配器在真正发请求前调用 inject_fault()；命中时抛 SourceFault，
由适配器转成结构化 SourceResult，从而验证“单源失败不中断主流程”。
"""
import random

from app.core.config import get_settings
from app.integrations.source_result import SourceFault, SourceStatus

_MODE_STATUS = {
    "timeout": SourceStatus.TIMEOUT,
    "rate_limit": SourceStatus.RATE_LIMITED,
    "auth": SourceStatus.AUTH_FAILED,
    "auth_failed": SourceStatus.AUTH_FAILED,
    "error": SourceStatus.ERROR,
    "unreachable": SourceStatus.UNREACHABLE,
    "empty": SourceStatus.NO_HITS,
}


def _matches(source: str, configured: str) -> bool:
    names = {x.strip().lower() for x in (configured or "").split(",") if x.strip()}
    return "*" in names or source.lower() in names


def inject_fault(source: str) -> None:
    settings = get_settings()
    if not _matches(source, settings.fault_inject_source):
        return
    rate = max(0.0, min(1.0, float(settings.fault_inject_rate)))
    if rate < 1.0 and random.random() > rate:
        return
    mode = (settings.fault_inject_mode or "timeout").strip().lower()
    status = _MODE_STATUS.get(mode, SourceStatus.ERROR)
    raise SourceFault(status, f"fault_injected:{source}:{mode}")
