"""文档解析器：PDF（含图片 OCR）/ DOCX / MD / TXT → 纯文本。

PDF 智能解析（P1）：
  页级类型分流：文本充足=原生路径（版面还原）；文本稀少=整页 OCR（扫描件）
  版面还原：坐标排序阅读顺序、页眉页脚剔除、大字号标题转 markdown、页内图片 OCR
  每页首注入 [第N页] 标记（溯源 + LLM 引用上下文）
"""
import re
import statistics
from pathlib import Path

from app.core.config import get_settings

settings = get_settings()

SUPPORTED_EXTS = {".pdf": "pdf", ".docx": "docx", ".md": "md", ".txt": "txt"}
SUPPORTED_TYPES = set(SUPPORTED_EXTS.values())


def parse_file(path: str | Path, doc_type: str) -> str:
    path = Path(path)
    if doc_type == "pdf":
        return _parse_pdf(path)
    if doc_type == "docx":
        return _parse_docx(path)
    return _parse_text(path)


# ---------------- PDF ----------------

def _parse_pdf(path: Path) -> str:
    mode = (settings.parser_mode or "auto").strip().lower()
    if mode in ("auto", "grobid"):
        try:
            from app.rag.grobid_parser import parse_pdf_with_grobid

            text = parse_pdf_with_grobid(path)
            if text.strip():
                return text
        except Exception as exc:  # noqa: BLE001
            print(
                f"[kra][grobid] fallback to PyMuPDF: {type(exc).__name__}: {str(exc)[:160]}",
                flush=True,
            )
    return _parse_pdf_pymupdf(path)


def _parse_pdf_pymupdf(path: Path) -> str:
    from app.rag.ocr import ocr_image

    if not settings.pdf_ocr_enabled:
        return _parse_pdf_text_only(path)

    import fitz  # PyMuPDF

    doc = fitz.open(str(path))
    pages: list[str] = []
    try:
        for page in doc:
            page_text = _parse_pdf_page(page, doc, ocr_image)
            if page_text.strip():
                pages.append(f"[第{page.number + 1}页]\n{page_text}")
    finally:
        doc.close()
    return _clean("\n\n".join(pages))


def _parse_pdf_text_only(path: Path) -> str:
    """回退路径：pypdf 纯文本提取（PDF_OCR_ENABLED=false 时使用）。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return _clean("\n\n".join(pages))


def _median_font_size(data: dict) -> float:
    sizes = [
        span["size"]
        for block in data.get("blocks", [])
        if block.get("type") == 0
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    ]
    return statistics.median(sizes) if sizes else 12.0


def _order_by_columns(
    items: list[tuple[float, float, float, str]], page_w: float, page_h: float
) -> list[tuple[float, float, float, str]]:
    """按栏还原阅读顺序。

    原实现按 (y, text) 全局排序：双栏论文同一行的左右栏 y 坐标几乎相同，
    会导致两栏文字逐行交错，切片内容不可读。这里先按 x 分布判定是否双栏：
    - 单栏：维持按 y 排序；
    - 双栏：跨栏元素分置页首/页尾，中间先左栏后右栏，各栏内按 y 排序。
    """
    if not items:
        return items

    mid = page_w / 2
    tol = page_w * 0.08  # 中线容差带，容纳跨栏标题与居中图表

    left = [it for it in items if it[2] <= mid + tol]
    right = [it for it in items if it[1] >= mid - tol]
    full = [it for it in items if it[2] > mid + tol and it[1] < mid - tol]

    two_col = (
        len(left) >= 3
        and len(right) >= 3
        and (len(left) + len(right)) >= len(items) * 0.6
    )
    if not two_col:
        return sorted(items, key=lambda t: (t[0] // 8, t[1]))

    head_cut = page_h * 0.30  # 页首跨栏区（标题/摘要）
    top = sorted([it for it in full if it[0] < head_cut], key=lambda t: t[0])
    bottom = sorted([it for it in full if it[0] >= head_cut], key=lambda t: t[0])

    return (
        top
        + sorted(left, key=lambda t: (t[0] // 8, t[1]))
        + sorted(right, key=lambda t: (t[0] // 8, t[1]))
        + bottom
    )


def _parse_pdf_page(page, doc, ocr_image) -> str:
    """单页：类型分流 + 版面还原 + 图片 OCR（按栏还原阅读顺序）。"""
    raw_text = page.get_text("text") or ""
    if len(raw_text.strip()) < settings.pdf_ocr_min_chars:
        # 扫描页：整页渲染 OCR
        pix = page.get_pixmap(dpi=150)
        return _clean(ocr_image(pix.tobytes("png")))

    data = page.get_text("dict")
    median_size = _median_font_size(data)
    page_h = page.rect.height
    page_w = page.rect.width
    top_cut = page_h * settings.pdf_margin_top
    bottom_cut = page_h * settings.pdf_margin_bottom

    # (y0, x0, x1, 文本)：保留 x 范围，供双栏判定使用
    items: list[tuple[float, float, float, str]] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_box = block.get("bbox", (0, 0, 0, 0))
        block_lines: list[str] = []
        block_y = block_box[1]
        for line in block.get("lines", []):
            bbox = line.get("bbox", (0, 0, 0, 0))
            y0, y1 = bbox[1], bbox[3]
            if y1 < top_cut or y0 > bottom_cut:
                continue  # 页眉页脚剔除
            line_text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not line_text.strip():
                continue
            max_size = max(
                (span.get("size", 0) for span in line.get("spans", [])), default=0
            )
            # 标题检测：字号 ≥ 中位数 1.35 倍且行短 → markdown 标题（衔接 chunker 章节划分）
            if max_size >= median_size * 1.35 and len(line_text.strip()) <= 40:
                block_lines.append(f"# {line_text.strip()}")
            else:
                block_lines.append(line_text)
        if block_lines:
            # 按 block 聚合，保住段落完整性（逐行重排会把段落拆碎）
            items.append(
                (block_y, block_box[0], block_box[2], "\n".join(block_lines))
            )

    # 页内图片：OCR 文字 + VLM 描述（按位置排序插入）
    for img_info in page.get_images(full=True):
        try:
            xref = img_info[0]
            base = doc.extract_image(xref)
            img_bytes = base.get("image")
            if not img_bytes:
                continue
            rects = page.get_image_rects(xref)
            y_pos = rects[0].y0 if rects else page_h / 2
            parts_img: list[str] = []
            ocr_text = ocr_image(img_bytes)
            if ocr_text.strip():
                parts_img.append(f"[图] {ocr_text.strip()}")
            if settings.vlm_enabled:
                from app.rag.vlm import describe_image_or_mark

                desc = describe_image_or_mark(img_bytes)
                if desc.strip():
                    parts_img.append(f"[图描述] {desc.strip()}")
            if parts_img:
                items.append((y_pos, page.rect.x0, page_w, "\n".join(parts_img)))
        except Exception:
            continue

    ordered = _order_by_columns(items, page_w, page_h)
    return _clean("\n\n".join(t[3] for t in ordered))


# ---------------- DOCX / TXT ----------------

def _parse_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    return _clean("\n\n".join(parts))


def _parse_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return _clean(raw.decode(encoding))
        except (UnicodeDecodeError, LookupError):
            continue
    return _clean(raw.decode("utf-8", errors="ignore"))


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    # 合并多余空行、压缩连续空白
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
