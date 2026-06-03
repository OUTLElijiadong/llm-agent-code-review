"""
PDF报告导出器: 使用ReportLab生成审查报告PDF
"""
import os
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _register_chinese_font():
    """注册中文字体(SimSun),若不存在则使用系统默认字体"""
    font_paths = [
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in font_paths:
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont("ChineseFont", path))
            return
    pdfmetrics.registerFont(TTFont("ChineseFont", "Helvetica"))


def export_pdf_report(detail: dict) -> BytesIO:
    """将报告详情数据渲染为PDF文档

    Args:
        detail: 报告详情字典

    Returns:
        BytesIO: PDF文档的二进制缓冲
    """
    _register_chinese_font()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)

    styles = getSampleStyleSheet()
    normal = ParagraphStyle("ChineseNormal", parent=styles["Normal"], fontName="ChineseFont", fontSize=11,
                            leading=16)
    heading = ParagraphStyle("ChineseHeading", parent=styles["Heading1"], fontName="ChineseFont", fontSize=16,
                             leading=22, spaceAfter=12)

    elements = []

    project = detail.get("project", {})
    task = detail.get("task", {})
    stats = detail.get("stats", {})

    elements.append(Paragraph("代码审查报告", styles["Title"]))
    elements.append(Spacer(1, 10 * mm))

    project_name = project.get("project_name", project.get("name", ""))
    elements.append(Paragraph(f"项目: {escape(str(project_name))}", normal))
    elements.append(Paragraph(f"语言: {escape(str(project.get('language', '')))}", normal))
    elements.append(Paragraph(f"审查时间: {escape(str(task.get('create_time', '')))}", normal))
    elements.append(Paragraph(f"综合评分: {task.get('score', 0)} / 100", normal))
    elements.append(Spacer(1, 5 * mm))

    elements.append(Paragraph("审查统计", heading))
    sev = stats.get("severity", {})
    table_data = [
        ["指标", "数值"],
        ["审查文件数", str(stats.get("total_files", 0))],
        ["发现问题数", str(stats.get("total_issues", 0))],
        ["严重问题", str(sev.get("严重", 0))],
        ["高优先级问题", str(sev.get("高", 0))],
        ["中优先级问题", str(sev.get("中", 0))],
        ["低优先级问题", str(sev.get("低", 0))],
    ]
    t = Table(table_data, colWidths=[120, 80])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 5 * mm))

    summary = detail.get("summary", "")
    if summary:
        elements.append(Paragraph("总体评价", heading))
        elements.append(Paragraph(escape(str(summary)), normal))

    doc.build(elements)
    buffer.seek(0)
    return buffer
