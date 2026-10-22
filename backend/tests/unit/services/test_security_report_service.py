from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.base import AgentResult
from app.agents.fullchain_audit_agent import FullChainAuditOrchestrator, ReconReport
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.ai.cvss import calculate_cvss_score
from app.api.v1 import security as security_api
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.core.exceptions import (
    NotFoundError,
)
from app.core.exceptions import (
    PermissionError as AppPermissionError,
)
from app.core.permission_codes import PermissionCode
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.schemas.security import (
    FullChainAuditIn,
    SecurityScanAllProjectsIn,
    SecurityScanFileIn,
)
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
            "summary": "发现一项需要修复的命令注入风险。安全评分 58/100。",
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
    assert task.status == "partial"
    assert task.review_type == "security_scan"
    assert task.coverage["verification_status"] == "partial"
    # 部分扫描按已扫描范围出分，并明确标注评分范围。
    assert task.coverage["score_available"] is True
    assert task.coverage["score_scope"] == "partial"
    assert task.coverage["unscanned"] == ["src/other.py: 语义分析未执行"]
    assert report.content_json["execution_status"] == "partial"
    assert report.content_json["score_available"] is True
    assert isinstance(report.content_json["risk_score"], int)
    assert report.content_json["findings"][0]["title"] == issue.title == "未授权命令执行"
    assert "验证覆盖" in report.content_json["report_md"]
    assert f"风险评分：{report.content_json['risk_score']}/100" in report.content_json["report_md"]
    assert "仅代表已扫描范围" in report.content_json["report_md"]
    # 来源自报的 58 分只作为核对值披露，不作为报告评分。
    assert "来源评分：58/100" in report.content_json["report_md"]
    assert "src/other.py" in report.content_json["report_md"]
    assert "*" not in report.content_json["report_md"]
    assert task_file.file_snapshot["file_path"] == "src/app.py"

    detail = report_service.get_report_detail(db, owner, task_id)
    assert detail["task"]["score"] == report.content_json["risk_score"]
    assert detail["task"]["score_scope"]["scope"] == "partial"
    assert detail["source"]["type"] == "security_scan"
    assert detail["source"]["security_report"]["threat_model"]["data_flows"]
    assert any(item["file_path"] == "src/app.py" for item in detail["files"])
    with pytest.raises(NotFoundError):
        report_service.get_report_detail(db, other, task_id)


