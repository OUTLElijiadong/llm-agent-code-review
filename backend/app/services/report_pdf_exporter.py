"""PDF 报告导出服务 (T12)

基于 reportlab.platypus 构建 PDF 文档,支持中文字体(STSong-Light CID)
与严重度颜色编码(严重:红 / 高:橙 / 中:黄 / 低:蓝)。

依赖:
- T11 report_exporter.export_to_dict: 构建报告上下文(task_info/issues/statistics)
- reportlab >= 4.0: PDF 文档生成

文档结构:
    1. 报告头(任务名 / 审查时间 / 综合评分)
    2. 统计摘要表(问题总数 / 严重度分布 / 已修复数)
    3. 问题列表(按严重度分组:严重 > 高 > 中 > 低)
"""
from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.ai.scoring import score_risk_level
from app.services.report_exporter import (
    conflict_status_label,
    cvss_source_label,
    decompilation_status_label,
    discovery_source_label,
    evidence_quality_label,
    export_to_dict,
    finding_status_label,
    human_review_status_label,
    report_status_label,
)
from app.services.report_markdown import markdown_to_blocks, parse_markdown_blocks, reportlab_markup

# ============ 模块常量 ============

# 中文字体名称(ReportLab 内置 CID 字体,无需额外字体文件)
_CHINESE_FONT_NAME: str = "STSong-Light"
_LATIN_FONT_NAME: str = "Vera"

# 严重度 → 颜色映射(ReportLab colors 对象)
_SEVERITY_COLORS: Dict[str, colors.Color] = {
    "严重": colors.HexColor("#DC2626"),  # 红色
    "高": colors.HexColor("#EA580C"),    # 橙色
    "中": colors.HexColor("#CA8A04"),    # 黄色
    "低": colors.HexColor("#2563EB"),    # 蓝色
}

# 严重度展示顺序(严重 > 高 > 中 > 低 > 其他)
_SEVERITY_ORDER: Tuple[str, ...] = ("严重", "高", "中", "低")

# 字体是否已注册的标志(避免重复注册)
_font_registered: bool = False


def _code_markup(text: str) -> str:
    """代码中的中文显式使用 CID 字体，西文保留 Courier 等宽布局。"""
    return re.sub(
        r"[^\x00-\x7f]+",
        lambda match: f'<font name="{_CHINESE_FONT_NAME}">{match.group(0)}</font>',
        text,
    )


# ============ 内部辅助 ============

def _ensure_chinese_font() -> str:
    """注册并返回 PDF 中文字体名称。

    使用 ReportLab 内置的 STSong-Light CID 字体,无需依赖系统字体文件,
    在 Mac / Linux / Windows 上均能正常渲染中文(无方块乱码)。
    注册操作幂等,多次调用不会重复注册。

    Returns:
        str: 可传给 ReportLab fontName 参数的字体名称("STSong-Light")。
    """
    global _font_registered
    if _font_registered:
        return _CHINESE_FONT_NAME
    try:
        pdfmetrics.getFont(_CHINESE_FONT_NAME)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_CHINESE_FONT_NAME))
    vera_font_dir = Path(reportlab.__file__).parent / "fonts"
    for font_name, filename in (
        (_LATIN_FONT_NAME, "Vera.ttf"),
        ("Vera-Bold", "VeraBd.ttf"),
        ("Vera-Italic", "VeraIt.ttf"),
        ("Vera-BoldItalic", "VeraBI.ttf"),
    ):
        try:
            pdfmetrics.getFont(font_name)
        except KeyError:
            pdfmetrics.registerFont(TTFont(font_name, str(vera_font_dir / filename)))
    pdfmetrics.registerFontFamily(
        _LATIN_FONT_NAME,
        normal=_LATIN_FONT_NAME,
        bold="Vera-Bold",
        italic="Vera-Italic",
        boldItalic="Vera-BoldItalic",
    )
    _font_registered = True
    return _CHINESE_FONT_NAME


