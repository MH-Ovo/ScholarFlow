"""知网（CNKI）文献检索与全文下载。

主路径 —— 通过 cnki_mcp 调用 CNKI-Enhanced-MCP：
    structured_search  → 确定性字段检索，返回题录 + 摘要（含 record_ref）
    download_paper     → 下载 PDF 全文（实测无需人工验证）

为什么是这两条路：

1. **不用 read_online_html**。实测同一篇论文，HTML 阅读只给 7,877 字符且标记
   `scope: "trial"`（`extracted_characters == dom_characters` 说明提取完整、
   是页面本身只放这么多），而 PDF 下载拿到 8,771 字符的完整 7 页。HTML 路径还
   每次调用都要人工过滑块，后台任务不可能满足。

2. **PDF 下载不需要登录**。校园网 IP 已被识别为机构（实测"华南师范大学"），
   会议论文与核心期刊均实测可下载，`personal` 仍是 `not_observed`。

能力边界与已知缺陷见 `word/知网适配层技术实现文档.md`。
"""
import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings
from app.core.text import normalize_fullwidth
from app.integrations import cnki_mcp

settings = get_settings()

# 摘要写入 snippet 的长度；完整摘要另存 meta.abstract
SNIPPET_MAX_CHARS = 500

# 检索状态
ST_OK = "ok"                  # 正常返回
ST_NO_HITS = "no_hits"        # 检索成功但没有结果（正常状态）
ST_RATE_LIMITED = "rate_limited"   # 触发知网频率限制（可重试）
ST_UNREACHABLE = "unreachable"     # MCP 进程不可用
ST_DEGRADED = "degraded"           # 已降级到 site:cnki.net

_WS_RE = re.compile(r"[ \t]{2,}")


@dataclass
class CnkiSearchResult:
    """检索结果与状态。

    把 `status` 显式返回而不是只返回 list，是因为必须区分两件性质不同的事：
    "检索成功但没有相关内容"（no_hits，正常）与"检索根本没执行成功"
    （rate_limited / unreachable，故障）。混为一谈会让排查成本极高——
    这和此前"向量库故障被当作空结果"是同一类问题。
    """

    items: list[dict] = field(default_factory=list)
    status: str = ST_NO_HITS
    detail: str = ""


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", normalize_fullwidth(text or "")).strip()


def _map_record(rec: dict) -> dict | None:
    """把 structured_search 的记录映射成项目统一 schema。"""
    title = _clean(rec.get("title") or "")
    if not title:
        return None

    abstract = _clean(rec.get("abstract") or "")
    authors = rec.get("authors") or []
    if isinstance(authors, list):
        authors = ", ".join(str(a) for a in authors if a)
    keywords = rec.get("keywords") or []
    if isinstance(keywords, list):
        keywords = ", ".join(str(k) for k in keywords if k)
    institutions = rec.get("institutions") or []
    if isinstance(institutions, list):
        institutions = ", ".join(str(i) for i in institutions if i)

    year = rec.get("year")
    if not isinstance(year, int):
        y = str(year or "")[:4]
        year = int(y) if y.isdigit() else None

    return {
        "title": title,
        "url": str(rec.get("url") or ""),
        "snippet": abstract[:SNIPPET_MAX_CHARS],
        "meta": {
            "authors": str(authors),
            "journal": _clean(rec.get("source") or rec.get("journal") or ""),
            "year": year,
            "doi": str(rec.get("doi") or ""),
            "institutions": str(institutions),
            "keywords": str(keywords),
            "document_type": str(rec.get("document_type") or ""),
            "abstract": abstract,          # 完整摘要，报告生成时可取
            "record_ref": str(rec.get("record_ref") or ""),  # 供全文下载使用
            "evidence_level": "abstract",  # 明确标注：非全文
            "fulltext_available": "unknown",
            "degraded": False,
        },
    }


async def _enrich_metadata(items: list[dict]) -> list[dict]:
    """用 get_metadata 补全摘要、期刊名、作者。

    `structured_search` 只返回 record_ref / title / url，没有摘要——不补的话
    rerank 只能按标题判断相关性，知网源会和有全文切片的知识库严重不对等。
    """
    refs = [it["meta"].get("record_ref") for it in items if it["meta"].get("record_ref")]
    if not refs:
        return items

    try:
        obj = await cnki_mcp.call_tool(
            "get_metadata",
            {"record_refs": refs, "fields": ["abstract", "keywords", "doi", "authors", "source", "year", "institutions"]},
            kind="search",
            timeout=cnki_mcp.ENRICH_TIMEOUT,
        )
    except Exception:
        return items  # 补全失败不影响主流程，只是摘要为空

    records = (obj.get("result") or {}).get("records") or []
    by_ref = {str(r.get("record_ref")): r for r in records if r.get("record_ref")}
    if not by_ref:
        return items

    for it in items:
        rec = by_ref.get(it["meta"].get("record_ref"))
        if not rec:
            continue
        abstract = _clean(rec.get("abstract") or "")
        if abstract:
            it["snippet"] = abstract[:SNIPPET_MAX_CHARS]
            it["meta"]["abstract"] = abstract
        journal = _clean(rec.get("source") or "")
        if journal:
            it["meta"]["journal"] = journal
        authors = rec.get("authors") or []
        if isinstance(authors, list) and authors:
            it["meta"]["authors"] = ", ".join(str(a) for a in authors if a)
        if rec.get("doi"):
            it["meta"]["doi"] = str(rec["doi"])
        if isinstance(rec.get("year"), int) and not it["meta"].get("year"):
            it["meta"]["year"] = rec["year"]
    return items


