"""GROBID TEI → 章节/段落文本（带页码标记）。

GROBID 负责章节结构（<div>/<head>/<p>），页码用 PyMuPDF 的逐页文本做回填：
GROBID 的 TEI 默认不一定输出 <pb>，而 chunk—页码映射又必须有页码，所以
对每个段落取“与它 bigram 重合最高的 PDF 页”作为页码。

输出格式与现有 chunker 兼容：
    [第1页]
    # I. INTRODUCTION
    段落文本...
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from app.core.config import get_settings

settings = get_settings()
NS = {"t": "http://www.tei-c.org/ns/1.0"}
_WS = re.compile(r"\s+")
_PAGE_MARK = re.compile(r"\[第(\d+)页\]")


class GrobidParseError(RuntimeError):
    pass


def _norm(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def _bigrams(text: str) -> set[str]:
    text = _norm(text)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _page_texts(pdf_path: Path) -> list[str]:
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    try:
        return [_norm(page.get_text("text") or "") for page in doc]
    finally:
        doc.close()


def _best_page(text: str, pages: list[str], previous: int) -> int:
    text = _norm(text)
    if len(text) < 20 or not pages:
        return previous
    probe = text[:500]
    grams = _bigrams(probe)
    if not grams:
        return previous
    best_page, best_score = previous, 0.0
    for i, page in enumerate(pages):
        if not page:
            continue
        score = len(grams & _bigrams(page[:4000])) / len(grams)
        if score > best_score:
            best_page, best_score = i, score
    # 低置信度时沿用上一页，避免标题/公式被分到无关页
    return best_page if best_score >= 0.12 else previous


def _head_text(div: ET.Element) -> str:
    head = div.find("t:head", NS)
    return _norm("".join(head.itertext())) if head is not None else ""


def _direct_paragraphs(div: ET.Element) -> list[str]:
    paras = []
    for p in div.findall("t:p", NS):
        text = _norm("".join(p.itertext()))
        if len(text) >= 10:
            paras.append(text)
    return paras


def _walk_sections(
    div: ET.Element, depth: int, pages: list[str], state: dict
) -> list[tuple[int, str, list[tuple[int, str]]]]:
    """返回 [(depth, heading, [(page_idx, paragraph), ...]), ...]。"""
    out: list[tuple[int, str, list[tuple[int, str]]]] = []
    heading = _head_text(div)
    paras: list[tuple[int, str]] = []
    for text in _direct_paragraphs(div):
        page = _best_page(text, pages, state["page"])
        state["page"] = page
        paras.append((page, text))
    if heading or paras:
        out.append((depth, heading, paras))
    for child in div.findall("t:div", NS):
        out.extend(_walk_sections(child, depth + 1, pages, state))
    return out


def parse_pdf_with_grobid(path: str | Path) -> str:
    path = Path(path)
    url = settings.grobid_url.rstrip("/") + "/api/processFulltextDocument"
    try:
        with path.open("rb") as fh:
            resp = httpx.post(
                url,
                files={"input": (path.name, fh, "application/pdf")},
                data={"consolidateHeader": "1", "consolidateCitations": "0"},
                timeout=settings.grobid_timeout,
            )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise GrobidParseError(f"GROBID request failed: {type(exc).__name__}: {exc}") from exc

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise GrobidParseError(f"GROBID returned invalid TEI: {exc}") from exc

    body = root.find("t:text/t:body", NS)
    if body is None:
        raise GrobidParseError("GROBID TEI has no body")

    pages = _page_texts(path)
    sections: list[tuple[int, str, list[tuple[int, str]]]] = []
    state = {"page": 0}
    for div in body.findall("t:div", NS):
        sections.extend(_walk_sections(div, 1, pages, state))
    if not sections:
        raise GrobidParseError("GROBID TEI has no sections")

    lines: list[str] = []
    last_page = -1
    paragraph_count = 0
    for depth, heading, paras in sections:
        section_page = paras[0][0] if paras else last_page
        if heading:
            if section_page != last_page:
                lines.append(f"[第{section_page + 1}页]")
                last_page = section_page
            lines.append(f"{'#' * min(depth, 4)} {heading}")
        for page, text in paras:
            if page != last_page:
                lines.append(f"[第{page + 1}页]")
                last_page = page
            lines.append(text)
            paragraph_count += 1

    text = "\n\n".join(lines).strip()
    raw_chars = sum(len(p) for p in pages)
    # 中文/扫描 PDF 上 GROBID 有时只抽到表格/局部页；正文明显缺失时回退 PyMuPDF
    min_chars = max(500, int(raw_chars * 0.20))
    if paragraph_count < 3 or len(text) < min_chars:
        raise GrobidParseError(
            f"GROBID structure too thin: paragraphs={paragraph_count}, "
            f"chars={len(text)}, raw_chars={raw_chars}"
        )
    return text


def grobid_alive() -> bool:
    try:
        r = httpx.get(
            settings.grobid_url.rstrip("/") + "/api/isalive",
            timeout=5.0,
        )
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False