def _with_cjk_fallback(markup: str) -> str:
    """Keep Latin metrics on a standard font and use the CID font only for CJK text."""
    pieces: List[str] = []
    active_fonts: List[str] = []
    token_pattern = re.compile(r"<[^>]+>|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);|[^<&]+|[<&]")
    for match in token_pattern.finditer(markup):
        token = match.group(0)
        if token.startswith("<"):
            if token.startswith("</font"):
                if active_fonts:
                    active_fonts.pop()
            elif token.startswith("<font"):
                font_match = re.search(r'name=[\"\']([^\"\']+)', token)
                active_fonts.append(font_match.group(1) if font_match else "")
            pieces.append(token)
            continue
        if token.startswith("&") or token in {"<", "&"}:
            pieces.append(token)
            continue
        current_font = active_fonts[-1] if active_fonts else ""
        if current_font == _CHINESE_FONT_NAME:
            pieces.append(token)
            continue
        chunks = re.split(r"([^\x00-\x7f]+)", token)
        pieces.extend(
            f'<font name="{_CHINESE_FONT_NAME}">{chunk}</font>'
            if chunk and not chunk.isascii() else chunk
            for chunk in chunks
        )
    return "".join(pieces)


def _paragraph(markup: str, style: ParagraphStyle, **kwargs: Any) -> Paragraph:
    """Create a Paragraph with explicit Latin/CJK font fallback."""
    return Paragraph(_with_cjk_fallback(markup), style, **kwargs)


def _build_styles(font_name: str) -> Dict[str, ParagraphStyle]:
    """构建 PDF 文档使用的段落样式集合。

    Args:
        font_name: 已注册的中文字体名称。

    Returns:
        Dict[str, ParagraphStyle]: 样式名称 → ParagraphStyle 的映射,
            包含 title / heading / normal / code / severity_* 五种样式。
    """
    base = getSampleStyleSheet()
    styles: Dict[str, ParagraphStyle] = {
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Title"],
            fontName=_LATIN_FONT_NAME, fontSize=20, leading=28, spaceAfter=14,
            alignment=1,  # 居中
        ),
        "heading": ParagraphStyle(
            "ReportHeading", parent=base["Heading1"],
            fontName=_LATIN_FONT_NAME, fontSize=15, leading=22, spaceBefore=14, spaceAfter=8,
            textColor=colors.HexColor("#111827"),
        ),
        "subheading": ParagraphStyle(
            "ReportSubHeading", parent=base["Heading2"],
            fontName=_LATIN_FONT_NAME, fontSize=12, leading=18, spaceBefore=10, spaceAfter=6,
            textColor=colors.HexColor("#1F2937"),
        ),
        "normal": ParagraphStyle(
            "ReportNormal", parent=base["Normal"],
            fontName=_LATIN_FONT_NAME, fontSize=10, leading=16,
            textColor=colors.HexColor("#374151"),
        ),
        "table_header": ParagraphStyle(
            "ReportTableHeader", parent=base["Normal"],
            fontName=_LATIN_FONT_NAME, fontSize=10, leading=16,
            textColor=colors.whitesmoke,
        ),
        "table_cell": ParagraphStyle(
            "ReportTableCell", parent=base["Normal"],
            fontName=_LATIN_FONT_NAME, fontSize=9, leading=13,
            textColor=colors.HexColor("#374151"),
        ),
        "code": ParagraphStyle(
            "ReportCode", parent=base["Code"],
            fontName="Courier", fontSize=9, leading=13,
            textColor=colors.HexColor("#059669"),
            backColor=colors.HexColor("#F0FDF4"),
            borderPadding=4, leftIndent=8, rightIndent=8,
        ),
        "quote": ParagraphStyle(
            "ReportQuote", parent=base["Normal"],
            fontName=_LATIN_FONT_NAME, fontSize=10, leading=16,
            textColor=colors.HexColor("#4B5563"),
            leftIndent=12, borderColor=colors.HexColor("#CBD5E1"),
            borderWidth=1, borderPadding=6, spaceBefore=3, spaceAfter=6,
        ),
    }
    return styles


def _summary_inline_markup(runs: List[Dict[str, Any]]) -> str:
    """Render the supported inline Markdown subset using ReportLab-safe tags."""
    parts: List[str] = []
    for run in runs:
        text = escape(str(run.get("text") or "")).replace("\n", "<br/>")
        if run.get("code"):
            text = f'<font name="Courier">{text}</font>'
        if run.get("italic"):
            text = f"<i>{text}</i>"
        if run.get("bold"):
            text = f"<b>{text}</b>"
        href = run.get("href")
        if href:
            safe_href = escape(str(href), {"\"": "&quot;"})
            text = f'<link href="{safe_href}" color="#2563EB">{text}</link>'
        parts.append(text)
    return "".join(parts)


