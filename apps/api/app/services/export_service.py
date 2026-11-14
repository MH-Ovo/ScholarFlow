"""报告导出：Markdown → DOCX / PDF。

服务端生成而不是浏览器打印，保证两端（Word / PDF）版式一致。
只实现报告实际会用到的 Markdown 子集：标题、段落、列表、引用、代码块、表格、分隔线。
"""
from __future__ import annotations

import io
import re
from xml.sax.saxutils import escape

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*```")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|`[^`]+`|\*[^*\n]+?\*|_[^_\n]+?_)")


def _is_table_row(line: str) -> bool:
    return line.count("|") >= 2


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _iter_blocks(markdown: str):
    """把 Markdown 拆成块级元素；表格需要向后看一行，所以用索引遍历。"""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip():
            i += 1
            continue

        if _FENCE.match(line):
            i += 1
            code: list[str] = []
            while i < len(lines) and not _FENCE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1
            yield ("code", "\n".join(code))
            continue

        match = _HEADING.match(line)
        if match:
            yield ("heading", len(match.group(1)), match.group(2).strip())
            i += 1
            continue

        if _HR.match(line):
            yield ("hr",)
            i += 1
            continue

        # 表格：当前行有 |，下一行是分隔行
        if _is_table_row(line) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            header = _split_row(line)
            rows = [header]
            i += 2
            while i < len(lines) and _is_table_row(lines[i]) and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            yield ("table", rows)
            continue

        match = _QUOTE.match(line)
        if match:
            quote: list[str] = [match.group(1)]
            i += 1
            while i < len(lines):
                nxt = _QUOTE.match(lines[i])
                if not nxt:
                    break
                quote.append(nxt.group(1))
                i += 1
            yield ("quote", " ".join(x.strip() for x in quote if x.strip()))
            continue

        match = _BULLET.match(line)
        if match:
            yield ("bullet", match.group(1).strip())
            i += 1
            continue

        match = _NUMBER.match(line)
        if match:
            yield ("number", match.group(2).strip())
            i += 1
            continue

        # 普通段落：连续非空行合并
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip():
            nxt = lines[i].strip()
            if (
                _HEADING.match(nxt)
                or _BULLET.match(nxt)
                or _NUMBER.match(nxt)
                or _QUOTE.match(nxt)
                or _HR.match(nxt)
                or _FENCE.match(nxt)
                or (_is_table_row(nxt) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]))
            ):
                break
            para.append(nxt)
            i += 1
        yield ("para", " ".join(para))


def _inline_runs(text: str):
    """拆分行内格式，产出 (文本, 粗体, 斜体, 代码)。"""
    for part in _INLINE.split(text):
        if not part:
            continue
        if len(part) > 4 and part.startswith("**") and part.endswith("**"):
            yield (part[2:-2], True, False, False)
        elif len(part) > 4 and part.startswith("__") and part.endswith("__"):
            yield (part[2:-2], True, False, False)
        elif len(part) > 2 and part.startswith("`") and part.endswith("`"):
            yield (part[1:-1], False, False, True)
        elif len(part) > 2 and part.startswith("*") and part.endswith("*"):
            yield (part[1:-1], False, True, False)
        elif len(part) > 2 and part.startswith("_") and part.endswith("_"):
            yield (part[1:-1], False, True, False)
        else:
            yield (part, False, False, False)


# ---------------------------------------------------------------- DOCX

def _docx_font(run, name: str) -> None:
    from docx.oxml.ns import qn

    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        from docx.oxml import OxmlElement

        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)


def _docx_add_runs(paragraph, text: str, size: float | None = None, base_bold: bool = False) -> None:
    from docx.shared import Pt

    for chunk, bold, italic, code in _inline_runs(text):
        run = paragraph.add_run(chunk)
        run.bold = bold or base_bold
        run.italic = italic
        if code:
            _docx_font(run, "Consolas")
        if size:
            run.font.size = Pt(size)


def markdown_to_docx(markdown: str, title: str = "") -> bytes:
    from docx import Document as DocxDocument
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = DocxDocument()

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(4)
    from docx.oxml.ns import qn

    normal_rpr = normal.element.get_or_add_rPr()
    normal_fonts = normal_rpr.get_or_add_rFonts()
    normal_fonts.set(qn("w:eastAsia"), "宋体")

    if title:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(14)
        run = p.add_run(title)
        run.bold = True
        run.font.size = Pt(18)
        run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
        _docx_font(run, "黑体")

    heading_sizes = {1: 16, 2: 14, 3: 12, 4: 11, 5: 10.5, 6: 10.5}
    first_block = True
    for block in _iter_blocks(markdown):
        kind = block[0]
        is_first = first_block
        first_block = False
        if kind == "heading":
            level, text = block[1], block[2]
            if is_first and title and text.strip() == title.strip():
                continue
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(10 if level <= 2 else 6)
            p.paragraph_format.space_after = Pt(4)
            _docx_add_runs(p, text, size=heading_sizes.get(level, 10.5), base_bold=True)
            for run in p.runs:
                _docx_font(run, "黑体")
                run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
        elif kind == "para":
            p = doc.add_paragraph()
            _docx_add_runs(p, block[1])
        elif kind == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            _docx_add_runs(p, block[1])
        elif kind == "number":
            p = doc.add_paragraph(style="List Number")
            _docx_add_runs(p, block[1])
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(6)
            _docx_add_runs(p, block[1])
            for run in p.runs:
                run.italic = True
                run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        elif kind == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(12)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(8)
            lines = block[1].split("\n") or [""]
            for idx, line_text in enumerate(lines):
                run = p.add_run(line_text)
                _docx_font(run, "Consolas")
                run.font.size = Pt(9)
                if idx < len(lines) - 1:
                    run.add_break()
        elif kind == "table":
            rows: list[list[str]] = block[1]
            cols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=cols)
            table.style = "Table Grid"
            for r_idx, row in enumerate(rows):
                for c_idx in range(cols):
                    cell = table.cell(r_idx, c_idx)
                    cell.text = ""
                    p = cell.paragraphs[0]
                    p.paragraph_format.line_spacing = 1.15
                    text = row[c_idx] if c_idx < len(row) else ""
                    _docx_add_runs(p, text, size=9, base_bold=(r_idx == 0))
        elif kind == "hr":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(4)
            run = p.add_run("─" * 40)
            run.font.color.rgb = RGBColor(0xCC, 0xCC, 0xCC)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------- PDF

