"""C28：同一隔离任务的页面事实与实际导出成品对照。"""

import json
from datetime import datetime, timezone
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from test_report_api import admin_client as report_admin_client

from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.services.report_exporter import export_to_dict
from app.services.report_pdf_exporter import _build_issue_section, _build_styles, _ensure_chinese_font


@pytest.fixture
def admin_client():
    yield from report_admin_client.__wrapped__()


def enrich_report_sample(session, task_id):
    task = session.get(ReviewTask, task_id)
    session.get(Project, task.project_id).project_name = "棱镜中文审计平台"
    task.task_name = "鉴权与账号隔离复核"
    task.total_files = task.processed_files = 3
    task.coverage = {"complete": False, "reason": "第三方依赖未纳入本次源码审查"}
    stamp = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    task.create_time = task.start_time = task.end_time = stamp
    for index, name in enumerate(["鉴权服务.py", "工具函数.py", "无问题配置.py"], 100):
        session.add(CodeFile(
            id=index, project_id=task.project_id, file_name=name, file_path=f"源码/{name}",
            language="python", content="print('中文')", status="active",
        ))
        session.add(ReviewTaskFile(
            task_id=task_id, file_id=index, version_no=7, content_sha256="a" * 64,
            file_snapshot={"file_name": name, "file_path": f"源码/{name}", "language": "python"},
        ))
    issues = session.query(ReviewIssue).filter_by(task_id=task_id).order_by(ReviewIssue.id).all()
    for issue in issues:
        issue.file_name = "鉴权服务.py" if issue.file_id == 100 else "工具函数.py"
        issue.evidence = "用户标识 = 请求参数\n数据库执行(用户标识) # 中文证据末尾"
        issue.fixed_code = "安全参数 = 验证(用户标识)\n参数化查询(安全参数) # 修复代码末尾"
        issue.source_details = [{"source": "llm:中文审查员", "evidence": "中文来源证据"}]
        issue.create_time = issue.update_time = stamp
    session.commit()


def collect_report_files(client, task_id):
    detail = client.get(f"/api/reports/{task_id}")
    assert detail.status_code == 200
    outputs = {}
    for format_name in ("json", "html", "pdf", "word"):
        response = client.get(f"/api/reports/tasks/{task_id}/export?format={format_name}")
        assert response.status_code == 200
        outputs[format_name] = response.content
    return detail.json()["data"], outputs