def _build_summary_markdown_elements(markdown: str, styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """Convert model Markdown into readable PDF flowables without leaking markers."""
    elements: List[Any] = []
    for block in parse_markdown_blocks(markdown):
        kind = block["type"]
        if kind == "heading":
            level = int(block.get("level") or 2)
            style = styles["heading"] if level <= 2 else styles["subheading"]
            elements.append(_paragraph(_summary_inline_markup(block["runs"]), style))
        elif kind == "paragraph":
            elements.append(_paragraph(_summary_inline_markup(block["runs"]), styles["normal"]))
        elif kind in {"unordered_list", "ordered_list"}:
            for index, item in enumerate(block["items"], start=1):
                bullet = "•" if kind == "unordered_list" else f"{index}."
                elements.append(_paragraph(
                    _summary_inline_markup(item), styles["normal"], bulletText=bullet,
                ))
        elif kind == "table":
            rows = block.get("rows") or []
            if rows:
                data = [
                    [
                        _paragraph(
                            _summary_inline_markup(cell),
                            styles["table_header"] if row_index == 0 else styles["normal"],
                        )
                        for cell in row
                    ]
                    for row_index, row in enumerate(rows)
                ]
                table = Table(data, repeatRows=1, hAlign="LEFT")
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                elements.append(table)
                elements.append(Spacer(1, 3 * mm))
        elif kind == "quote":
            elements.append(_paragraph(_summary_inline_markup(block["runs"]), styles["quote"]))
        elif kind == "code":
            code_text = escape(str(block.get("text") or "")).replace("\n", "<br/>")
            elements.append(_paragraph(code_text or " ", styles["code"]))
        elif kind == "rule":
            elements.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#CBD5E1")))
    return elements


def _severity_color(severity: Optional[str]) -> colors.Color:
    """获取严重度对应的颜色对象。

    Args:
        severity: 严重度字符串(严重/高/中/低)。

    Returns:
        colors.Color: 对应的 ReportLab 颜色对象;未知严重度返回灰色。
    """
    if severity and severity in _SEVERITY_COLORS:
        return _SEVERITY_COLORS[severity]
    return colors.HexColor("#6B7280")  # 灰色(未知严重度)


def _group_issues_by_severity(issues: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按严重度分组 issue(严重 > 高 > 中 > 低 > 其他)。

    Args:
        issues: 已归一化的 issue 字典列表。

    Returns:
        Dict[str, List[Dict[str, Any]]]: 键为严重度,值为该严重度下的 issue 列表。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {key: [] for key in _SEVERITY_ORDER}
    grouped["其他"] = []
    for issue in issues or []:
        severity = issue.get("severity")
        if severity in grouped:
            grouped[severity].append(issue)
        else:
            grouped["其他"].append(issue)
    return grouped


def _format_score(score: Optional[int]) -> str:
    """格式化评分展示文本(含风险等级)。

    Args:
        score: 综合评分 0-100。

    Returns:
        str: 格式化后的评分文本,如 "72/100 (中风险)"。
    """
    if score is None:
        return "未评估"
    return f"{score}/100 ({score_risk_level(score)})"


def _append_markdown_elements(
    elements: List[Any],
    value: str | None,
    styles: Dict[str, ParagraphStyle],
) -> None:
    """将 Markdown 内容转换为 PDF 原生段落、列表和代码块。"""
    blocks = markdown_to_blocks(value)
    keep_final_quote = (
        len(blocks) >= 2
        and blocks[-1].kind == "quote"
        and blocks[-2].kind in {"paragraph", "bullet", "ordered"}
    )
    initial_element_count = len(elements)
    for block in blocks:
        if block.kind == "rule":
            elements.append(Spacer(1, 3 * mm))
            continue
        if block.kind == "table":
            if not block.rows:
                continue
            data = []
            for row_index, row in enumerate(block.rows):
                cells = []
                for cell in row:
                    markup = reportlab_markup(cell, code_non_ascii_font=_CHINESE_FONT_NAME)
                    if row_index == 0:
                        markup = f"<b>{markup}</b>"
                    style = styles["table_header"] if row_index == 0 else styles["table_cell"]
                    cells.append(_paragraph(markup or " ", style))
                data.append(cells)
            available_width = A4[0] - 36 * mm
            table = Table(
                data,
                colWidths=[available_width / len(data[0])] * len(data[0]),
                repeatRows=1,
                hAlign="LEFT",
                splitByRow=1,
            )
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            elements.append(table)
            elements.append(Spacer(1, 3 * mm))
            continue
        if block.kind == "heading":
            style = styles["heading"] if block.level <= 2 else styles["subheading"]
            elements.append(_paragraph(reportlab_markup(block.segments), style))
            continue
        if block.kind == "code":
            code = block.segments[0].text if block.segments else ""
            code_markup = escape(code.expandtabs(4)).replace(" ", "&#160;").replace("\n", "<br/>")
            elements.append(_paragraph(_code_markup(code_markup), styles["code"]))
            continue
        markup = reportlab_markup(
            block.segments,
            code_non_ascii_font=_CHINESE_FONT_NAME,
        )
        if block.kind == "bullet":
            markup = "• " + markup
        elif block.kind == "ordered":
            markup = f"{block.number or 1}. " + markup
        if block.kind == "quote":
            depth = max(1, block.list_depth)
            quote_style = ParagraphStyle(
                f"ReportQuote{depth}", parent=styles["quote"], leftIndent=12 * depth,
            )
            elements.append(_paragraph(markup, quote_style))
        else:
            elements.append(_paragraph(markup, styles["normal"]))

    if keep_final_quote and len(elements) >= initial_element_count + 2:
        previous, caveat = elements[-2:]
        if isinstance(previous, Paragraph) and isinstance(caveat, Paragraph):
            elements[-2:] = [KeepTogether([previous, caveat])]


# ============ PDF 文档构建 ============

def _build_header_elements(
    task_info: Dict[str, Any],
    score: int,
    styles: Dict[str, ParagraphStyle],
) -> List[Any]:
    """构建报告头部元素(标题 + 任务元信息)。

    Args:
        task_info: 任务信息字典(含 task_name / project_name / create_time 等)。
        score: 综合评分。
        styles: 段落样式集合。

    Returns:
        List[Any]: PDF 元素列表(Paragraph / Spacer)。
    """
    elements: List[Any] = []
    elements.append(_paragraph("代码审查报告", styles["title"]))
    elements.append(Spacer(1, 6 * mm))

    task_name = escape(str(task_info.get("task_name") or "未命名任务"))
    project_name = escape(str(task_info.get("project_name") or "-"))
    review_type = escape(str(task_info.get("review_type") or "-"))
    create_time = escape(str(task_info.get("create_time") or "-"))
    model_name = escape(str(task_info.get("model_name") or "-"))

    elements.append(_paragraph(f"任务名称:{task_name}", styles["normal"]))
    elements.append(_paragraph(f"所属项目:{project_name}", styles["normal"]))
    elements.append(_paragraph(f"审查类型:{review_type}", styles["normal"]))
    elements.append(_paragraph(f"审查模型:{model_name}", styles["normal"]))
    elements.append(_paragraph(f"生成时间:{create_time}", styles["normal"]))
    elements.append(_paragraph(f"综合评分:<b>{_format_score(score)}</b>", styles["normal"]))
    elements.append(_paragraph(f"执行状态:{escape(report_status_label(task_info.get('status')))}", styles["normal"]))
    if task_info.get("error_message"):
        elements.append(_paragraph(f"错误:{escape(str(task_info['error_message']))}", styles["normal"]))
    elements.append(Spacer(1, 4 * mm))
    return elements


def _build_summary_table(
    statistics: Dict[str, Any],
    font_name: str,
) -> Table:
    """构建统计摘要表格。

    Args:
        statistics: 统计信息字典(含 total_issues / severity_count / fixed_count)。
        font_name: 中文字体名称。

    Returns:
        Table: ReportLab 表格对象,包含问题总数 / 各严重度数量 / 已修复数。
    """
    severity_count = statistics.get("severity_count", {})
    raw_rows = [
        ["指标", "数值"],
        ["问题总数", str(statistics.get("total_issues", 0))],
        ["严重问题", str(severity_count.get("严重", 0))],
        ["高优先级问题", str(severity_count.get("高", 0))],
        ["中优先级问题", str(severity_count.get("中", 0))],
        ["低优先级问题", str(severity_count.get("低", 0))],
        ["已修复问题", str(statistics.get("fixed_count", 0))],
    ]
    cell_style = _build_styles(font_name)
    table_data = [
        [
            _paragraph(escape(value), cell_style["table_header" if row_index == 0 else "table_cell"])
            for value in row
        ]
        for row_index, row in enumerate(raw_rows)
    ]
    table = Table(table_data, colWidths=[120, 80])
    table.setStyle(TableStyle([
        # 表头样式
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), _LATIN_FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, 0), 11),
        # 数据行样式
        ("FONTNAME", (0, 1), (-1, -1), _LATIN_FONT_NAME),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        # 严重行高亮(红色背景)
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#FEE2E2")),
        # 高行高亮(橙色背景)
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#FED7AA")),
    ]))
    return table


