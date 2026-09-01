"""T12 报告生成 API 集成测试

覆盖范围:
- 4 种格式(JSON/HTML/PDF/Word)报告生成成功
- 报告预览(HTML)成功
- 模板列表查询(全部 + 按类型筛选)
- 创建/更新/删除自定义模板
- 删除系统内置模板失败(返回 400)
- 不存在的 task_id 返回 404
- 无权限用户访问返回 403
- 导出报告(PDF/Word/JSON/HTML)成功
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.main import app
from app.models.report_template import ReportTemplate
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.report_exporter import load_builtin_template

# ============ 测试数据工厂 ============

def _make_admin_user() -> User:
    """创建管理员测试用户。

    Returns:
        User: role=admin 的用户 ORM 对象(未入库)。
    """
    return User(
        id=1,
        username="admin",
        password="x",
        email="admin@test.com",
        nickname="管理员",
        role="admin",
        status=1,
    )


def _make_plain_user() -> User:
    """创建普通测试用户(无权限)。

    Returns:
        User: role=user 的用户 ORM 对象(未入库)。
    """
    return User(
        id=2,
        username="plain",
        password="x",
        email="plain@test.com",
        nickname="普通用户",
        role="user",
        status=1,
    )


def _seed_builtin_templates(db_session) -> None:
    """播种 3 套内置报告模板到数据库。

    使用 T11 文件系统模板内容(正确的 task_info 上下文结构),
    确保 HTML 渲染测试能正确通过。

    Args:
        db_session: 测试数据库会话。
    """
    template_types = [
        ("simple", "简洁版", "简洁版报告,适合快速概览"),
        ("detailed", "详细版", "详细版报告(默认),适合技术团队"),
        ("compliance", "合规版", "合规版报告,按 ISO27001/GDPR/PCI-DSS/HIPAA 分章节"),
    ]
    for tpl_type, name, desc in template_types:
        content = load_builtin_template(tpl_type)
        template = ReportTemplate(
            name=name,
            type=tpl_type,
            content=content,
            is_builtin=1,
            creator_id=None,
            description=desc,
        )
        db_session.add(template)
    db_session.commit()


def _seed_review_task(db_session, user_id: int = 1) -> int:
    """播种审查任务与问题数据。

    创建一条 status=success 的审查任务和 3 条不同严重度的问题。

    Args:
        db_session: 测试数据库会话。
        user_id: 任务发起者用户 ID,默认 1(admin)。

    Returns:
        int: 创建的审查任务 ID。
    """
    task = ReviewTask(
        user_id=user_id,
        project_id=1,
        task_name="SQL注入审查",
        review_type="security",
        status="success",
        total_files=2,
        processed_files=2,
        total_issues=3,
        severe_issues=1,
        high_issues=1,
        medium_issues=1,
        low_issues=0,
        score=65,
        summary="发现多处安全漏洞,建议尽快修复。",
        model_name="deepseek-v3",
        duration_ms=3200,
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    # 创建 3 条问题(覆盖严重/高/中)
    issues_data = [
        {
            "task_id": task.id,
            "file_id": 100,
            "file_name": "app.py",
            "line_number": 42,
            "end_line": 45,
            "issue_type": "安全漏洞",
            "severity": "严重",
            "title": "SQL 注入漏洞",
            "description": "用户输入未经过滤直接拼接进 SQL 查询,可能导致数据泄露。",
            "suggestion": "使用参数化查询替代字符串拼接。",
            "fixed_code": "cursor.execute(\"SELECT * FROM users WHERE id=%s\", (user_id,))",
            "status": "unfixed",
            "cwe": "CWE-89",
            "owasp": "A03:2021-Injection",
            "confidence": 0.95,
            "source": "llm",
            "cvss_score": 9.1,
            "confirmation_count": 2,
            "aggregation_version": "finding-aggregation-v1",
            "evidence_quality": "direct",
            "conflict_status": "unresolved",
            "human_review_status": "pending",
            "risk_score": 91.0,
            "aggregation_json": {"claims": [{"claim_id": "report-claim-1"}]},
        },
        {
            "task_id": task.id,
            "file_id": 100,
            "file_name": "app.py",
            "line_number": 78,
            "issue_type": "安全漏洞",
            "severity": "高",
            "title": "XSS 跨站脚本攻击",
            "description": "用户输入未转义直接输出到 HTML,可能导致 XSS 攻击。",
            "suggestion": "对用户输入进行 HTML 转义后再输出。",
            "fixed_code": "return escape(user_input)",
            "status": "unfixed",
            "cwe": "CWE-79",
            "confidence": 0.9,
            "source": "llm",
            "cvss_score": 7.2,
        },
        {
            "task_id": task.id,
            "file_id": 101,
            "file_name": "utils.py",
            "line_number": 15,
            "issue_type": "潜在Bug",
            "severity": "中",
            "title": "未处理的异常",
            "description": "除法运算未处理除数为零的情况,可能抛出 ZeroDivisionError。",
            "suggestion": "添加除数为零的检查。",
            "fixed_code": "if b == 0:\n    return 0\nreturn a / b",
            "status": "unfixed",
            "cwe": "CWE-369",
            "confidence": 0.8,
            "source": "llm",
            "cvss_score": 5.5,
        },
    ]
    for data in issues_data:
        issue = ReviewIssue(**data)
        db_session.add(issue)
    db_session.commit()
    return task.id


# ============ 测试 Fixture ============

@pytest.fixture
def admin_client():
    """创建管理员 API 测试客户端(共享内存 SQLite)。

    覆盖 get_db 与 get_current_user,使所有请求以管理员身份访问测试数据库。
    数据库预置 3 套内置模板 + 1 条审查任务 + 3 条问题。

    Yields:
        tuple[TestClient, Session]: 测试客户端和数据库会话。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()

    # 播种测试数据
    admin = _make_admin_user()
    session.add(admin)
    session.commit()
    _seed_builtin_templates(session)
    task_id = _seed_review_task(session, user_id=1)

    def override_db():
        """覆盖数据库依赖。"""
        yield session

    def override_user():
        """覆盖当前用户依赖,返回管理员。"""
        return admin

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    try:
        yield TestClient(app), session, task_id
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        session.close()
        engine.dispose()


