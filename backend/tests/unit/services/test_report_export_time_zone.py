"""报告导出时间口径：机器可读(JSON)一律 UTC 带 Z，人读(HTML/PDF/Word/Markdown)一律北京时间并标注“（UTC+8）”。

线上事故回顾：任务 #231 库内时间 08:20(UTC)，页面一处显示 16:20、一处显示 08:20。导出文档若直接打印库内
naive 时间，就会第三次出现“08:20”。这里固定：同一时刻在所有导出格式里读者看到的是 16:20（UTC+8）。
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import reports as report_api
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.pentest import PentestEngagement
from app.models.project import Project
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import security_report_service
from app.services.domain_report_exporter import (
    build_pentest_report_markdown,
    build_remote_pentest_report_markdown,
)
from app.services.report_exporter import export_to_dict, export_to_html, export_to_json, load_builtin_template
from app.services.report_pdf_exporter import _build_header_elements, _build_issue_section, _build_styles
from app.services.report_pdf_exporter import _ensure_chinese_font
from app.services.report_word_exporter import export_to_word

STORED = datetime(2026, 10, 22, 8, 20, 0)  # 库内 naive，数值是 UTC
BEIJING_TEXT = "2026-10-22 16:20:00（UTC+8）"
INPUT_SHAPES = [
    pytest.param(STORED, id="naive-datetime"),
    pytest.param(STORED.replace(tzinfo=timezone.utc), id="aware-datetime"),
    pytest.param("2026-10-22T08:20:00", id="iso-naive"),
    pytest.param("2026-10-22T08:20:00Z", id="iso-z"),
    pytest.param("2026-10-22T08:20:00+00:00", id="iso-offset"),
    pytest.param("2026-10-22 08:20:00", id="space-separated"),
]


def _task(**overrides):
    base = {
        "id": 231, "task_name": "任务231", "project_id": 1, "project_name": "时间口径项目",
        "review_type": "security", "status": "success", "total_files": 1, "processed_files": 1,
        "total_issues": 1, "severe_issues": 0, "high_issues": 1, "medium_issues": 0, "low_issues": 0,
        "score": 80, "coverage": {"coverage_status": "complete", "score_available": True},
        "model_name": "deepseek-v3", "duration_ms": 1000,
        "start_time": STORED, "end_time": STORED + timedelta(minutes=5), "create_time": STORED,
    }
    base.update(overrides)
    return base


def _issue(**overrides):
    base = {
        "id": 1, "task_id": 231, "file_id": 1, "file_name": "a.py", "line_number": 3, "end_line": 4,
        "issue_type": "安全漏洞", "severity": "高", "title": "SQL 注入", "description": "拼接 SQL",
        "suggestion": "参数化", "fixed_code": "", "status": "fixed", "handled_by": 7,
        "handled_at": STORED + timedelta(hours=1), "create_time": STORED,
        "update_time": STORED + timedelta(hours=2), "cwe": "CWE-89", "confidence": 0.9, "source": "llm",
        "references_json": [], "source_details": [],
    }
    base.update(overrides)
    return base


def _pdf_text(elements) -> str:
    return "\n".join(item.getPlainText() for item in elements if hasattr(item, "getPlainText")).replace("\xa0", " ")


def _word_text(task, issues) -> str:
    document = Document(io.BytesIO(export_to_word(task, issues, "摘要", 80)))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


# ---------------------------------------------------------------------------
# JSON：机器可读，UTC + Z
# ---------------------------------------------------------------------------

def test_json_export_tags_task_and_issue_times_as_utc():
    payload = json.loads(export_to_json(_task(), [_issue()], "摘要", 80))
    info = payload["task_info"]
    assert info["create_time"] == "2026-10-22T08:20:00Z"
    assert info["start_time"] == "2026-10-22T08:20:00Z"
    assert info["end_time"] == "2026-10-22T08:25:00Z"
    issue = payload["issues"][0]
    assert issue["create_time"] == "2026-10-22T08:20:00Z"
    assert issue["handled_at"] == "2026-10-22T09:20:00Z"
    assert issue["update_time"] == "2026-10-22T10:20:00Z"


@pytest.mark.parametrize("shape", INPUT_SHAPES)
def test_json_export_normalizes_every_historical_input_shape(shape):
    payload = json.loads(export_to_json(_task(create_time=shape), [_issue(update_time=shape)], "摘要", 80))
    assert payload["task_info"]["create_time"] == "2026-10-22T08:20:00Z"
    assert payload["issues"][0]["update_time"] == "2026-10-22T08:20:00Z"


def test_json_export_keeps_missing_times_empty():
    payload = json.loads(export_to_json(_task(end_time=None), [_issue(handled_at=None)], "摘要", 80))
    assert payload["task_info"]["end_time"] is None
    assert payload["issues"][0]["handled_at"] is None


# ---------------------------------------------------------------------------
# HTML / PDF / Word：人读，北京时间 + 标注
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template_type", ["simple", "detailed", "compliance"])
@pytest.mark.parametrize("shape", INPUT_SHAPES)
def test_html_export_shows_beijing_time_with_label_for_every_template_and_input_shape(template_type, shape):
    html = export_to_html(_task(create_time=shape), [], "摘要", 80, load_builtin_template(template_type))
    assert f"生成时间:{BEIJING_TEXT}" in html
    assert "2026-10-22T08:20:00" not in html
    assert "2026-10-22 08:20:00" not in html


@pytest.mark.parametrize("template_type", ["detailed", "compliance"])
def test_html_issue_handling_times_are_beijing_time(template_type):
    html = export_to_html(_task(), [_issue()], "摘要", 80, load_builtin_template(template_type))
    assert "处理时间: 2026-10-22 17:20:00（UTC+8）" in html
    assert "更新时间: 2026-10-22 18:20:00（UTC+8）" in html
    assert "2026-10-22T09:20:00" not in html


def test_html_issue_without_handling_time_keeps_dash_placeholder():
    html = export_to_html(
        _task(), [_issue(handled_at=None, status="unfixed")], "摘要", 80, load_builtin_template("detailed"),
    )
    assert "处理时间: - · 更新时间: 2026-10-22 18:20:00（UTC+8）" in html


def test_html_legacy_template_variable_task_also_gets_beijing_time():
    """008 迁移预置的旧模板用 task.create_time，不能因为别名绕过本地化。"""
    html = export_to_html(_task(), [], "摘要", 80, "<p>{{ task.create_time }}</p><p>{{ task_info.create_time }}</p>")
    assert html.count(BEIJING_TEXT) == 2


@pytest.mark.parametrize("shape", INPUT_SHAPES)
def test_pdf_header_and_issue_section_show_beijing_time(shape):
    context = export_to_dict(_task(create_time=shape), [_issue(handled_at=shape, update_time=shape)], "摘要", 80)
    font = _ensure_chinese_font()
    styles = _build_styles(font)
    header = _pdf_text(_build_header_elements(context["task_info"], 80, styles))
    assert f"生成时间:{BEIJING_TEXT}" in header
    section = _pdf_text(_build_issue_section(context["issues"][0], 1, styles, font))
    assert f"处理时间：{BEIJING_TEXT}" in section
    assert f"更新时间：{BEIJING_TEXT}" in section
    assert "2026-10-22T08:20:00" not in header + section


@pytest.mark.parametrize("shape", INPUT_SHAPES)
def test_word_export_shows_beijing_time(shape):
    text = _word_text(_task(create_time=shape), [_issue(handled_at=shape, update_time=shape)])
    assert f"生成时间: {BEIJING_TEXT}" in text
    assert f"处理时间: {BEIJING_TEXT}" in text
    assert f"更新时间: {BEIJING_TEXT}" in text
    assert "2026-10-22T08:20:00" not in text


def test_all_visual_formats_agree_with_the_json_instant():
    """同一任务：JSON 的 UTC 时刻换算到 UTC+8 后，等于 HTML/PDF/Word 里读者看到的文字。"""
    task, issues = _task(), [_issue()]
    wire = json.loads(export_to_json(task, issues, "摘要", 80))["task_info"]["create_time"]
    shown = (
        datetime.fromisoformat(wire.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    assert shown == "2026-10-22 16:20:00"
    html = export_to_html(task, issues, "摘要", 80, load_builtin_template("detailed"))
    context = export_to_dict(task, issues, "摘要", 80)
    pdf = _pdf_text(_build_header_elements(context["task_info"], 80, _build_styles(_ensure_chinese_font())))
    word = _word_text(task, issues)
    for rendered in (html, pdf, word):
        assert f"{shown}（UTC+8）" in rendered


# ---------------------------------------------------------------------------
# 领域报告 Markdown（渗透 / 远程评估 / 安全审计）
# ---------------------------------------------------------------------------

def test_pentest_markdown_authorization_window_is_beijing_time():
    markdown = build_pentest_report_markdown(
        {"status": "completed", "window_start": STORED, "window_end": STORED + timedelta(hours=4), "scope": {}},
        task_name="渗透", project_name="项目", execution_status="success", error_message=None, coverage={},
    )
    assert "授权窗口：2026-10-22 16:20:00（UTC+8） 至 2026-10-22 20:20:00（UTC+8）" in markdown
    assert "2026-10-22 08:20:00" not in markdown


def test_pentest_markdown_missing_window_stays_unrecorded():
    markdown = build_pentest_report_markdown(
        {"status": "completed", "scope": {}},
        task_name="渗透", project_name="项目", execution_status="success", error_message=None, coverage={},
    )
    assert "授权窗口：未记录 至 未记录" in markdown


def test_remote_markdown_authorization_window_accepts_stored_text_shapes():
    """远程评估报告快照里的时间是 json default=str 写下的空格分隔文本，也要按 UTC 解释。"""
    markdown = build_remote_pentest_report_markdown({
        "status": "completed",
        "scope": {"window_start": "2026-10-22 08:20:00", "window_end": "2026-10-22T12:20:00Z"},
        "methodology": {}, "phases": [], "findings": [],
    })
    assert "授权窗口：2026-10-22 16:20:00（UTC+8） 至 2026-10-22 20:20:00（UTC+8）" in markdown


def test_security_report_generation_time_is_beijing_time(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def utcnow(cls):
            return STORED

    monkeypatch.setattr(security_report_service, "datetime", FrozenDatetime)
    markdown = security_report_service._report_markdown(
        task_name="审计", project_name="项目", execution_status="success", summary="摘要", score=90,
        findings=[], coverage={"coverage_status": "complete"}, result={"file_count": 1}, error_message="",
    )
    assert f"报告生成时间：{BEIJING_TEXT}" in markdown
    assert "UTC\n" not in markdown


# ---------------------------------------------------------------------------
# 领域 JSON 导出：真实路由，渗透委托里的 datetime 带 Z
# ---------------------------------------------------------------------------

@pytest.fixture
def pentest_export():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    owner = User(username="time-owner", password="x", role="super_admin", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="时间口径项目", language="Python", status="active")
    db.add(project)
    db.flush()
    task = ReviewTask(
        user_id=owner.id, project_id=project.id, task_name="渗透任务", review_type="pentest", status="success",
        total_issues=0, score=0, create_time=STORED, summary="x",
    )
    db.add(task)
    db.flush()
    engagement = PentestEngagement(
        public_id=f"pt-time-{task.id}", user_id=owner.id, project_id=project.id, target_type="web",
        status="completed", report_task_id=task.id, authorized_at=STORED, window_start=STORED,
        window_end=STORED + timedelta(hours=4), started_at=STORED, completed_at=STORED + timedelta(hours=1),
        create_time=STORED,
    )
    db.add(engagement)
    db.flush()
    db.add(ReviewReport(
        task_id=task.id, user_id=owner.id, score=0, create_time=STORED,
        content_json={"source": "pentest", "engagement_public_id": engagement.public_id},
    ))
    db.commit()
    application = FastAPI()
    register_handlers(application)
    application.include_router(report_api.router, prefix="/api/reports")
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: owner
    try:
        with TestClient(application) as client:
            yield client, task
    finally:
        db.close()
        engine.dispose()


def test_domain_json_export_tags_nested_datetimes_as_utc(pentest_export):
    client, task = pentest_export
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["domain_data"]["window_start"] == "2026-10-22T08:20:00Z"
    assert body["domain_data"]["window_end"] == "2026-10-22T12:20:00Z"
    assert body["domain_data"]["create_time"] == "2026-10-22T08:20:00Z"
    assert body["task_info"]["create_time"] == "2026-10-22T08:20:00Z"


def test_domain_html_export_shows_beijing_authorization_window(pentest_export):
    client, task = pentest_export
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=html")
    assert response.status_code == 200, response.text
    assert "授权窗口：2026-10-22 16:20:00（UTC+8） 至 2026-10-22 20:20:00（UTC+8）" in response.text