def test_project_scan_conflict_preserves_409_and_does_not_persist_report(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'security_scan_conflict.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    owner = User(username="security_scan_conflict_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    role = db.query(Role).filter_by(code="user").one()
    permission = db.query(Permission).filter_by(code=PermissionCode.SECURITY_SCAN).one_or_none()
    if permission is None:
        permission = Permission(
            code=PermissionCode.SECURITY_SCAN,
            name=PermissionCode.SECURITY_SCAN,
            module="security",
            type="api",
        )
        db.add(permission)
        db.flush()
    db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    project = Project(user_id=owner.id, project_name="冲突审计项目", language="python", status="active")
    db.add(project)
    db.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    archive = ProjectSourceArchive(
        project_id=project.id,
        owner_id=owner.id,
        original_filename="source.zip",
        media_type="application/zip",
        archive_sha256="a" * 64,
        compressed_size=1,
        expanded_size=1,
        file_count=1,
        max_member_size=1,
        max_compression_ratio=1.0,
        storage_status="active",
        malware_status="clean",
        audit_status="running",
        audit_started_at=now,
        audit_heartbeat_at=now,
        scan_summary_json="{}",
        archive_blob=b"x",
    )
    db.add(archive)
    db.commit()

    agent = SecuritySentinelAgent()
    agent._db = db
    agent._user = owner
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=agent),
    )

    app = FastAPI()
    register_handlers(app)
    app.include_router(security_api.router, prefix="/api/security")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/security/scan-project",
                json={"project_id": project.id},
            )

        assert response.status_code == 409
        assert response.json()["code"] == 40902
        assert "已有白盒审计正在运行" in response.json()["message"]
        db.refresh(archive)
        assert archive.audit_status == "running"
        assert db.query(ReviewTask).count() == 0
        assert db.query(ReviewReport).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_fullchain_verification_failure_persists_completed_analysis_as_partial_report(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fullchain_verification_failure.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    owner = User(username="fullchain_partial_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    role = db.query(Role).filter_by(code="user").one()
    permission = db.query(Permission).filter_by(code=PermissionCode.SECURITY_SCAN).one_or_none()
    if permission is None:
        permission = Permission(
            code=PermissionCode.SECURITY_SCAN,
            name=PermissionCode.SECURITY_SCAN,
            module="security",
            type="api",
        )
        db.add(permission)
        db.flush()
    db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    project = Project(user_id=owner.id, project_name="全链验证中断项目", language="python", status="active")
    db.add(project)
    db.flush()
    code_file = CodeFile(
        project_id=project.id,
        file_name="app.py",
        file_path="src/app.py",
        language="python",
        size_bytes=24,
        line_count=1,
        version_no=1,
        content="run(user_input)",
        status="active",
    )
    db.add(code_file)
    db.commit()

    agent = SecuritySentinelAgent()
    agent._db = db
    agent._user = owner
    agent._authz_project = lambda *_args: None
    agent._execution_context = lambda *_args: None
    agent._require_current_execution = lambda *_args: None
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=agent),
    )
    finding = {
        "title": "命令执行入口未校验",
        "category": "安全漏洞",
        "severity": "高",
        "file_path": "src/app.py",
        "line_number": 1,
        "evidence": "run(user_input)",
        "exploit_scenario": "外部输入可能进入命令执行函数。",
        "fix_suggestion": "校验输入并使用安全参数化调用。",
        "cwe": "CWE-78",
        "owasp": "A03:2021-Injection",
    }
    monkeypatch.setattr(
        FullChainAuditOrchestrator,
        "_recon",
        lambda *_args: SimpleNamespace(surface=SimpleNamespace(file_profiles=[])),
    )
    monkeypatch.setattr(
        FullChainAuditOrchestrator,
        "_analysis",
        lambda *_args: AgentResult(
            success=True,
            data={
                "findings": [finding],
                "summary": "分析阶段发现命令执行风险。",
                "file_count": 1,
                "compliance": {"scan_complete": True, "coverage_status": "complete"},
            },
        ),
    )
    provider_secret = "provider=https://private.invalid key=do-not-expose"
    monkeypatch.setattr(
        FullChainAuditOrchestrator,
        "_verification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(provider_secret)),
    )

    app = FastAPI()
    register_handlers(app)
    app.include_router(security_api.router, prefix="/api/security")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/security/fullchain-audit",
                json={"project_id": project.id, "enable_sandbox": False},
            )

        assert response.status_code == 200
        assert response.json()["data"]["execution_status"] == "partial"
        assert provider_secret not in response.text
        task = db.query(ReviewTask).filter_by(project_id=project.id, review_type="fullchain_audit").one()
        report = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
        issue = db.query(ReviewIssue).filter_by(task_id=task.id).one()
        assert task.status == "partial"
        assert task.total_issues == 1
        assert task.coverage["verification_status"] == "partial"
        assert task.coverage["score_available"] is True
        assert task.coverage["score_scope"] == "partial"
        assert task.coverage["unscanned"]
        assert issue.title == "命令执行入口未校验"
        assert report.content_json["execution_status"] == "partial"
        assert report.content_json["findings"][0]["title"] == issue.title
        assert "覆盖范围：部分" in report.content_json["report_md"]
        assert "验证目标数：1" in report.content_json["report_md"]
        assert "独立验证完成数：0" in report.content_json["report_md"]
        assert "命令执行入口未校验" in report.content_json["report_md"]
        assert provider_secret not in report.content_json["report_md"]
    finally:
        db.close()
        engine.dispose()


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
    assert task.coverage["score_available"] is False
    assert report.content_json["score_available"] is False
    assert "完整源码审计范围未执行" in report.content_json["report_md"]

    detail = report_service.get_report_detail(db, owner, task_id)
    assert report_service.list_reports(db, owner)["items"] == []
    listed = report_service.list_reports(db, owner, include_history=True)["items"]
    item = next(row for row in listed if row["task_id"] == task_id)
    assert detail["task"]["score"] is None
    assert detail["stats"]["score"] is None
    assert item["score"] is None
    assert "未评估" in report.content_json["report_md"]
    assert report.score == 0


