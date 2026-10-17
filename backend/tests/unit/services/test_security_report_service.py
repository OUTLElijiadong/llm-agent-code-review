import pytest

from app.core.exceptions import NotFoundError
from app.core.permission_codes import PermissionCode
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.services import report_service
from app.services.security_report_service import infer_coverage_status, persist_security_report


def _grant_security_report_view(db, user):
    role = db.query(Role).filter_by(code="user").one_or_none()
    if role is None:
        role = Role(name="普通用户", code="user", status="active", is_builtin=1)
        db.add(role)
        db.flush()
    if db.query(UserRole).filter_by(user_id=user.id, role_id=role.id).count() == 0:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    for code in (PermissionCode.REPORT_VIEW, PermissionCode.SECURITY_VIEW):
        permission = db.query(Permission).filter_by(code=code).one_or_none()
        if permission is None:
            permission = Permission(code=code, name=code, module="report", type="api")
            db.add(permission)
            db.flush()
        if db.query(RolePermission).filter_by(role_id=role.id, permission_id=permission.id).count() == 0:
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()


def test_security_scan_persists_readable_owner_scoped_report_and_partial_coverage(db):
    owner = User(username="security_report_owner", password="x", role="user", status=1)
    other = User(username="security_report_other", password="x", role="user", status=1)
    db.add_all([owner, other])
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="报告闭环样例", language="python", status="active")
    db.add(project)
    db.flush()
    code_file = CodeFile(
        project_id=project.id, file_name="app.py", file_path="src/app.py", language="python",
        size_bytes=30, line_count=2, version_no=1, content="query = input()\nrun(query)", status="active",
    )
    db.add(code_file)
    db.flush()

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="security_scan",
        task_name="项目威胁建模",
        execution_status="partial",
        result={
            "findings": [{
                "title": "未授权命令执行",
                "category": "安全漏洞",
                "severity": "高",
                "file_path": "src/app.py",
                "line_number": 2,
                "evidence": "run(query)",
                "exploit_scenario": "外部输入未经校验进入命令执行入口。",
                "fix_suggestion": "使用参数化调用并校验输入。",
                "cwe": "CWE-78",
                "owasp": "A03:2021-Injection",
            }],
            "risk_score": 58,
            "summary": "发现一项需要修复的命令注入风险。",
            "file_count": 1,
            "duration_ms": 1200,
            "compliance": {"scan_complete": False, "total_file_count": 2, "scanned_file_count": 1},
            "threat_model": {"data_flows": [{"from": "src/app.py:1", "to": "src/app.py:2"}]},
        },
        error_message="语义分析预算耗尽，未完成剩余文件",
        coverage={"coverage_status": "partial", "unscanned": ["src/other.py: 语义分析未执行"]},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    issue = db.query(ReviewIssue).filter_by(task_id=task_id).one()
    task_file = db.query(ReviewTaskFile).filter_by(task_id=task_id, file_id=code_file.id).one()
    assert task.status == "success"
    assert task.review_type == "security_scan"
    assert task.coverage["verification_status"] == "partial"
    assert task.coverage["unscanned"] == ["src/other.py: 语义分析未执行"]
    assert report.content_json["execution_status"] == "partial"
    assert report.content_json["findings"][0]["title"] == issue.title == "未授权命令执行"
    assert "验证覆盖" in report.content_json["report_md"]
    assert "src/other.py" in report.content_json["report_md"]
    assert "*" not in report.content_json["report_md"]
    assert task_file.file_snapshot["file_path"] == "src/app.py"

    detail = report_service.get_report_detail(db, owner, task_id)
    assert detail["source"]["type"] == "security_scan"
    assert detail["source"]["security_report"]["threat_model"]["data_flows"]
    assert any(item["file_path"] == "src/app.py" for item in detail["files"])
    with pytest.raises(NotFoundError):
        report_service.get_report_detail(db, other, task_id)