def _build_score_breakdown_elements(
    statistics: Dict[str, Any],
    styles: Dict[str, ParagraphStyle],
    font_name: str,
) -> List[Any]:
    """构建可人工核对的统一评分明细。"""
    breakdown = statistics.get("score_breakdown") or {}
    if breakdown.get("score") is None:
        rows = [
            ("评分明细", "未评估", True),
            ("评分来源", breakdown.get("score_source") or "未提供", True),
        ]
        table_data: List[List[Any]] = [[
            _paragraph("字段", styles["table_header"]),
            _paragraph("值", styles["table_header"]),
        ]]
        for label, value, _code_value in rows:
            table_data.append([
                _paragraph(escape(str(label)), styles["table_cell"]),
                _paragraph(escape(str(value)), styles["table_cell"]),
            ])
        table = Table(table_data, colWidths=[45 * mm, 120 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), _LATIN_FONT_NAME),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return [_paragraph("评分明细", styles["heading"]), table, Spacer(1, 4 * mm)]
    weights = breakdown.get("weights") or {}
    counts = breakdown.get("counts") or {}
    deductions = breakdown.get("deductions") or {}
    rows = [
        ("评分版本", breakdown.get("version") or "-", True),
        ("基础分", breakdown.get("base_score", 100), False),
        (
            "扣分权重",
            "严重 × {0}；高 × {1}；中 × {2}；低 × {3}".format(
                weights.get("严重", 15),
                weights.get("高", 8),
                weights.get("中", 3),
                weights.get("低", 1),
            ),
            False,
        ),
        (
            "问题计数",
            "严重 {0}；高 {1}；中 {2}；低 {3}".format(
                counts.get("严重", 0),
                counts.get("高", 0),
                counts.get("中", 0),
                counts.get("低", 0),
            ),
            False,
        ),
        (
            "分级扣分",
            "严重 {0}；高 {1}；中 {2}；低 {3}".format(
                deductions.get("严重", 0),
                deductions.get("高", 0),
                deductions.get("中", 0),
                deductions.get("低", 0),
            ),
            False,
        ),
        ("总扣分", breakdown.get("total_deduction", 0), False),
        ("原始分", breakdown.get("raw_score", 100), False),
        ("最终分", breakdown.get("score", 100), False),
        ("风险等级", breakdown.get("risk_level") or "-", False),
        ("评分来源", breakdown.get("score_source") or "-", True),
    ]
    override = breakdown.get("compatibility_override")
    if isinstance(override, dict):
        rows.append((
            "兼容说明",
            f"{override.get('reason') or '-'}; canonical={override.get('canonical_score')}",
            True,
        ))

    table_data: List[List[Any]] = [[
        _paragraph("字段", styles["table_header"]),
        _paragraph("值", styles["table_header"]),
    ]]
    for label, value, code_value in rows:
        table_data.append([
            _paragraph(escape(str(label)), styles["normal"]),
            _paragraph(
                _code_markup(escape(str(value)).replace("\n", "<br/>")) or "-",
                styles["code"] if code_value else styles["normal"],
            ),
        ])
    table = Table(table_data, colWidths=[38 * mm, 132 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), _LATIN_FONT_NAME),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [
        _paragraph("评分明细", styles["heading"]),
        table,
        Spacer(1, 6 * mm),
    ]