def word_text(content):
    with ZipFile(BytesIO(content)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "\n".join(root.itertext())


@pytest.mark.parametrize("format_name", ["json", "html", "word"])
def test_exported_project_name_matches_detail(admin_client, format_name):
    client, session, task_id = admin_client
    enrich_report_sample(session, task_id)
    detail, files = collect_report_files(client, task_id)
    expected = detail["project"]["project_name"]
    if format_name == "word":
        text = word_text(files[format_name])
    else:
        text = files[format_name].decode("utf-8")
    assert expected in text


def test_json_export_matches_page_file_inventory_and_frozen_versions(admin_client):
    client, session, task_id = admin_client
    enrich_report_sample(session, task_id)
    detail, files = collect_report_files(client, task_id)
    report = json.loads(files["json"])
    assert report["statistics"]["total_issues"] == detail["stats"]["total_issues"] == 3
    assert report["score"] == detail["stats"]["score"] == 74
    assert {item["file_name"] for item in report.get("files", [])} == {
        item["file_name"] for item in detail["files"]
    }
    assert all(item["version_no"] == 7 for item in report["files"])
    assert all(item["content_sha256"] == "a" * 64 for item in report["files"])
    assert report["task_info"]["coverage"] == session.get(ReviewTask, task_id).coverage


def test_pdf_chinese_code_does_not_fall_back_to_dingbat_squares(admin_client):
    client, session, task_id = admin_client
    enrich_report_sample(session, task_id)
    _detail, files = collect_report_files(client, task_id)
    assert b"/ZapfDingbats" not in files["pdf"]


@pytest.mark.parametrize("template", ["simple", "detailed", "compliance"])
@pytest.mark.parametrize("coverage", [None, {"stage": "complete"}, {"stage": "failed", "reason": "未审查依赖"}])
def test_html_templates_preserve_frozen_scope_and_unknown_coverage(admin_client, template, coverage):
    client, session, task_id = admin_client
    enrich_report_sample(session, task_id)
    session.get(ReviewTask, task_id).coverage = coverage
    session.get(CodeFile, 102).file_name = "审查后改名.py"
    session.commit()
    response = client.get(f"/api/reports/tasks/{task_id}/export?format=html&template_type={template}")
    assert response.status_code == 200
    assert "无问题配置.py" in response.text
    assert "审查后改名.py" not in response.text
    assert "版本：7" in response.text
    assert "a" * 64 in response.text
    assert "不等同于语义覆盖率" in response.text
    if coverage is None:
        assert "未记录，不能据此判定完整覆盖" in response.text
    else:
        assert coverage["stage"] in response.text
    assert 'class="table-scroll"' in response.text


@pytest.mark.parametrize("format_name", ["json", "html", "word"])
def test_generate_and_legacy_downloads_use_same_scope(admin_client, format_name):
    client, session, task_id = admin_client
    enrich_report_sample(session, task_id)
    response = client.post("/api/reports/generate", json={"task_id": task_id, "format": format_name})
    assert response.status_code == 200
    text = word_text(response.content) if format_name == "word" else response.text
    for fact in ["棱镜中文审计平台", "无问题配置.py", "第三方依赖未纳入本次源码审查"]:
        assert fact in text
    if format_name == "word":
        legacy = client.get(f"/api/reports/{task_id}/export/word")
        assert legacy.status_code == 200
        assert "无问题配置.py" in word_text(legacy.content)


def test_empty_report_retains_all_submitted_files_and_historical_score(admin_client):
    client, session, task_id = admin_client
    enrich_report_sample(session, task_id)
    session.query(ReviewIssue).filter_by(task_id=task_id).delete()
    session.commit()
    detail, files = collect_report_files(client, task_id)
    report = json.loads(files["json"])
    assert len(report["files"]) == len(detail["files"]) == 3
    assert report["statistics"]["total_issues"] == detail["stats"]["total_issues"] == 0
    assert report["score"] == detail["stats"]["score"] == 65
    assert report["statistics"]["score_breakdown"]["score_source"] == "task_explicit_empty_report"


def test_legacy_file_inventory_does_not_invent_frozen_versions(admin_client):
    client, _session, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}/export?format=json")
    assert response.status_code == 200
    report = response.json()
    assert len(report["files"]) == 2
    assert all(item["version_no"] is None and item["content_sha256"] is None for item in report["files"])
    assert report["task_info"]["coverage"] is None


def test_pdf_code_fragments_select_chinese_font_and_preserve_literal_markup():
    font_name = _ensure_chinese_font()
    issue = {"severity": "高", "evidence": "证据 <script>中文</script>",
             "fixed_code": "修复 = '<中文>'\n英文 = ascii_tail"}
    paragraphs = _build_issue_section(issue, 1, _build_styles(font_name), font_name)
    texts = [item.getPlainText().replace("\u00a0", " ") for item in paragraphs if hasattr(item, "getPlainText")]
    assert issue["evidence"] in texts
    assert "修复 = '<中文>'英文 = ascii_tail" in texts
    for item in paragraphs:
        if not hasattr(item, "frags"):
            continue
        for frag in item.frags:
            if any("\u4e00" <= char <= "\u9fff" for char in getattr(frag, "text", "")):
                assert frag.fontName == font_name


def test_normalization_keeps_unknown_scope_unknown_for_direct_export():
    context = export_to_dict({"id": 1, "task_name": "历史任务"}, [], "", 100)
    assert context["files"] == []
    assert context["task_info"]["coverage"] is None
    assert any("不能由问题列表推断全部送审文件" in line for line in context["scope_lines"])


def test_pdf_code_preserves_python_indentation():
    font_name = _ensure_chinese_font()
    paragraphs = _build_issue_section(
        {"severity": "中", "fixed_code": "if value:\n    return value\n        nested()"},
        1, _build_styles(font_name), font_name,
    )
    text = [p.getPlainText() for p in paragraphs if hasattr(p, "getPlainText")][-1].replace("\u00a0", " ")
    assert " " * 4 + "return value" in text
    assert " " * 8 + "nested()" in text