def test_scan_file_runtime_failure_persists_real_safe_report(db, monkeypatch):
    owner = User(username="security_scan_runtime_failure", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="运行异常报告", language="python", status="active")
    db.add(project)
    db.flush()
    code_file = CodeFile(
        project_id=project.id,
        file_name="app.py",
        file_path="src/app.py",
        language="python",
        size_bytes=8,
        line_count=1,
        version_no=1,
        content="value=1\n",
        status="active",
    )
    db.add(code_file)
    db.commit()
    provider_secret = "provider=https://private.invalid key=do-not-expose"

    def crash_scan(**_kwargs):
        raise RuntimeError(provider_secret)

    monkeypatch.setattr(security_api, "require_project_execution", lambda *_args: None)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(
            security_sentinel=SimpleNamespace(scan_file=crash_scan),
        ),
    )

    response = security_api.scan_file(
        SecurityScanFileIn(file_id=code_file.id),
        db=db,
        user=owner,
    )

    task_id = response.data.report_task_id
    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    assert task.status == "failed"
    assert task.coverage["verification_status"] == "failed"
    assert report.content_json["execution_status"] == "failed"
    assert report.content_json["coverage"]["verification_status"] == "failed"
    assert provider_secret not in task.error_message
    assert provider_secret not in task.summary
    assert provider_secret not in report.content_json["report_md"]
    assert "本次结果无法给出" in report.content_json["report_md"]


def test_scan_all_projects_runtime_failure_persists_per_project_report(db, monkeypatch):
    owner = User(username="security_scan_all_runtime_failure", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="全量扫描异常项目", language="python", status="active")
    db.add(project)
    db.commit()
    provider_secret = "provider=https://private.invalid key=do-not-expose"
    agent = SecuritySentinelAgent()
    agent.inject(db, user=owner)
    monkeypatch.setattr(agent, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_build_multi_agent_discussion", lambda *_args, **_kwargs: {})

    def crash_project_scan(*_args, **_kwargs):
        raise RuntimeError(provider_secret)

    monkeypatch.setattr(agent, "_scan_project_impl", crash_project_scan)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=agent),
    )

    response = security_api.scan_all_projects(
        SecurityScanAllProjectsIn(),
        db=db,
        user=owner,
    )

    assert len(response.data.report_task_ids) == 1
    task_id = response.data.report_task_ids[0]
    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    assert task.status == "failed"
    assert task.project_id == project.id
    assert task.coverage["verification_status"] == "failed"
    assert report.content_json["execution_status"] == "failed"
    assert provider_secret not in task.error_message
    assert provider_secret not in task.summary
    assert provider_secret not in report.content_json["report_md"]
    assert "本次结果不完整" in report.content_json["report_md"]


def test_fullchain_permission_revocation_during_model_call_does_not_persist_report(db, monkeypatch):
    owner = User(username="fullchain_permission_revoked", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="权限撤销项目", language="python", status="active")
    db.add(project)
    db.flush()
    db.add(CodeFile(
        project_id=project.id,
        file_name="app.py",
        file_path="src/app.py",
        language="python",
        size_bytes=8,
        line_count=1,
        version_no=1,
        content="value=1\n",
        status="active",
    ))
    db.commit()

    agent = SecuritySentinelAgent()
    agent.inject(db, user=owner)
    permission_check_stages = []
    current_stage = "pipeline"
    before_model_call_invoked = False
    model_request_sent = False
    denial = AppPermissionError("项目执行权已撤销")

    def revoke_on_model_call(_project_id, _actor=None):
        permission_check_stages.append(current_stage)
        if current_stage == "before_model_call":
            raise denial

    def call_model(_prompt, *, ctx=None, **_kwargs):
        nonlocal before_model_call_invoked, current_stage, model_request_sent
        callback = (ctx.extra or {}).get("before_model_call") if ctx else None
        if callable(callback):
            before_model_call_invoked = True
            current_stage = "before_model_call"
            callback()
        model_request_sent = True
        return AgentResult(success=True, data={"verdict": "plausible", "poc": "只读复核"})

    monkeypatch.setattr(agent, "_require_current_execution", revoke_on_model_call)
    monkeypatch.setattr(agent, "call_json", call_model)
    monkeypatch.setattr(security_api, "require_project_execution", lambda *_args: None)
    monkeypatch.setattr(
        security_api,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=agent),
    )
    monkeypatch.setattr(FullChainAuditOrchestrator, "_recon", lambda *_args: ReconReport())
    monkeypatch.setattr(
        FullChainAuditOrchestrator,
        "_analysis",
        lambda *_args: AgentResult(success=True, data={
            "findings": [{"severity": "高", "confidence": 0.9, "evidence": "evidence"}],
        }),
    )

    with pytest.raises(AppPermissionError) as exc_info:
        security_api.fullchain_audit(
            FullChainAuditIn(project_id=project.id),
            db=db,
            user=owner,
        )

    assert exc_info.value is denial
    assert permission_check_stages[-1] == "before_model_call"
    assert permission_check_stages.count("before_model_call") == 1
    assert before_model_call_invoked is True
    assert model_request_sent is False
    assert db.query(ReviewTask).count() == 0
    assert db.query(ReviewReport).count() == 0