def _build_decompilation_evidence_elements(
    evidence: Dict[str, Any],
    styles: Dict[str, ParagraphStyle],
    font_name: str,
) -> List[Any]:
    """构建可核对的反编译证据章节。"""
    decompilation = evidence.get("decompilation") if isinstance(evidence, dict) else None
    if not isinstance(decompilation, dict):
        return []

    artifact_refs = decompilation.get("artifact_refs")
    if isinstance(artifact_refs, list):
        artifacts = ", ".join(str(item) for item in artifact_refs)
    else:
        artifacts = str(artifact_refs or "-")
    raw_input_hashes = decompilation.get("input_artifact_sha256s")
    if isinstance(raw_input_hashes, list):
        input_artifact_sha256s = ", ".join(str(item) for item in raw_input_hashes)
    else:
        input_artifact_sha256s = str(raw_input_hashes or "-")
    rows = [
        ("状态", decompilation_status_label(decompilation.get("status"))),
        ("工具", f"{decompilation.get('tool') or '-'} {decompilation.get('tool_version') or ''}".strip()),
        ("输入清单 SHA-256", decompilation.get("input_sha256") or "-"),
        ("原始制品 SHA-256", input_artifact_sha256s),
        ("输出 SHA-256", decompilation.get("output_sha256") or "-"),
        ("输出文件数", decompilation.get("output_file_count", 0)),
        ("输出字节数", decompilation.get("output_size_bytes", 0)),
        ("退出码", decompilation.get("exit_code", "-")),
        ("日志", decompilation.get("log_ref") or "-"),
        ("制品", artifacts),
    ]
    table_data: List[List[Any]] = [[
        _paragraph("字段", styles["table_header"]),
        _paragraph("值", styles["table_header"]),
    ]]
    for label, value in rows:
        table_data.append([
            _paragraph(escape(str(label)), styles["normal"]),
            _paragraph(_code_markup(escape(str(value))), styles["code"]),
        ])
    table = Table(table_data, colWidths=[38 * mm, 132 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), _LATIN_FONT_NAME),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [
        _paragraph("反编译证据", styles["heading"]),
        table,
        Spacer(1, 6 * mm),
    ]