def _detect_rate_limit(obj: dict) -> bool:
    """判断返回是否属于限流，而不是"真的没结果"。

    工具的 coverage 里会给出明确证据：
        {"failed": true, "error": "站点返回临时空页面，不能判定零结果"}
    这句"不能判定零结果"本身就是工具在提示调用方不要当作空结果。
    """
    result = obj.get("result") or {}
    if result.get("candidate_count"):
        return False
    coverage = result.get("coverage") or []
    for ch in coverage:
        err = str(ch.get("error") or "")
        if "临时空页面" in err or "不能判定" in err:
            return True
    has_rows = bool(result.get("records") or result.get("results"))
    return obj.get("status") == "partial" and not has_rows


async def search_cnki(query: str, max_results: int | None = None) -> CnkiSearchResult:
    """检索知网文献。MCP 优先，失败按情况降级或报错。

    返回项：{"title", "url", "snippet", "meta"}，meta 含 record_ref 供下载全文。
    """
    keyword = _clean(query)
    if not keyword:
        return CnkiSearchResult([], ST_NO_HITS, "空查询")

    limit = max_results or int(getattr(settings, "cnki_max_results", 5) or 5)
    mode = (getattr(settings, "cnki_mode", "auto") or "auto").strip().lower()

    if mode != "fallback":
        if not cnki_mcp.available():
            return CnkiSearchResult([], ST_UNREACHABLE, "未找到 cnki-mcp 可执行文件")

        try:
            obj = await cnki_mcp.call_tool(
                "structured_search",
                {
                    "expression": {"field": "subject", "value": keyword, "match": "phrase"},
                    "sort": "relevance",
                    "limit": max(1, min(limit, 50)),
                },
                kind="search",
            )
        except Exception as exc:
            msg = str(exc)
            if "needs_verification" in msg:
                return CnkiSearchResult(
                    [], ST_UNREACHABLE,
                    "知网要求人工验证（滑块），后台任务无法满足；如需恢复请手动在浏览器完成一次验证",
                )
            return CnkiSearchResult([], ST_UNREACHABLE, f"{type(exc).__name__}: {msg[:120]}")

        status = obj.get("status")
        if status == "failed":
            errors = obj.get("errors") or []
            detail = str(errors[0].get("message") if errors else "")[:160]
            return CnkiSearchResult([], ST_UNREACHABLE, detail or "MCP 检索失败")
        if status not in ("completed", "partial"):
            # 任何非预期状态都按故障处理，不要让它掉进下面的"没有 records → no_hits"
            return CnkiSearchResult([], ST_UNREACHABLE, f"非预期状态: {status}")

        result = obj.get("result") or {}
        # 字段名不一致：structured_search 返回 `results`，get_metadata 返回 `records`。
        # 只认一个会静默解析出 0 条，把"检索成功"误判成"没有结果"。
        records = result.get("records") or result.get("results") or []

        if not records:
            if _detect_rate_limit(obj):
                return CnkiSearchResult([], ST_RATE_LIMITED, "站点返回临时空页面（触发频率限制）")
            return CnkiSearchResult([], ST_NO_HITS)

        items = [m for m in (_map_record(r) for r in records) if m]
        if not items:
            return CnkiSearchResult([], ST_NO_HITS)

        # structured_search 只给 record_ref/title/url，摘要与期刊名要靠 get_metadata 补
        items = await _enrich_metadata(items[:limit])
        return CnkiSearchResult(items, ST_OK)

    # 显式配置为 fallback：直接走 site 限定搜索
    items = await _search_via_site(keyword, limit)
    return CnkiSearchResult(
        items, ST_DEGRADED if items else ST_NO_HITS,
        "" if items else "site:cnki.net 无结果",
    )


async def _search_via_site(query: str, max_results: int) -> list[dict]:
    """降级：DeepSeek Search 限定 site:cnki.net，只拿到公开条目。

    这条路径没有任何机构权限要求，但结果质量明显更差（标题常被截断，
    甚至串到无关页面），所以调用方应把它的初始 relevance 压低。
    """
    from app.integrations.web_search import search_web

    rows = await search_web(f"{query} site:cnki.net", max_results=max_results)
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "title": _clean(r.get("title", "")),
                "url": r.get("url", ""),
                "snippet": _clean(r.get("snippet", ""))[:SNIPPET_MAX_CHARS],
                "meta": {
                    **(r.get("meta") or {}),
                    "record_ref": "",
                    "evidence_level": "title_only",
                    "fulltext_available": "no",
                    "degraded": True,
                },
            }
        )
    return out


