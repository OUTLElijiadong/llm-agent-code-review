"""Word 报告导出服务 (T12)

基于 python-docx 构建 .docx 文档,支持中文字体与严重度颜色编码
(严重:红 / 高:橙 / 中:黄 / 低:蓝),修复代码使用 Courier New 字体。

依赖:
- T11 report_exporter.export_to_dict: 构建报告上下文(task_info/issues/statistics)
- python-docx >= 1.1.0: Word 文档生成

文档结构:
    1. 报告头(标题 / 任务名 / 审查时间 / 综合评分)
    2. 统计摘要表(问题总数 / 严重度分布 / 已修复数)
    3. 问题列表(每个问题一个段落,按严重度分组)
"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

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
from app.services.report_markdown import MarkdownSegment, markdown_to_blocks, parse_markdown_blocks

# ============ 模块常量 ============

# 严重度 → RGB 颜色映射(python-docx RGBColor)
_SEVERITY_RGB: Dict[str, RGBColor] = {
    "严重": RGBColor(0xDC, 0x26, 0x26),  # 红色
    "高": RGBColor(0xEA, 0x58, 0x0C),    # 橙色
    "中": RGBColor(0xCA, 0x8A, 0x04),    # 黄色
    "低": RGBColor(0x25, 0x63, 0xEB),    # 蓝色
}

# 严重度展示顺序(严重 > 高 > 中 > 低 > 其他)
_SEVERITY_ORDER: Tuple[str, ...] = ("严重", "高", "中", "低")

# 中文字体名称(Noto Sans SC 在本机与服务端字体集内可用，支持简体中文)
_CHINESE_FONT: str = "Noto Sans SC"

# 代码字体名称(Courier New 等宽字体,适合展示代码)
_CODE_FONT: str = "Courier New"


# ============ 内部辅助 ============

def _severity_rgb(severity: Optional[str]) -> RGBColor:
    """获取严重度对应的 RGB 颜色。

    Args:
        severity: 严重度字符串(严重/高/中/低)。

    Returns:
        RGBColor: 对应的 RGB 颜色对象;未知严重度返回灰色。
    """
    if severity and severity in _SEVERITY_RGB:
        return _SEVERITY_RGB[severity]
    return RGBColor(0x6B, 0x72, 0x80)  # 灰色


def _set_run_chinese_font(run, font_name: str) -> None:
    """设置 run 的中文字体(包括东亚字体)。

    python-docx 默认只设置西文字体,中文需要额外设置 w:eastAsia 属性。

    Args:
        run: docx.run.Run 对象。
        font_name: 字体名称(如 "SimSun")。

    Returns:
        None
    """
    run.font.name = font_name
    # 设置东亚字体(中文渲染必需)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        from docx.oxml import OxmlElement
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), _CHINESE_FONT if font_name == _CODE_FONT else font_name)


def _add_run(paragraph, text: str, *, bold: bool = False, color: Optional[RGBColor] = None,
             font_name: str = _CHINESE_FONT, size: int = 11) -> Any:
    """向段落添加一个 run 并设置格式。

    Args:
        paragraph: docx 段落对象。
        text: run 文本内容。
        bold: 是否加粗,默认 False。
        color: 文字颜色(RGBColor),默认 None(黑色)。
        font_name: 字体名称,默认 SimSun(中文)。
        size: 字号(pt),默认 11。

    Returns:
        Any: 创建的 run 对象。
    """
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color
    _set_run_chinese_font(run, font_name)
    return run


def _append_markdown_runs(paragraph: Any, segments: tuple[MarkdownSegment, ...], *, size: int = 10) -> None:
    for segment in segments:
        run = _add_run(
            paragraph,
            segment.text,
            bold=segment.bold,
            font_name=_CODE_FONT if segment.code else _CHINESE_FONT,
            size=9 if segment.code else size,
        )
        run.italic = segment.italic


def _append_markdown_to_word(doc: Any, value: str | None) -> None:
    """把 Markdown 语义映射到 Word 标题、列表、表格、引用和代码段落。"""
    ordered_depth: int | None = None
    ordered_index = 0
    for block in markdown_to_blocks(value):
        if block.kind != "ordered":
            ordered_depth = None
            ordered_index = 0
        if block.kind == "rule":
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(4)
            continue
        if block.kind == "table":
            if not block.rows:
                continue
            table = doc.add_table(rows=len(block.rows), cols=max(len(row) for row in block.rows))
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            table.autofit = True
            for row_index, row in enumerate(block.rows):
                for column_index, cell_segments in enumerate(row):
                    cell = table.cell(row_index, column_index)
                    paragraph = cell.paragraphs[0]
                    _append_markdown_runs(paragraph, cell_segments, size=9)
                    if row_index == 0:
                        for run in paragraph.runs:
                            run.bold = True
                        shading = OxmlElement("w:shd")
                        shading.set(qn("w:fill"), "E8EEF8")
                        cell._tc.get_or_add_tcPr().append(shading)
            doc.add_paragraph()
            continue
        if block.kind == "heading":
            paragraph = doc.add_heading(level=min(3, max(1, block.level)))
            _append_markdown_runs(paragraph, block.segments, size=12 if block.level <= 2 else 11)
            continue
        if block.kind == "code":
            paragraph = doc.add_paragraph(style="No Spacing")
            paragraph.paragraph_format.left_indent = Inches(0.15)
            code = block.segments[0].text if block.segments else ""
            for index, line in enumerate(code.split("\n")):
                if index:
                    paragraph.add_run().add_break()
                _add_run(paragraph, line, font_name=_CODE_FONT, size=9)
            continue
        if block.kind == "bullet":
            paragraph = doc.add_paragraph(style="List Bullet")
            paragraph.paragraph_format.left_indent = Inches(0.25 * max(1, block.list_depth))
        elif block.kind == "ordered":
            ordered_index = ordered_index + 1 if ordered_depth == block.list_depth else 1
            ordered_depth = block.list_depth
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Inches(0.25 * max(1, block.list_depth))
            paragraph.paragraph_format.first_line_indent = Inches(-0.2)
            _add_run(paragraph, f"{ordered_index}. ", size=10)
        elif block.kind == "quote":
            paragraph = doc.add_paragraph(style="Quote")
            paragraph.paragraph_format.left_indent = Inches(0.25 * max(1, block.list_depth))
        else:
            paragraph = doc.add_paragraph()
        _append_markdown_runs(paragraph, block.segments)


def _add_hyperlink(paragraph: Any, text: str, href: str) -> None:
    """Add an external hyperlink whose URL has passed the safe Markdown parser."""
    relationship_id = paragraph.part.relate_to(
        href, RELATIONSHIP_TYPE.HYPERLINK, is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "2563EB")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.extend((color, underline))
    run.append(properties)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _append_summary_runs(paragraph: Any, runs: List[Dict[str, Any]]) -> None:
    for item in runs:
        text = str(item.get("text") or "")
        href = item.get("href")
        if href:
            _add_hyperlink(paragraph, text, str(href))
            continue
        run = _add_run(
            paragraph,
            text,
            bold=bool(item.get("bold")),
            font_name=_CODE_FONT if item.get("code") else _CHINESE_FONT,
            size=9 if item.get("code") else 11,
        )
        run.italic = bool(item.get("italic"))


def _add_summary_markdown(doc: Document, markdown: str) -> None:
    """Render supported Markdown as native Word headings, lists, tables and links."""
    for block in parse_markdown_blocks(markdown):
        kind = block["type"]
        if kind == "heading":
            level = max(1, min(4, int(block.get("level") or 2)))
            paragraph = doc.add_paragraph(style=f"Heading {level}")
            _append_summary_runs(paragraph, block["runs"])
        elif kind == "paragraph":
            _append_summary_runs(doc.add_paragraph(), block["runs"])
        elif kind in {"unordered_list", "ordered_list"}:
            style = "List Bullet" if kind == "unordered_list" else "List Number"
            for item in block["items"]:
                _append_summary_runs(doc.add_paragraph(style=style), item)
        elif kind == "table":
            rows = block.get("rows") or []
            if not rows:
                continue
            table = doc.add_table(rows=len(rows), cols=max(len(row) for row in rows))
            table.style = "Table Grid"
            for row_index, row in enumerate(rows):
                for column_index, cell_runs in enumerate(row):
                    cell = table.cell(row_index, column_index)
                    paragraph = cell.paragraphs[0]
                    _append_summary_runs(paragraph, cell_runs)
                    if row_index == 0:
                        for run in paragraph.runs:
                            run.bold = True
                        shading = OxmlElement("w:shd")
                        shading.set(qn("w:fill"), "E8EEF8")
                        cell._tc.get_or_add_tcPr().append(shading)
            doc.add_paragraph()
        elif kind == "quote":
            _append_summary_runs(doc.add_paragraph(style="Quote"), block["runs"])
        elif kind == "code":
            paragraph = doc.add_paragraph(style="No Spacing")
            run = _add_run(paragraph, str(block.get("text") or ""), font_name=_CODE_FONT, size=9)
            run.font.color.rgb = RGBColor(0x37, 0x47, 0x51)
        elif kind == "rule":
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(6)


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


# ============ Word 文档构建 ============

def _build_header(doc: Any, task_info: Dict[str, Any], score: int) -> None:
    """构建报告头部(标题 + 任务元信息)。

    Args:
        doc: python-docx Document 对象。
        task_info: 任务信息字典。
        score: 综合评分。

    Returns:
        None
    """
    # 主标题(居中)
    title_para = doc.add_heading(level=0)
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_run(title_para, "代码审查报告", bold=True, size=22)

    # 任务元信息
    meta_fields = [
        ("任务名称", str(task_info.get("task_name") or "未命名任务")),
        ("所属项目", str(task_info.get("project_name") or "-")),
        ("审查类型", str(task_info.get("review_type") or "-")),
        ("审查模型", str(task_info.get("model_name") or "-")),
        ("生成时间", str(task_info.get("create_time") or "-")),
    ]
    for label, value in meta_fields:
        para = doc.add_paragraph()
        _add_run(para, f"{label}: ", bold=True)
        _add_run(para, value)

    # 综合评分(加粗显示)
    score_para = doc.add_paragraph()
    _add_run(score_para, "综合评分: ", bold=True)
    _add_run(score_para, _format_score(score), bold=True, color=RGBColor(0x11, 0x18, 0x27))

    status_para = doc.add_paragraph()
    _add_run(status_para, "执行状态: ", bold=True)
    _add_run(status_para, report_status_label(task_info.get("status")))
    if task_info.get("error_message"):
        error_para = doc.add_paragraph()
        _add_run(error_para, "错误: ", bold=True)
        _add_run(error_para, str(task_info["error_message"]))


def _build_summary_table(doc: Any, statistics: Dict[str, Any]) -> None:
    """构建统计摘要表格。

    Args:
        doc: python-docx Document 对象。
        statistics: 统计信息字典。

    Returns:
        None
    """
    doc.add_heading("统计摘要", level=1)

    severity_count = statistics.get("severity_count", {})
    rows_data = [
        ("指标", "数值"),
        ("问题总数", str(statistics.get("total_issues", 0))),
        ("严重问题", str(severity_count.get("严重", 0))),
        ("高优先级问题", str(severity_count.get("高", 0))),
        ("中优先级问题", str(severity_count.get("中", 0))),
        ("低优先级问题", str(severity_count.get("低", 0))),
        ("已修复问题", str(statistics.get("fixed_count", 0))),
    ]

    table = doc.add_table(rows=len(rows_data), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"

    for row_idx, (label, value) in enumerate(rows_data):
        cells = table.rows[row_idx].cells
        # 标签列
        label_para = cells[0].paragraphs[0]
        label_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_run(label_para, label, bold=(row_idx == 0), size=10)
        # 数值列
        value_para = cells[1].paragraphs[0]
        value_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_run(value_para, value, bold=(row_idx == 0), size=10)

    # 设置列宽
    for row in table.rows:
        row.cells[0].width = Pt(120)
        row.cells[1].width = Pt(80)


def _build_score_breakdown(doc: Any, statistics: Dict[str, Any]) -> None:
    """写入所有报告出口共用的 15/8/3/1 评分明细。"""
    breakdown = statistics.get("score_breakdown") or {}
    if breakdown.get("score") is None:
        doc.add_heading("评分明细", level=1)
        table = doc.add_table(rows=3, cols=2)
        table.style = "Table Grid"
        for row_idx, (label, value) in enumerate((
            ("字段", "值"), ("评分明细", "未评估"),
            ("评分来源", str(breakdown.get("score_source") or "未提供")),
        )):
            _add_run(table.rows[row_idx].cells[0].paragraphs[0], label, bold=(row_idx == 0), size=10)
            _add_run(table.rows[row_idx].cells[1].paragraphs[0], value, bold=(row_idx == 0), size=10)
        return
    weights = breakdown.get("weights") or {}
    counts = breakdown.get("counts") or {}
    deductions = breakdown.get("deductions") or {}
    rows = [
        ("字段", "值"),
        ("评分版本", str(breakdown.get("version") or "-")),
        ("基础分", str(breakdown.get("base_score", 100))),
        (
            "扣分权重",
            "严重 × {0}；高 × {1}；中 × {2}；低 × {3}".format(
                weights.get("严重", 15),
                weights.get("高", 8),
                weights.get("中", 3),
                weights.get("低", 1),
            ),
        ),
        (
            "问题计数",
            "严重 {0}；高 {1}；中 {2}；低 {3}".format(
                counts.get("严重", 0),
                counts.get("高", 0),
                counts.get("中", 0),
                counts.get("低", 0),
            ),
        ),
        (
            "分级扣分",
            "严重 {0}；高 {1}；中 {2}；低 {3}".format(
                deductions.get("严重", 0),
                deductions.get("高", 0),
                deductions.get("中", 0),
                deductions.get("低", 0),
            ),
        ),
        ("总扣分", str(breakdown.get("total_deduction", 0))),
        ("原始分", str(breakdown.get("raw_score", 100))),
        ("最终分", str(breakdown.get("score", 100))),
        ("风险等级", str(breakdown.get("risk_level") or "-")),
        ("评分来源", str(breakdown.get("score_source") or "-")),
    ]
    override = breakdown.get("compatibility_override")
    if isinstance(override, dict):
        rows.append((
            "兼容说明",
            f"{override.get('reason') or '-'}; canonical={override.get('canonical_score')}",
        ))

    doc.add_heading("评分明细", level=1)
    table = doc.add_table(rows=len(rows), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    for row_idx, (label, value) in enumerate(rows):
        _add_run(
            table.rows[row_idx].cells[0].paragraphs[0],
            label,
            bold=(row_idx == 0),
            size=10,
        )
        _add_run(
            table.rows[row_idx].cells[1].paragraphs[0],
            value,
            bold=(row_idx == 0),
            size=10,
        )


def _build_decompilation_evidence(doc: Any, evidence: Dict[str, Any]) -> None:
    """将结构化反编译证据写入 Word 报告。"""
    decompilation = evidence.get("decompilation") if isinstance(evidence, dict) else None
    if not isinstance(decompilation, dict):
        return

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
    doc.add_heading("反编译证据", level=1)
    table = doc.add_table(rows=len(rows) + 1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    for row_idx, (label, value) in enumerate([("字段", "值"), *rows]):
        label_para = table.rows[row_idx].cells[0].paragraphs[0]
        value_para = table.rows[row_idx].cells[1].paragraphs[0]
        _add_run(label_para, str(label), bold=(row_idx == 0), size=10)
        _add_run(
            value_para,
            str(value),
            bold=(row_idx == 0),
            font_name=_CODE_FONT if row_idx > 0 else _CHINESE_FONT,
            size=9 if row_idx > 0 else 10,
        )


def _build_issue_paragraph(doc: Any, issue: Dict[str, Any], index: int, severity: str) -> None:
    """构建单个 issue 的段落(标题 + 基本信息 + 描述 + 修复建议 + 修复代码)。

    Args:
        doc: python-docx Document 对象。
        issue: 已归一化的 issue 字典。
        index: issue 在当前严重度分组中的序号(从 1 开始)。
        severity: 严重度字符串。

    Returns:
        None
    """
    color = _severity_rgb(severity)

    # 问题标题(严重度色块 + 序号 + 标题)
    title_para = doc.add_paragraph()
    _add_run(title_para, f"[{severity}] ", bold=True, color=color, size=12)
    title_text = str(issue.get("title") or "未命名问题")
    _add_run(title_para, f"{index}. {title_text}", bold=True, size=12)

    # 基本信息
    info_para = doc.add_paragraph()
    file_name = str(issue.get("file_name") or "-")
    line_number = issue.get("line_number") or "-"
    issue_type = str(issue.get("issue_type") or "-")
    cwe = str(issue.get("cwe") or "-")
    _add_run(info_para, f"文件:{file_name} | 行号:{line_number} | 类型:{issue_type} | CWE:{cwe}", size=10)

    cvss_parts = []
    if issue.get("cvss_score") is not None:
        cvss_parts.append(f"CVSS 评分：{issue.get('cvss_score')}")
    if issue.get("cvss_vector"):
        cvss_parts.append(f"评分向量：{issue.get('cvss_vector')}")
    if issue.get("cvss_version"):
        cvss_parts.append(f"向量版本：{issue.get('cvss_version')}")
    if issue.get("cvss_source"):
        cvss_parts.append(f"评分来源: {cvss_source_label(issue.get('cvss_source'))}")
    if cvss_parts:
        cvss_para = doc.add_paragraph()
        _add_run(cvss_para, "CVSS: ", bold=True, size=10)
        _add_run(cvss_para, "；".join(cvss_parts), size=9)

    source_para = doc.add_paragraph()
    _add_run(source_para, "来源与确认: ", bold=True, size=10)
    _add_run(
        source_para,
        "发现来源: {0}；独立确认数: {1}；问题指纹: {2}".format(
            discovery_source_label(issue.get("source")),
            issue.get("confirmation_count") or 0,
            issue.get("finding_fingerprint") or "-",
        ),
        font_name=_CODE_FONT,
        size=9,
    )
    if issue.get("source_details"):
        from app.services.report_exporter import _format_scope_facts

        details_para = doc.add_paragraph()
        _add_run(details_para, "来源明细: ", bold=True, size=10)
        details = _format_scope_facts({"source_details": issue["source_details"]}, "source")
        _add_run(details_para, "；".join(details), size=9)

    if issue.get("aggregation_version"):
        trust_para = doc.add_paragraph()
        _add_run(trust_para, "可信聚合: ", bold=True, size=10)
        _add_run(
            trust_para,
            "版本: {0}；风险分: {1}；证据等级: {2}；冲突: {3}；人工复核: {4}".format(
                issue.get("aggregation_version") or "-",
                issue.get("risk_score") if issue.get("risk_score") is not None else "-",
                evidence_quality_label(issue.get("evidence_quality")),
                conflict_status_label(issue.get("conflict_status")),
                human_review_status_label(issue.get("human_review_status")),
            ),
            size=9,
        )

    status_para = doc.add_paragraph()
    _add_run(status_para, "处理状态: ", bold=True, size=10)
    _add_run(
        status_para,
        "状态: {0}；处理人: {1}；处理时间: {2}；更新时间: {3}".format(
            finding_status_label(issue.get("status")),
            issue.get("handled_by") or "-",
            issue.get("handled_at") or "-",
            issue.get("update_time") or "-",
        ),
        size=9,
    )

    # 问题描述
    description = issue.get("description") or ""
    if description:
        desc_para = doc.add_paragraph()
        _add_run(desc_para, "描述:", bold=True, size=10)
        _append_markdown_runs(desc_para, (MarkdownSegment(" "),))
        _append_markdown_to_word(doc, str(description))

    evidence = issue.get("evidence") or ""
    if evidence:
        evidence_para = doc.add_paragraph()
        _add_run(evidence_para, "证据:", bold=True, size=10)
        _add_run(evidence_para, str(evidence), font_name=_CODE_FONT, size=9)

    exploit_scenario = issue.get("exploit_scenario") or ""
    if exploit_scenario:
        exploit_para = doc.add_paragraph()
        _add_run(exploit_para, "攻击场景:", bold=True, size=10)
        _append_markdown_runs(exploit_para, (MarkdownSegment(" "),))
        _append_markdown_to_word(doc, str(exploit_scenario))

    # 修复建议
    suggestion = issue.get("suggestion") or ""
    if suggestion:
        sug_para = doc.add_paragraph()
        _add_run(sug_para, "修复建议:", bold=True, size=10)
        _append_markdown_runs(sug_para, (MarkdownSegment(" "),))
        _append_markdown_to_word(doc, str(suggestion))

    remediation = issue.get("remediation") or ""
    if remediation:
        remediation_para = doc.add_paragraph()
        _add_run(remediation_para, "详细修复:", bold=True, size=10)
        _append_markdown_runs(remediation_para, (MarkdownSegment(" "),))
        _append_markdown_to_word(doc, str(remediation))

    # 修复代码(Courier New 字体)
    fixed_code = issue.get("fixed_code") or ""
    if fixed_code:
        code_label_para = doc.add_paragraph()
        code_label_para.paragraph_format.keep_with_next = True
        _add_run(code_label_para, "修复代码:", bold=True, size=10)
        # 代码内容使用 Courier New 等宽字体
        code_para = doc.add_paragraph()
        _add_run(code_para, str(fixed_code), font_name=_CODE_FONT, size=9,
                 color=RGBColor(0x05, 0x96, 0x69))

    references = issue.get("references_json") or []
    if not isinstance(references, (list, tuple)):
        references = [references]
    if references:
        refs_para = doc.add_paragraph()
        _add_run(refs_para, "参考:", bold=True, size=10)
        _add_run(
            refs_para,
            "\n".join(str(reference) for reference in references if reference),
            size=9,
        )


def _build_issues_section(doc: Any, issues: List[Dict[str, Any]]) -> None:
    """构建问题列表章节(按严重度分组)。

    Args:
        doc: python-docx Document 对象。
        issues: 已归一化且按严重度排序的 issue 字典列表。

    Returns:
        None
    """
    doc.add_heading("问题详情", level=1)

    grouped = _group_issues_by_severity(issues)
    has_any_issue = any(len(grouped[key]) > 0 for key in grouped)

    if not has_any_issue:
        empty_para = doc.add_paragraph()
        _add_run(empty_para, "本次审查未发现问题。", size=11)
        return

    for severity in _SEVERITY_ORDER + ("其他",):
        group = grouped.get(severity, [])
        if not group:
            continue
        color = _severity_rgb(severity)
        # 分组标题
        group_para = doc.add_paragraph()
        _add_run(group_para, f"{severity}级问题({len(group)} 个)", bold=True, color=color, size=12)

        for idx, issue in enumerate(group, start=1):
            _build_issue_paragraph(doc, issue, idx, severity)


# ============ 对外导出接口 ============

def export_to_word(
    task: Any,
    issues: List[Any],
    summary: Optional[str],
    score: int,
    template_type: str = "detailed",
    evidence: dict[str, Any] | None = None,
) -> bytes:
    """导出 Word 报告字节流。

    使用 python-docx 构建 .docx 文档,包含报告头、统计摘要表、
    按严重度分组的问题列表。中文字体使用 SimSun,修复代码使用 Courier New。

    Args:
        task: 审查任务(TaskDetailOut / ReviewTask / dict)。
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。
        summary: AI 总体评价文本,可为 None。
        score: 综合评分 0-100。
        template_type: 模板类型(simple/detailed/compliance),目前仅影响
            章节详略预留,Word 内容结构一致。默认 "detailed"。

    Returns:
        bytes: Word 文档的二进制字节流(可直接写入文件或作为 HTTP 响应体)。
    """
    # 构建报告上下文(复用 T11 的归一化逻辑)
    context = export_to_dict(task, issues, summary, score, evidence)
    task_info = context.get("task_info", {})
    statistics = context.get("statistics", {})
    sorted_issues = context.get("issues", [])
    summary_text = context.get("summary") or ""

    # 创建 Word 文档
    doc = Document()

    # 设置默认样式字体
    normal_style = doc.styles["Normal"]
    normal_style.font.name = _CHINESE_FONT
    normal_style.font.size = Pt(11)
    # 设置默认样式的东亚字体
    rpr = normal_style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        from docx.oxml import OxmlElement
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), _CHINESE_FONT)

    # 1. 报告头
    _build_header(doc, task_info, context.get("score", score))

    doc.add_heading("审查范围与版本", level=1)
    for line in context["scope_lines"]:
        _add_run(doc.add_paragraph(), line, size=10)

    # 2. 总体评价
    if summary_text:
        doc.add_heading("总体评价", level=1)
        _add_summary_markdown(doc, str(summary_text))

    # 3. 反编译证据
    _build_decompilation_evidence(doc, context.get("evidence", {}))

    # 4. 统计摘要表
    _build_summary_table(doc, statistics)

    # 5. 评分明细
    _build_score_breakdown(doc, statistics)

    # 6. 问题详情(按严重度分组)
    _build_issues_section(doc, sorted_issues)

    security_report = context.get("source", {}).get("security_report", {})
    security_markdown = security_report.get("report_md") if isinstance(security_report, dict) else None
    if isinstance(security_markdown, str) and security_markdown.strip():
        doc.add_heading("安全审计专项结果", level=1)
        _add_summary_markdown(doc, security_markdown)

    # 导出字节流
    buffer = BytesIO()
    doc.save(buffer)
    word_bytes = buffer.getvalue()
    buffer.close()
    return word_bytes