def _build_issue_section(
    issue: Dict[str, Any],
    index: int,
    styles: Dict[str, ParagraphStyle],
    font_name: str,
) -> List[Any]:
    """构建单个 issue 的 PDF 元素列表。

    Args:
        issue: 已归一化的 issue 字典。
        index: issue 在当前严重度分组中的序号(从 1 开始)。
        styles: 段落样式集合。
        font_name: 中文字体名称。

    Returns:
        List[Any]: PDF 元素列表(标题 / 描述 / 修复建议 / 修复代码)。
    """
    elements: List[Any] = []
    severity = issue.get("severity") or "未知"
    title = escape(str(issue.get("title") or "未命名问题"))
    color_hex = _severity_color(severity).hexval()

    # 问题标题(含严重度色块)
    title_html = (
        f'<font color="{color_hex}">[{severity}]</font> '
        f'{index}. {title}'
    )
    elements.append(_paragraph(title_html, styles["subheading"]))

    # 基本信息
    file_name = escape(str(issue.get("file_name") or "-"))
    line_number = issue.get("line_number") or "-"
    issue_type = escape(str(issue.get("issue_type") or "-"))
    cwe = escape(str(issue.get("cwe") or "-"))
    elements.append(_paragraph(
        f"文件:{file_name} | 行号:{line_number} | 类型:{issue_type} | CWE:{cwe}",
        styles["normal"],
    ))

    cvss_parts = []
    if issue.get("cvss_score") is not None:
        cvss_parts.append(f"CVSS 评分：{issue.get('cvss_score')}")
    if issue.get("cvss_vector"):
        cvss_parts.append(f"评分向量：{issue.get('cvss_vector')}")
    if issue.get("cvss_version"):
        cvss_parts.append(f"向量版本：{issue.get('cvss_version')}")
    if issue.get("cvss_source"):
        cvss_parts.append(f"评分来源：{cvss_source_label(issue.get('cvss_source'))}")
    if cvss_parts:
        elements.append(_paragraph("<b>CVSS:</b>", styles["normal"]))
        elements.append(_paragraph(escape("；".join(cvss_parts), {"&": "&amp;"}), styles["normal"]))

    source_parts = [f"发现来源：{discovery_source_label(issue.get('source'))}"]
    source_parts.append(f"独立确认数：{issue.get('confirmation_count') or 0}")
    elements.append(_paragraph("<b>来源与确认:</b>", styles["normal"]))
    elements.append(_paragraph(escape("；".join(source_parts), {"&": "&amp;"}), styles["normal"]))
    fingerprint = escape(str(issue.get("finding_fingerprint") or "-"))
    elements.append(_paragraph(f"<b>问题指纹:</b> {_code_markup(fingerprint)}", styles["normal"]))
    source_details = issue.get("source_details")
    if source_details:
        from app.services.report_exporter import _format_scope_facts

        details_lines = _format_scope_facts({"source_details": source_details}, "source")
        elements.extend(_paragraph(escape(line), styles["normal"]) for line in details_lines)

    if issue.get("aggregation_version"):
        trust_parts = [f"聚合版本：{issue.get('aggregation_version')}"]
        trust_parts.append(f"风险分：{issue.get('risk_score') if issue.get('risk_score') is not None else '-'}")
        trust_parts.append(f"证据等级：{evidence_quality_label(issue.get('evidence_quality'))}")
        trust_parts.append(f"冲突状态：{conflict_status_label(issue.get('conflict_status'))}")
        trust_parts.append(f"人工复核：{human_review_status_label(issue.get('human_review_status'))}")
        elements.append(_paragraph("<b>可信聚合:</b>", styles["normal"]))
        elements.append(_paragraph(escape("；".join(trust_parts), {"&": "&amp;"}), styles["normal"]))

    status_parts = [f"状态：{finding_status_label(issue.get('status'))}"]
    status_parts.append(f"处理人：{issue.get('handled_by') or '-'}")
    status_parts.append(f"处理时间：{issue.get('handled_at') or '-'}")
    status_parts.append(f"更新时间：{issue.get('update_time') or '-'}")
    elements.append(_paragraph("<b>处理状态:</b>", styles["normal"]))
    elements.append(_paragraph(escape("；".join(status_parts), {"&": "&amp;"}), styles["normal"]))

    # 问题描述
    description = str(issue.get("description") or "")
    if description:
        elements.append(_paragraph("<b>描述:</b>", styles["normal"]))
        _append_markdown_elements(elements, description, styles)

    evidence = issue.get("evidence") or ""
    if evidence:
        evidence_text = escape(str(evidence).expandtabs(4)).replace(" ", "&#160;").replace("\n", "<br/>")
        elements.append(_paragraph("<b>证据:</b>", styles["normal"]))
        elements.append(_paragraph(_code_markup(evidence_text), styles["code"]))

    exploit_scenario = str(issue.get("exploit_scenario") or "")
    if exploit_scenario:
        elements.append(_paragraph("<b>攻击场景:</b>", styles["normal"]))
        _append_markdown_elements(elements, exploit_scenario, styles)

    # 修复建议
    suggestion = str(issue.get("suggestion") or "")
    if suggestion:
        elements.append(_paragraph("<b>修复建议:</b>", styles["normal"]))
        _append_markdown_elements(elements, suggestion, styles)

    remediation = str(issue.get("remediation") or "")
    if remediation:
        elements.append(_paragraph("<b>详细修复:</b>", styles["normal"]))
        _append_markdown_elements(elements, remediation, styles)

    # 修复代码(Courier 字体)
    fixed_code = issue.get("fixed_code") or ""
    if fixed_code:
        # 转义 HTML 特殊字符并保留换行
        code_escaped = escape(str(fixed_code).expandtabs(4)).replace(" ", "&#160;").replace("\n", "<br/>")
        elements.append(_paragraph("<b>修复代码:</b>", styles["normal"]))
        elements.append(_paragraph(_code_markup(code_escaped), styles["code"]))

    references = issue.get("references_json") or []
    if not isinstance(references, (list, tuple)):
        references = [references]
    if references:
        elements.append(_paragraph("<b>参考:</b>", styles["normal"]))
        for reference in references:
            if reference:
                elements.append(_paragraph(_code_markup(escape(str(reference))), styles["code"]))

    elements.append(Spacer(1, 4 * mm))
    return elements