_PDF_FONT = "STSong-Light"
_PDF_FONT_READY = False


def _ensure_pdf_font() -> None:
    global _PDF_FONT_READY
    if _PDF_FONT_READY:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont(_PDF_FONT))
    pdfmetrics.registerFontFamily(
        _PDF_FONT, normal=_PDF_FONT, bold=_PDF_FONT, italic=_PDF_FONT, boldItalic=_PDF_FONT
    )
    _PDF_FONT_READY = True


def _pdf_inline(text: str) -> str:
    value = escape(text)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"__(.+?)__", r"<b>\1</b>", value)
    value = re.sub(
        r"`([\x20-\x7e]+?)`", r'<font face="Courier">\1</font>', value
    )
    value = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", value)
    value = re.sub(r"(?<!_)_([^_]+?)_(?!_)", r"<i>\1</i>", value)
    return value


def markdown_to_pdf(markdown: str, title: str = "") -> bytes:
    _ensure_pdf_font()

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buffer = io.BytesIO()
    margin_x, margin_y = 18 * mm, 18 * mm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=margin_y,
        bottomMargin=margin_y,
        title=title or "ScholarFlow 研究报告",
        author="ScholarFlow",
    )
    width = A4[0] - 2 * margin_x

    body = ParagraphStyle(
        "Body", fontName=_PDF_FONT, fontSize=10.5, leading=17,
        wordWrap="CJK", spaceAfter=5, textColor=colors.HexColor("#1F2328"),
    )
    title_style = ParagraphStyle(
        "Title", parent=body, fontSize=19, leading=28, alignment=TA_CENTER,
        spaceAfter=14, textColor=colors.HexColor("#111111"),
    )
    heading = {
        level: ParagraphStyle(
            f"H{level}", parent=body, fontSize=size, leading=size + 7,
            spaceBefore=10 if level <= 2 else 6, spaceAfter=4,
            textColor=colors.HexColor("#111111"),
        )
        for level, size in {1: 15.5, 2: 13.5, 3: 12, 4: 11, 5: 10.5, 6: 10.5}.items()
    }
    bullet_style = ParagraphStyle("Bullet", parent=body, leftIndent=14, bulletIndent=3)
    quote_style = ParagraphStyle(
        "Quote", parent=body, leftIndent=14, textColor=colors.HexColor("#555555"),
        spaceBefore=3, spaceAfter=6,
    )
    code_style = ParagraphStyle(
        "Code", parent=body, fontName=_PDF_FONT, fontSize=8.8, leading=13,
        leftIndent=10, backColor=colors.HexColor("#F5F6F8"),
        borderPadding=6, spaceBefore=4, spaceAfter=8,
    )
    cell_style = ParagraphStyle(
        "Cell", parent=body, fontSize=8.8, leading=13, spaceAfter=0,
    )

    story: list = []
    if title:
        story.append(Paragraph(_pdf_inline(title), title_style))

    first_block = True
    for block in _iter_blocks(markdown):
        kind = block[0]
        is_first = first_block
        first_block = False
        if kind == "heading":
            level, text = block[1], block[2]
            if is_first and title and text.strip() == title.strip():
                continue
            story.append(Paragraph(_pdf_inline(text), heading.get(level, heading[4])))
        elif kind == "para":
            story.append(Paragraph(_pdf_inline(block[1]), body))
        elif kind == "bullet":
            story.append(Paragraph(_pdf_inline(block[1]), bullet_style, bulletText="•"))
        elif kind == "number":
            story.append(Paragraph(_pdf_inline(block[1]), bullet_style, bulletText="›"))
        elif kind == "quote":
            story.append(Paragraph(_pdf_inline(block[1]), quote_style))
        elif kind == "code":
            lines = block[1].split("\n") or [""]
            content = "<br/>".join(_pdf_inline(x) or "&nbsp;" for x in lines)
            story.append(Paragraph(content, code_style))
        elif kind == "table":
            rows: list[list[str]] = block[1]
            cols = max(len(r) for r in rows)
            data = [
                [
                    Paragraph(
                        (
                            f"<b>{_pdf_inline(row[c] if c < len(row) else '')}</b>"
                            if r == 0
                            else _pdf_inline(row[c] if c < len(row) else "")
                        ),
                        ParagraphStyle(
                            f"Cell{r}", parent=cell_style,
                            textColor=colors.HexColor("#111111"),
                        ),
                    )
                    for c in range(cols)
                ]
                for r, row in enumerate(rows)
            ]
            table = Table(data, colWidths=[width / cols] * cols, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 6))
        elif kind == "hr":
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD")))
            story.append(Spacer(1, 6))

    def _decorate(canvas, doc_obj) -> None:
        canvas.saveState()
        canvas.setFont(_PDF_FONT, 8.5)
        canvas.setFillColor(colors.HexColor("#8A8F98"))
        canvas.drawCentredString(A4[0] / 2, 10 * mm, f"ScholarFlow · 第 {doc_obj.page} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return buffer.getvalue()