def _fulltext_failed(reason: str) -> dict:
    return {
        "state": "failed", "path": None, "reason": reason,
        "sha256": "", "bytes": 0, "format": "", "filename": "",
    }


def _download_error(exc: Exception) -> str:
    """把底层异常翻译成可展示的原因，同时保留原始错误便于排查。"""
    raw = f"{type(exc).__name__}: {str(exc)[:120]}"
    if "needs_verification" in raw:
        return "知网要求人工滑块验证，后台任务无法完成；请先在浏览器完成验证"
    if "operation_timeout" in raw:
        return f"知网下载超时（可能限流或机构权限失效），请稍后重试；原始错误：{raw}"
    if "mcp_timeout" in raw:
        return f"知网 MCP 响应超时，请确认浏览器/进程未被占用；原始错误：{raw}"
    return raw


def _download_failure_reason(obj: dict) -> str:
    """把 MCP 的下载终态错误码翻译成可操作的原因。"""
    errors = obj.get("errors") or []
    codes = {str(e.get("code") or "") for e in errors}
    if "VERIFICATION_SKIPPED" in codes:
        return "该文献需要人工验证（登录/滑块），后台任务无法完成；请先在知网完成登录/验证后重试"
    if "BROWSER_PROFILE_IN_USE" in codes:
        return "知网专用浏览器 profile 被残留进程占用，请关闭旧服务/浏览器后重试"
    if "DATA_DIRECTORY_IN_USE" in codes:
        return "知网 MCP 数据目录被占用，请关闭残留 cnki-mcp 进程后重试"
    if "SIDE_EFFECT_UNCERTAIN" in codes:
        return "知网下载结果不确定（可能仍在进行），请稍后用相同文献重试"
    for e in errors:
        if e.get("message"):
            return str(e["message"])[:160]
    return str(obj.get("status") or "下载失败")[:160]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _archive_pdf(src: Path, target_dir: Path | None, sha: str = "") -> dict:
    """把 PDF 复制到归档目录，用 sha256 前 12 位命名（避开中文文件名并天然去重）。"""
    dest_dir = target_dir or (Path(__file__).resolve().parents[3] / "data" / "papers" / "cn")
    dest_dir.mkdir(parents=True, exist_ok=True)
    sha = sha or _sha256_file(src)
    name = f"cnki_{sha[:12]}.pdf"
    dest = dest_dir / name
    if not dest.exists():
        shutil.copy2(src, dest)
    return {
        "state": "completed",
        "path": str(dest),
        "reason": None,
        "sha256": sha,
        "bytes": dest.stat().st_size,
        "format": "pdf",
        "filename": name,
    }


async def fetch_fulltext(record_ref: str, target_dir: Path | None = None) -> dict:
    """下载指定文献的 PDF 并归档。

    返回 {"state": "completed"|"failed", "path", "reason",
          "sha256", "bytes", "format", "filename"}

    注意：这是独立能力，不在调研主流程里自动调用——单篇下载实测 13–40 秒，
    自动调用会让每次调研都超时，且显著增加触发知网限流的概率。

    已下载过的文献（MCP downloads 目录按 record_ref 命中）直接复用本地文件，
    不再请求知网；既省时间，也在限流时保证已下载文献仍可入库。

    强制 `format="pdf"`：下游解析链路只支持 PDF；知网若只提供 CAJ，
    这里直接返回失败，避免把无法解析的文件写进知识库。
    """
    if not record_ref:
        return _fulltext_failed("缺少 record_ref")

    # 已下载过：直接复用 MCP downloads 里的文件，不再请求知网（避开限流）
    cached = cnki_mcp.find_downloaded_paper(record_ref)
    if cached is not None:
        return _archive_pdf(cached, target_dir)

    if not cnki_mcp.available():
        return _fulltext_failed("未找到 cnki-mcp 可执行文件")

    try:
        # on_verification=skip：后台任务无法满足人工验证，遇到就跳过而不是卡住
        obj = await cnki_mcp.call_tool(
            "download_paper",
            {"record_ref": record_ref, "format": "pdf", "on_verification": "skip"},
            kind="download",
            timeout=cnki_mcp.DOWNLOAD_TIMEOUT,
        )
    except Exception as exc:
        return _fulltext_failed(_download_error(exc))

    result = obj.get("result") or {}
    state = result.get("state") or obj.get("status")
    if state != "completed":
        return _fulltext_failed(_download_failure_reason(obj))

    fmt = str(result.get("format") or "").lower()
    if fmt and fmt != "pdf":
        return _fulltext_failed(f"知网仅返回 {fmt.upper()} 格式，当前解析链路只支持 PDF")

    src = Path(str(result.get("path") or ""))
    if not src.is_file():
        return _fulltext_failed("下载文件不存在")

    out = _archive_pdf(src, target_dir, str(result.get("sha256") or ""))
    out["bytes"] = int(result.get("bytes") or out["bytes"])
    return out


async def cnki_health() -> dict:
    return await cnki_mcp.health()