def _build_issues_section(
    issues: List[Dict[str, Any]],
    styles: Dict[str, ParagraphStyle],
    font_name: str,
    empty_text: str = "审查未完成或覆盖范围未知，没有可用结论，不能据此判定无问题。",
) -> List[Any]:
    """构建问题列表章节(按严重度分组)。

    Args:
        issues: 已归一化且按严重度排序的 issue 字典列表。
        styles: 段落样式集合。
        font_name: 中文字体名称。

    Returns:
        List[Any]: PDF 元素列表(章节标题 + 各分组 issue)。
    """
    elements: List[Any] = []
    elements.append(_paragraph("问题详情", styles["heading"]))

    grouped = _group_issues_by_severity(issues)
    has_any_issue = any(len(grouped[key]) > 0 for key in grouped)

    if not has_any_issue:
        elements.append(_paragraph(empty_text, styles["normal"]))
        return elements

    for severity in _SEVERITY_ORDER + ("其他",):
        group = grouped.get(severity, [])
        if not group:
            continue
        color_hex = _severity_color(severity).hexval()
        elements.append(_paragraph(
            f'<font color="{color_hex}">{severity}级问题({len(group)} 个)</font>',
            styles["subheading"],
        ))
        for idx, issue in enumerate(group, start=1):
            elements.extend(_build_issue_section(issue, idx, styles, font_name))

    return elements


