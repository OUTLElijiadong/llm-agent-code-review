"""AI report Markdown must render as readable, escaped document formatting."""

from io import BytesIO

from docx import Document

from app.services.report_exporter import export_to_html, load_builtin_template
from app.services.report_pdf_exporter import (
    _build_styles,
    _build_summary_markdown_elements,
    _ensure_chinese_font,
    export_to_pdf,
)
from app.services.report_word_exporter import export_to_word

SUMMARY = """# 总体结论

发现 **高危问题**，建议先修复 `access check`。

- 核对输入边界
- 补充回归测试

[官方记录](https://www.cnvd.org.cn/flaw/show/CNVD-2026-40686)
[无效链接](javascript:alert(1))

原始输入：<script>alert('xss')</script>
"""

TABLE_SUMMARY = """| 项目 | 说明 |
| --- | --- |
| **问题** | SQL *injection |
| 来源 | CNVD |
"""


def _task():
    return {
        "id": 10,
        "task_name": "Markdown 报告验证",
        "project_name": "合成项目",
        "review_type": "standard",
        "status": "success",
        "total_files": 1,
        "total_issues": 0,
        "score": 100,
        "coverage": {"coverage_status": "complete"},
    }


def test_all_builtin_html_templates_render_safe_formatted_markdown():
    for template_type in ("simple", "detailed", "compliance"):
        html = export_to_html(_task(), [], SUMMARY, 100, load_builtin_template(template_type))

        assert "<h1>总体结论</h1>" in html
        assert "<strong>高危问题</strong>" in html
        assert "<ul>" in html and "<li>核对输入边界</li>" in html
        assert '<a href="https://www.cnvd.org.cn/flaw/show/CNVD-2026-40686">官方记录</a>' in html
        assert "javascript:" not in html
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html
        assert "**高危问题**" not in html


def test_pdf_export_consumes_markdown_without_leaking_emphasis_markers():
    pdf = export_to_pdf(_task(), [], SUMMARY, 100)

    assert pdf.startswith(b"%PDF-")
    assert b"**" not in pdf
    assert b"javascript:" not in pdf


def test_word_export_uses_native_markdown_formatting_and_safe_links():
    word = export_to_word(_task(), [], SUMMARY, 100)
    document = Document(BytesIO(word))

    paragraph_text = [paragraph.text for paragraph in document.paragraphs]
    assert "总体结论" in paragraph_text
    assert "发现 高危问题，建议先修复 access check。" in paragraph_text
    assert any(paragraph.style.name == "List Bullet" for paragraph in document.paragraphs)
    assert all("**" not in text for text in paragraph_text)
    bold_runs = [run for paragraph in document.paragraphs for run in paragraph.runs if run.bold]
    assert any(run.text == "高危问题" for run in bold_runs)
    external_links = [
        relation.target_ref
        for relation in document.part.rels.values()
        if relation.reltype.endswith("/hyperlink")
    ]
    assert external_links == ["https://www.cnvd.org.cn/flaw/show/CNVD-2026-40686"]


def test_summary_tables_render_as_tables_in_html_word_and_pdf_without_raw_markers():
    html = export_to_html(_task(), [], TABLE_SUMMARY, 100, load_builtin_template("detailed"))
    assert '<table class="report-summary-table">' in html
    assert "<th>项目</th>" in html and "<td>SQL injection</td>" in html
    assert "*injection" not in html

    document = Document(BytesIO(export_to_word(_task(), [], TABLE_SUMMARY, 100)))
    summary_table = next(
        table for table in document.tables
        if [cell.text for cell in table.rows[0].cells] == ["项目", "说明"]
    )
    table_text = [[cell.text for cell in row.cells] for row in summary_table.rows]
    assert table_text == [["项目", "说明"], ["问题", "SQL injection"], ["来源", "CNVD"]]

    elements = _build_summary_markdown_elements(TABLE_SUMMARY, _build_styles(_ensure_chinese_font()))
    table = next(element for element in elements if element.__class__.__name__ == "Table")
    pdf_table_text = [
        cell.getPlainText()
        for row in table._cellvalues
        for cell in row
        if hasattr(cell, "getPlainText")
    ]
    assert pdf_table_text == ["项目", "说明", "问题", "SQL injection", "来源", "CNVD"]