def test_successful_security_scan_without_coverage_evidence_is_unknown():
    assert infer_coverage_status("success", {}) == "unknown"
    assert infer_coverage_status("success", {"file_count": 12}) == "unknown"
    assert infer_coverage_status(
        "success", {"scanned_file_count": 12, "total_file_count": 12}
    ) == "complete"
    assert infer_coverage_status(
        "success", {"coverage_status": "complete", "scanned_file_count": 12, "total_file_count": 13}
    ) == "partial"
    assert infer_coverage_status(
        "success", {"coverage_status": "complete", "verification_status": "partial"}
    ) == "partial"
    assert infer_coverage_status(
        "success", {"coverage_status": "unknown", "verification_status": "complete"}
    ) == "unknown"
    assert infer_coverage_status(
        "success", {"coverage_status": "complete", "verification_status": "failed"}
    ) == "failed"


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
        result={"summary": "扫描调用返回成功", "findings": [], "file_count": 12, "risk_score": 99},
        coverage={},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    assert task.coverage["verification_status"] == "unknown"
    assert task.coverage["unscanned"] == ["存在未完成或未能确认的扫描范围"]
    assert report.content_json["coverage"]["coverage_status"] == "unknown"
    # 已扫描 12 个文件但覆盖证据缺失：按已扫范围出分，不采用来源自报的 99 分。
    assert report.content_json["score_available"] is True
    assert report.content_json["risk_score"] == 100
    assert report.content_json["coverage"]["score_scope"] == "partial"
    assert "覆盖范围：未知" in report.content_json["report_md"]
    assert "风险评分：100/100" in report.content_json["report_md"]
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
    # 非有限的来源分被丢弃，改按已核验发现规则计分（1 个高危 = 92）。
    assert task.coverage["score_available"] is True
    assert report.content_json["risk_score"] == 92
    assert detail["task"]["score"] == 92
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