# ============ 对外导出接口 ============

def export_to_pdf(
    task: Any,
    issues: List[Any],
    summary: Optional[str],
    score: int,
    template_type: str = "detailed",
    evidence: dict[str, Any] | None = None,
) -> bytes:
    """导出 PDF 报告字节流。

    使用 reportlab.platypus 构建 PDF 文档,包含报告头、统计摘要表、
    按严重度分组的问题列表。中文字体使用 STSong-Light CID(无方块乱码)。

    Args:
        task: 审查任务(TaskDetailOut / ReviewTask / dict)。
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。
        summary: AI 总体评价文本,可为 None。
        score: 综合评分 0-100。
        template_type: 模板类型(simple/detailed/compliance),目前仅影响
            章节详略预留,PDF 内容结构一致。默认 "detailed"。

    Returns:
        bytes: PDF 文档的二进制字节流(可直接写入文件或作为 HTTP 响应体)。
    """
    # 注册中文字体
    font_name = _ensure_chinese_font()
    styles = _build_styles(font_name)

    # 构建报告上下文(复用 T11 的归一化逻辑)
    context = export_to_dict(task, issues, summary, score, evidence)
    task_info = context.get("task_info", {})
    statistics = context.get("statistics", {})
    sorted_issues = context.get("issues", [])

    # 构建 PDF 文档
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f"代码审查报告 - {task_info.get('task_name', '')}",
        pageCompression=0,
    )

    elements: List[Any] = []

    # 1. 报告头
    elements.extend(_build_header_elements(task_info, context.get("score", score), styles))

    elements.append(_paragraph("审查范围与版本", styles["heading"]))
    for line in context["scope_lines"]:
        elements.append(_paragraph(escape(line), styles["normal"]))

    # 2. 总体评价
    summary_text = context.get("summary") or ""
    if summary_text:
        elements.append(_paragraph("总体评价", styles["heading"]))
        elements.extend(_build_summary_markdown_elements(str(summary_text), styles))
        elements.append(Spacer(1, 4 * mm))

    # 3. 反编译证据
    elements.extend(_build_decompilation_evidence_elements(
        context.get("evidence", {}), styles, font_name,
    ))

    # 4. 统计摘要
    elements.append(_paragraph("统计摘要", styles["heading"]))
    elements.append(_build_summary_table(statistics, font_name))
    elements.append(Spacer(1, 6 * mm))

    # 5. 评分明细
    elements.extend(_build_score_breakdown_elements(statistics, styles, font_name))

    # 6. 问题详情(按严重度分组)
    elements.extend(_build_issues_section(
        sorted_issues, styles, font_name,
        context.get("empty_issues_text") or "审查未完成或覆盖范围未知，没有可用结论，不能据此判定无问题。",
    ))

    security_report = context.get("source", {}).get("security_report", {})
    security_markdown = security_report.get("report_md") if isinstance(security_report, dict) else None
    if isinstance(security_markdown, str) and security_markdown.strip():
        elements.append(_paragraph("安全审计专项结果", styles["heading"]))
        elements.extend(_build_summary_markdown_elements(security_markdown, styles))

    # 构建 PDF
    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