def test_failed_security_scan_still_persists_report_with_unknown_coverage(db):
    owner = User(username="security_report_failed", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="失败报告", language="python", status="active")
    db.add(project)
    db.flush()

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="fullchain_audit",
        task_name="全链路审计",
        execution_status="failed",
        result={"summary": "侦察阶段失败", "findings": [], "risk_score": None, "file_count": 0},
        error_message="模型服务连接失败",
        coverage={"coverage_status": "failed", "unscanned": ["完整源码审计范围未执行"]},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    assert task.status == "failed"
    assert task.error_message == "模型服务连接失败"
    assert report.content_json["coverage"]["verification_status"] == "failed"
    assert "完整源码审计范围未执行" in report.content_json["report_md"]

    detail = report_service.get_report_detail(db, owner, task_id)
    listed = report_service.list_reports(db, owner)["items"]
    item = next(row for row in listed if row["task_id"] == task_id)
    assert detail["task"]["score"] is None
    assert detail["stats"]["score"] is None
    assert item["score"] is None
    assert "未评估" in report.content_json["report_md"]
    assert report.score == 0


def test_successful_security_scan_without_coverage_evidence_is_unknown():
    assert infer_coverage_status("success", {}) == "unknown"
    assert infer_coverage_status("success", {"file_count": 12}) == "unknown"
    assert infer_coverage_status(
        "success", {"scanned_file_count": 12, "total_file_count": 12}
    ) == "complete"
    assert infer_coverage_status(
        "success", {"coverage_status": "complete", "scanned_file_count": 12, "total_file_count": 13}
    ) == "partial"


def test_successful_security_scan_without_coverage_evidence_reports_unknown_scope(db):
    owner = User(username="security_report_unknown_scope", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="未知覆盖范围", language="python", status="active")
    db.add(project)
    db.flush()

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="security_scan",
        task_name="缺少覆盖证据的扫描",
        execution_status="success",
        result={"summary": "扫描调用返回成功", "findings": [], "file_count": 12},
        coverage={},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    assert task.coverage["verification_status"] == "unknown"
    assert task.coverage["unscanned"] == ["存在未完成或未能确认的扫描范围"]
    assert report.content_json["coverage"]["coverage_status"] == "unknown"
    assert "覆盖范围：未知" in report.content_json["report_md"]
    assert "存在未完成或未能确认的扫描范围" in report.content_json["report_md"]


def test_security_report_removes_ai_markdown_and_rejects_non_finite_scores(db):
    owner = User(username="security_report_format", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="格式检查", language="python", status="active")
    db.add(project)
    db.flush()

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="security_scan",
        task_name="**项目安全审计**",
        execution_status="success",
        result={
            "summary": "## **审计结论**\n*发现一个需要修复的问题。*",
            "risk_score": float("nan"),
            "findings": [{
                "title": "**未授权访问**",
                "severity": "高",
                "file_path": "**src/auth.py**",
                "exploit_scenario": "*攻击者可访问受限接口。*",
                "evidence": "**if is_admin: allow()**",
                "fix_suggestion": "**增加服务端权限校验**",
                "verification": "**待复核**",
                "cwe": "**CWE-284**",
                "owasp": "**A01:2025**",
            }],
            "file_count": 1,
            "threat_model": {
                "data_flows": [{"from": "**src/auth.py:1**", "to": "**src/admin.py:8**", "risk_type": "访问控制"}],
            },
        },
        coverage={"unscanned": ["* 未扫描的测试目录"]},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    issue = db.query(ReviewIssue).filter_by(task_id=task_id).one()
    detail = report_service.get_report_detail(db, owner, task_id)
    assert task.coverage["score_available"] is False
    assert report.content_json["risk_score"] is None
    assert detail["task"]["score"] is None
    assert "*" not in report.content_json["report_md"]
    assert "审计结论" in report.content_json["report_md"]
    assert "CWE：CWE-284" in report.content_json["report_md"]
    assert "OWASP：A01:2025" in report.content_json["report_md"]
    assert "位置：src/auth.py" in report.content_json["report_md"]
    assert "证据：if is_admin: allow()" in report.content_json["report_md"]
    assert "复核状态：待复核" in report.content_json["report_md"]
    assert "src/auth.py:1" in report.content_json["report_md"]
    assert "src/admin.py:8" in report.content_json["report_md"]
    assert "*" not in report.content_json["findings"][0]["title"]
    assert "*" not in report.content_json["findings"][0]["file_path"]
    assert "未授权访问" in issue.title
    assert "攻击者可访问受限接口。" == issue.description