def test_security_report_with_ungraded_finding_does_not_publish_source_score(db):
    owner = User(username="security_report_ungraded", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="未分级问题", language="python", status="active")
    db.add(project)
    db.flush()

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="security_scan",
        task_name="包含未分级发现的完整扫描",
        execution_status="success",
        result={
            "summary": "扫描完整，但发现尚未完成分级。",
            "risk_score": 100,
            "findings": [{
                "title": "待分级的风险",
                "severity": "未分级",
                "file_path": "src/app.py",
                "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            }],
            "file_count": 1,
        },
        coverage={"coverage_status": "complete", "scanned_file_count": 1, "total_file_count": 1},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    assert task.coverage["score_available"] is False
    assert task.coverage["score_unavailable_reason"] == "ungraded_findings"
    assert task.score == 0
    assert task.score_version is None
    assert task.score_breakdown is None
    assert report.content_json["risk_score"] is None
    assert report.content_json["score_version"] is None
    assert report.content_json["score_breakdown"] is None
    assert report.content_json["findings"][0]["severity"] == "未分级"
    assert "风险评分：未评估" in report.content_json["report_md"]


def test_security_report_score_matches_final_issue_severity_basis(db):
    owner = User(username="security_report_score_basis", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="统一评分", language="python", status="active")
    db.add(project)
    db.flush()

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="security_scan",
        task_name="评分来源一致性",
        execution_status="success",
        result={
            "summary": "已完成扫描。安全评分 100/100。",
            "risk_score": 100,
            "findings": [{"title": "高风险发现", "severity": "高", "file_path": "src/app.py"}],
            "file_count": 1,
        },
        coverage={"coverage_status": "complete", "scanned_file_count": 1, "total_file_count": 1},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    detail = report_service.get_report_detail(db, owner, task_id)
    assert task.score == 92
    assert report.score == report.content_json["risk_score"] == detail["task"]["score"] == 92
    assert report.content_json["source_risk_score"] == 100
    assert "风险评分：92/100" in report.content_json["report_md"]
    assert "来源评分：100/100" in report.content_json["report_md"]
    assert "CVSS 基础分：未评分" in report.content_json["report_md"]
    assert "CVSS 3.1 基础向量：未评分" in report.content_json["report_md"]
    assert "安全评分 100/100" not in report.content_json["summary"]


def test_security_report_persists_vector_recomputed_composite_score(db):
    owner = User(username="security_report_cvss_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="CVSS落库一致性", language="python", status="active")
    db.add(project)
    db.flush()

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="security_scan",
        task_name="CVSS持久化核验",
        execution_status="success",
        result={
            "summary": "扫描完成。安全评分 100/100。",
            "risk_score": 100,
            "findings": [{
                "title": "命令注入",
                "severity": "高",
                "file_path": "src/app.py",
                "cvss_score": 0.1,
                "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            }],
            "file_count": 1,
        },
        coverage={"coverage_status": "complete", "scanned_file_count": 1, "total_file_count": 1},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    issue = db.query(ReviewIssue).filter_by(task_id=task_id).one()
    detail = report_service.get_report_detail(db, owner, task_id)
    assert task.score == report.score == report.content_json["risk_score"] == detail["task"]["score"] == 29
    assert task.score_version == task.score_breakdown["version"] == "cvss-weighted-v1"
    assert task.score_breakdown["policy_version"] == "cvss-weighted-v1"
    assert task.score == task.score_breakdown["score"]
    assert report.content_json["score_version"] == task.score_version
    assert report.content_json["score_breakdown"] == task.score_breakdown
    assert report.content_json["source_risk_score"] == 100
    assert issue.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert report.content_json["findings"][0]["source_cvss_vector"] == (
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    )
    assert issue.cvss_score == pytest.approx(9.8)
    assert issue.cvss_version == "3.1"
    assert issue.cvss_source == "vector"
    assert "CVSS 基础分：9.8/10" in report.content_json["report_md"]
    assert "CVSS 3.1 基础向量：AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H" in report.content_json["report_md"]


def test_security_report_persists_full_cvss_source_and_recomputable_base_vector(db):
    owner = User(username="security_report_long_cvss_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_security_report_view(db, owner)
    project = Project(user_id=owner.id, project_name="长 CVSS 向量落库", language="python", status="active")
    db.add(project)
    db.flush()
    full_vector = (
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/"
        "E:P/RL:O/RC:C/CR:H/IR:H/AR:H/"
        "MAV:N/MAC:L/MPR:N/MUI:N/MS:U/MC:H/MI:H/MA:H"
    )
    base_vector = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"

    task_id = persist_security_report(
        db,
        owner=owner,
        project_id=project.id,
        report_kind="security_scan",
        task_name="长向量基础分复算核验",
        execution_status="success",
        result={
            "summary": "安全扫描完成。",
            "risk_score": 100,
            "findings": [{
                "title": "命令注入",
                "severity": "高",
                "file_path": "src/app.py",
                "cvss_vector": full_vector,
            }],
            "file_count": 1,
        },
        coverage={"coverage_status": "complete", "scanned_file_count": 1, "total_file_count": 1},
    )

    task = db.get(ReviewTask, task_id)
    report = db.query(ReviewReport).filter_by(task_id=task_id, user_id=owner.id).one()
    issue = db.query(ReviewIssue).filter_by(task_id=task_id).one()
    finding = report.content_json["findings"][0]
    assert len(full_vector) > 64
    assert issue.cvss_vector == finding["cvss_vector"] == base_vector
    assert len(issue.cvss_vector) <= 64
    assert issue.cvss_score == pytest.approx(calculate_cvss_score(base_vector))
    assert finding["source_cvss_vector"] == full_vector
    assert issue.cvss_version == "3.1"
    assert issue.cvss_source == "vector"
    assert task.score == task.score_breakdown["score"]
    assert "CVSS 基础分：9.8/10" in report.content_json["report_md"]
    assert f"CVSS 3.1 基础向量：{base_vector}" in report.content_json["report_md"]