@pytest.fixture
def plain_client():
    """创建普通用户(无权限)API 测试客户端。

    用于测试权限拒绝场景(403)。

    Yields:
        tuple[TestClient, Session]: 测试客户端和数据库会话。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()

    admin = _make_admin_user()
    plain = _make_plain_user()
    session.add(admin)
    session.add(plain)
    session.commit()
    _seed_builtin_templates(session)
    task_id = _seed_review_task(session, user_id=1)

    def override_db():
        """覆盖数据库依赖。"""
        yield session

    def override_user():
        """覆盖当前用户依赖,返回普通用户(无权限)。"""
        return plain

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    try:
        yield TestClient(app), session, task_id
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        session.close()
        engine.dispose()


# ============ 报告生成测试(4 种格式) ============

def test_generate_json_report_success(admin_client):
    """JSON 格式报告生成成功,返回可解析的 JSON。"""
    client, db, task_id = admin_client
    response = client.post("/api/reports/generate", json={
        "task_id": task_id,
        "format": "json",
    })
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert "task_info" in payload
    assert "issues" in payload
    assert "statistics" in payload
    assert payload["statistics"]["total_issues"] == 3
    assert payload["statistics"]["aggregation_summary"] == {
        "aggregated": 1,
        "independently_confirmed": 1,
        "pending_human_review": 1,
        "unresolved_conflicts": 1,
        "insufficient_evidence": 0,
    }
    assert payload["issues"][0]["aggregation_json"]["claims"][0]["claim_id"] == "report-claim-1"


def test_generate_html_report_success(admin_client):
    """HTML 格式报告生成成功,返回包含关键内容的 HTML。"""
    client, db, task_id = admin_client
    response = client.post("/api/reports/generate", json={
        "task_id": task_id,
        "format": "html",
        "template_type": "detailed",
    })
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "<!DOCTYPE html>" in html
    assert "SQL注入审查" in html or "SQL 注入漏洞" in html
    assert "finding-aggregation-v1" in html
    assert "人工复核 pending" in html


def test_generate_pdf_report_success(admin_client):
    """PDF 格式报告生成成功,返回 PDF 字节流。"""
    client, db, task_id = admin_client
    response = client.post("/api/reports/generate", json={
        "task_id": task_id,
        "format": "pdf",
    })
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert len(response.content) > 0
    # PDF 文件以 %PDF 开头
    assert response.content[:4] == b"%PDF"


def test_generate_word_report_success(admin_client):
    """Word 格式报告生成成功,返回 Word 字节流。"""
    client, db, task_id = admin_client
    response = client.post("/api/reports/generate", json={
        "task_id": task_id,
        "format": "word",
    })
    assert response.status_code == 200
    assert "wordprocessingml" in response.headers["content-type"]
    assert len(response.content) > 0
    # .docx 是 ZIP 文件,以 PK 开头
    assert response.content[:2] == b"PK"


@pytest.mark.parametrize(
    ("report_format", "permission"),
    [
        ("json", PermissionCode.REPORT_EXPORT_JSON),
        ("html", PermissionCode.REPORT_EXPORT_HTML),
        ("pdf", PermissionCode.REPORT_EXPORT_PDF),
        ("word", PermissionCode.REPORT_EXPORT_WORD),
    ],
)
def test_generate_report_checks_format_specific_permission(
    admin_client,
    monkeypatch,
    report_format,
    permission,
):
    """POST generate 不能用 report:view 代替具体格式的导出权限。"""
    client, _db, task_id = admin_client
    checked = []

    def fake_check_permission(_db, user_id, requested_permission):
        checked.append((user_id, requested_permission))
        return requested_permission == permission

    monkeypatch.setattr("app.api.v1.reports.check_permission", fake_check_permission)

    response = client.post(
        "/api/reports/generate",
        json={"task_id": task_id, "format": report_format},
    )

    assert response.status_code == 200
    assert checked == [(1, permission)]


@pytest.mark.parametrize(
    ("report_format", "permission"),
    [
        ("json", PermissionCode.REPORT_EXPORT_JSON),
        ("html", PermissionCode.REPORT_EXPORT_HTML),
    ],
)
def test_generate_json_and_html_reject_without_matching_export_permission(
    admin_client,
    monkeypatch,
    report_format,
    permission,
):
    """JSON 与 HTML 也必须分别拒绝缺少对应权限的生成请求。"""
    client, _db, task_id = admin_client
    monkeypatch.setattr("app.api.v1.reports.check_permission", lambda *_args: False)

    response = client.post(
        "/api/reports/generate",
        json={"task_id": task_id, "format": report_format},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["required_permission"] == permission


# ============ 报告预览测试 ============

def test_preview_report_html_success(admin_client):
    """报告预览(HTML)成功,返回可嵌入 iframe 的 HTML。"""
    client, db, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}", params={
        "template_type": "simple",
    })
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "<!DOCTYPE html>" in html
    assert "SQL注入审查" in html or "SQL 注入漏洞" in html


def test_preview_report_with_detailed_template(admin_client):
    """使用 detailed 模板预览报告成功。"""
    client, db, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}", params={
        "template_type": "detailed",
    })
    assert response.status_code == 200
    assert "SQL 注入漏洞" in response.text


def test_preview_report_with_compliance_template(admin_client):
    """使用 compliance 模板预览报告成功。"""
    client, db, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}", params={
        "template_type": "compliance",
    })
    assert response.status_code == 200
    html = response.text
    assert "ISO 27001" in html or "ISO27001" in html


# ============ 模板管理测试 ============

def test_list_templates_success(admin_client):
    """模板列表查询成功,返回 3 套内置模板。"""
    client, db, task_id = admin_client
    response = client.get("/api/reports/templates")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    templates = body["data"]
    assert len(templates) == 3
    types = {t["type"] for t in templates}
    assert types == {"simple", "detailed", "compliance"}


def test_list_templates_filter_by_type(admin_client):
    """按类型筛选模板成功,只返回匹配类型的模板。"""
    client, db, task_id = admin_client
    response = client.get("/api/reports/templates", params={
        "template_type": "simple",
    })
    assert response.status_code == 200
    templates = response.json()["data"]
    assert len(templates) == 1
    assert templates[0]["type"] == "simple"


def test_create_custom_template_success(admin_client):
    """创建自定义模板成功。"""
    client, db, task_id = admin_client
    response = client.post("/api/reports/templates", json={
        "name": "我的自定义模板",
        "type": "custom",
        "content": "<html>{{ task_info.task_name }}</html>",
        "description": "测试自定义模板",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    template = body["data"]
    assert template["name"] == "我的自定义模板"
    assert template["type"] == "custom"
    assert template["is_builtin"] == 0
    assert template["id"] is not None


def test_update_template_success(admin_client):
    """更新模板成功。"""
    client, db, task_id = admin_client
    # 先创建一个自定义模板
    create_resp = client.post("/api/reports/templates", json={
        "name": "待更新模板",
        "type": "custom",
        "content": "<html>old</html>",
    })
    template_id = create_resp.json()["data"]["id"]

    # 更新模板
    response = client.put(f"/api/reports/templates/{template_id}", json={
        "name": "已更新模板",
        "content": "<html>new</html>",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["name"] == "已更新模板"
    assert body["data"]["content"] == "<html>new</html>"


def test_delete_custom_template_success(admin_client):
    """删除自定义模板成功。"""
    client, db, task_id = admin_client
    # 先创建一个自定义模板
    create_resp = client.post("/api/reports/templates", json={
        "name": "待删除模板",
        "type": "custom",
        "content": "<html>temp</html>",
    })
    template_id = create_resp.json()["data"]["id"]

    # 删除模板
    response = client.delete(f"/api/reports/templates/{template_id}")
    assert response.status_code == 200
    assert response.json()["code"] == 0

    # 确认模板已被删除
    list_resp = client.get("/api/reports/templates")
    template_ids = [t["id"] for t in list_resp.json()["data"]]
    assert template_id not in template_ids


def test_delete_builtin_template_returns_400(admin_client):
    """删除系统内置模板失败,返回 400。"""
    client, db, task_id = admin_client
    # 获取内置模板 ID(simple 类型)
    list_resp = client.get("/api/reports/templates", params={"template_type": "simple"})
    builtin_id = list_resp.json()["data"][0]["id"]

    # 尝试删除内置模板
    response = client.delete(f"/api/reports/templates/{builtin_id}")
    assert response.status_code == 400
    body = response.json()
    assert "不可删除" in body["message"] or "内置" in body["message"]


# ============ 异常场景测试 ============

def test_generate_nonexistent_task_returns_404(admin_client):
    """不存在的 task_id 返回 404。"""
    client, db, task_id = admin_client
    response = client.post("/api/reports/generate", json={
        "task_id": 99999,
        "format": "json",
    })
    assert response.status_code == 404


def test_preview_nonexistent_task_returns_404(admin_client):
    """预览不存在的 task_id 返回 404。"""
    client, db, task_id = admin_client
    response = client.get("/api/reports/tasks/99999")
    assert response.status_code == 404


def test_generate_unauthorized_user_returns_403(plain_client):
    """无权限用户访问报告生成返回 403。"""
    client, db, task_id = plain_client
    response = client.post("/api/reports/generate", json={
        "task_id": task_id,
        "format": "json",
    })
    assert response.status_code == 403


def test_preview_unauthorized_user_returns_403(plain_client):
    """无权限用户访问报告预览返回 403。"""
    client, db, task_id = plain_client
    response = client.get(f"/api/reports/tasks/{task_id}")
    assert response.status_code == 403


def test_list_templates_unauthorized_returns_403(plain_client):
    """无权限用户访问模板列表返回 403。"""
    client, db, task_id = plain_client
    response = client.get("/api/reports/templates")
    assert response.status_code == 403


# ============ 导出报告测试(4 种格式) ============

def test_export_report_pdf_success(admin_client):
    """导出 PDF 报告成功,返回带下载头的 PDF 文件。"""
    client, db, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}/export", params={
        "format": "pdf",
    })
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers.get("content-disposition", "")
    assert response.content[:4] == b"%PDF"


def test_export_report_word_success(admin_client):
    """导出 Word 报告成功,返回带下载头的 Word 文件。"""
    client, db, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}/export", params={
        "format": "word",
    })
    assert response.status_code == 200
    assert "wordprocessingml" in response.headers["content-type"]
    assert "attachment" in response.headers.get("content-disposition", "")
    assert response.content[:2] == b"PK"


def test_export_report_json_success(admin_client):
    """导出 JSON 报告成功,返回带下载头的 JSON 文件。"""
    client, db, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}/export", params={
        "format": "json",
    })
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    payload = response.json()
    assert "task_info" in payload
    assert "statistics" in payload


def test_export_report_html_success(admin_client):
    """导出 HTML 报告成功,返回带下载头的 HTML 文件。"""
    client, db, task_id = admin_client
    response = client.get(f"/api/reports/tasks/{task_id}/export", params={
        "format": "html",
    })
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    assert "<!DOCTYPE html>" in response.text


def test_plain_owner_html_export_uses_format_specific_permission(
    plain_client, monkeypatch,
):
    """普通任务所有者有 HTML 权限时可导出，且不能借此导出 PDF。"""
    client, db, task_id = plain_client
    task = db.get(ReviewTask, task_id)
    task.user_id = 2
    db.commit()
    checked = []

    def fake_check_permission(_db, user_id, permission):
        checked.append((user_id, permission))
        return permission == PermissionCode.REPORT_EXPORT_HTML

    monkeypatch.setattr("app.api.v1.reports.check_permission", fake_check_permission)

    html_response = client.get(
        f"/api/reports/tasks/{task_id}/export",
        params={"format": "html"},
    )
    pdf_response = client.get(
        f"/api/reports/tasks/{task_id}/export",
        params={"format": "pdf"},
    )

    assert html_response.status_code == 200
    assert "attachment" in html_response.headers.get("content-disposition", "")
    assert "<!DOCTYPE html>" in html_response.text
    assert pdf_response.status_code == 403
    assert checked == [
        (2, PermissionCode.REPORT_EXPORT_HTML),
        (2, PermissionCode.REPORT_EXPORT_PDF),
    ]


def test_legacy_word_route_delegates_to_new_exporter_and_checks_permission(
    admin_client,
    monkeypatch,
):
    """旧 Word 地址只保留兼容路由，事实与内容交给新导出器。"""
    client, _db, task_id = admin_client
    checked = []
    captured = {}

    def fake_check_permission(_db, user_id, permission):
        checked.append((user_id, permission))
        return permission == PermissionCode.REPORT_EXPORT_WORD

    def fake_export(task, issues, summary, score, template_type, evidence):
        captured.update(task=task, issues=issues, summary=summary, score=score)
        return b"NEW_WORD_EXPORTER"

    monkeypatch.setattr("app.api.v1.reports.check_permission", fake_check_permission)
    monkeypatch.setattr("app.api.v1.reports.export_to_word", fake_export)

    response = client.get(f"/api/reports/{task_id}/export/word")

    assert response.status_code == 200
    assert response.content == b"NEW_WORD_EXPORTER"
    assert len(captured["issues"]) == 3
    assert checked == [(1, PermissionCode.REPORT_EXPORT_WORD)]


def test_legacy_pdf_route_delegates_to_new_exporter_and_checks_permission(
    admin_client,
    monkeypatch,
):
    """旧 PDF 地址使用新导出器，并独立要求 PDF 权限。"""
    client, _db, task_id = admin_client
    checked = []

    def fake_check_permission(_db, user_id, permission):
        checked.append((user_id, permission))
        return permission == PermissionCode.REPORT_EXPORT_PDF

    monkeypatch.setattr("app.api.v1.reports.check_permission", fake_check_permission)
    monkeypatch.setattr(
        "app.api.v1.reports.export_to_pdf",
        lambda *_args: b"NEW_PDF_EXPORTER",
    )

    response = client.get(f"/api/reports/{task_id}/export/pdf")

    assert response.status_code == 200
    assert response.content == b"NEW_PDF_EXPORTER"
    assert checked == [(1, PermissionCode.REPORT_EXPORT_PDF)]


# ============ 补充测试 ============

def test_generate_html_with_simple_template(admin_client):
    """使用 simple 模板生成 HTML 报告成功。"""
    client, db, task_id = admin_client
    response = client.post("/api/reports/generate", json={
        "task_id": task_id,
        "format": "html",
        "template_type": "simple",
    })
    assert response.status_code == 200
    assert "SQL注入审查" in response.text or "SQL 注入漏洞" in response.text


def test_export_html_falls_back_from_legacy_seeded_markdown(admin_client):
    """008 迁移遗留的 Markdown 内置模板不应导致 HTML 导出 500。"""

    client, db, task_id = admin_client
    template = (
        db.query(ReportTemplate)
        .filter(ReportTemplate.type == "simple", ReportTemplate.is_builtin == 1)
        .one()
    )
    template.content = (
        "# 代码审查报告 · {{ task.task_name }}\n\n"
        "- **项目**: {{ task.project_name }}\n"
        "- **审查类型**: {{ task.review_type }}\n"
        "- **执行时间**: {{ task.start_time }} ~ {{ task.end_time }}\n"
        "- **风险评分**: {{ score }}/100\n\n"
        "## 总览\n\n{{ summary }}\n\n"
        "| 严重度 | 数量 |\n"
        "|--------|------|\n"
        "| 严重 | {{ metrics.severity_counts.critical }} |\n"
        "| 高 | {{ metrics.severity_counts.high }} |\n"
        "| 中 | {{ metrics.severity_counts.medium }} |\n"
        "| 低 | {{ metrics.severity_counts.low }} |\n"
        "| 信息 | {{ metrics.severity_counts.info }} |\n"
    )
    db.commit()

    response = client.get(
        f"/api/reports/tasks/{task_id}/export",
        params={"format": "html", "template_type": "simple"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" in response.headers["content-disposition"]
    assert "<!DOCTYPE html>" in response.text
    assert "SQL注入审查" in response.text


def test_export_html_preserves_admin_customization_with_legacy_title(admin_client):
    """保留旧标题的管理员定制模板不能被误判为 008 原始模板。"""

    client, db, task_id = admin_client
    template = (
        db.query(ReportTemplate)
        .filter(ReportTemplate.type == "detailed", ReportTemplate.is_builtin == 1)
        .one()
    )
    template.content = (
        "# 代码审查报告 · {{ task.task_name }}\n\n"
        "<!DOCTYPE html><html><body>管理员定制：{{ task.task_name }}</body></html>"
    )
    db.commit()

    response = client.get(
        f"/api/reports/tasks/{task_id}/export",
        params={"format": "html", "template_type": "detailed"},
    )

    assert response.status_code == 200
    assert "管理员定制：SQL注入审查" in response.text


def test_update_builtin_template_content_allowed(admin_client):
    """内置模板可以更新内容(但 is_builtin 不可变)。"""
    client, db, task_id = admin_client
    # 获取内置模板 ID
    list_resp = client.get("/api/reports/templates", params={"template_type": "detailed"})
    builtin_id = list_resp.json()["data"][0]["id"]

    # 更新内置模板描述
    response = client.put(f"/api/reports/templates/{builtin_id}", json={
        "description": "更新后的描述",
    })
    assert response.status_code == 200
    assert response.json()["data"]["description"] == "更新后的描述"
    # is_builtin 不变
    assert response.json()["data"]["is_builtin"] == 1
