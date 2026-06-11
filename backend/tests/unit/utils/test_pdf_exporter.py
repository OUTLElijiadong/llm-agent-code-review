from app.exporters import pdf_exporter


def test_export_pdf_report_with_cid_font_fallback(monkeypatch):
    """验证缺少系统中文字体文件时仍能导出 PDF。

    Args:
        monkeypatch: pytest 提供的运行时替换工具。

    Returns:
        None: 断言导出的二进制内容为有效 PDF。
    """
    monkeypatch.setattr(pdf_exporter.os.path, "exists", lambda _: False)

    detail = {
        "project": {"project_name": "中文项目", "language": "python"},
        "task": {"create_time": "2026-06-12", "score": 88},
        "stats": {
            "total_files": 1,
            "total_issues": 1,
            "severity": {"严重": 0, "高": 1, "中": 0, "低": 0},
        },
        "summary": "总体评价：PDF 导出中文内容验证。",
    }

    buffer = pdf_exporter.export_pdf_report(detail)

    assert buffer.getvalue().startswith(b"%PDF-")
    assert len(buffer.getvalue()) > 1000
