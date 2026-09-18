"""工作台进度、权限撤回、列表文件进度以及自然日趋势回归。"""

from datetime import datetime, timedelta, timezone

from app.models.agent_response_run import AgentResponseRun
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.review import TaskOut
from app.services import dashboard_service, review_service


def seed(db):
    owner = User(username="progress-owner", password="x", role="user", status=1)
    other = User(username="progress-other", password="x", role="user", status=1)
    db.add_all([owner, other])
    db.flush()
    project = Project(user_id=owner.id, project_name="本人项目", status="active")
    foreign = Project(user_id=other.id, project_name="他人项目", status="active")
    db.add_all([project, foreign])
    db.flush()
    return owner, other, project, foreign


def task(db, owner, project, status="running", **kwargs):
    row = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name="进度任务",
        review_type="standard",
        status=status,
        total_files=5,
        processed_files=2,
        create_time=datetime.now(timezone.utc),
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def test_list_task_serializes_actual_processed_files(db):
    owner, _, project, _ = seed(db)
    row = task(db, owner, project)
    item = review_service.list_tasks(db, owner)["items"][0]
    assert item["processed_files"] == 2
    assert TaskOut(**item).model_dump()["processed_files"] == row.processed_files


def test_running_owner_scope_and_deleted_projects(db, monkeypatch):
    owner, other, project, foreign = seed(db)
    visible = task(db, owner, project)
    task(db, other, foreign)
    deleted = Project(user_id=owner.id, project_name="已删除", status="deleted")
    db.add(deleted)
    db.flush()
    task(db, owner, deleted)
    for user, run in [(owner, "own-run"), (other, "foreign-run")]:
        db.add(
            AgentResponseRun(
                user_id=user.id,
                run_id=run,
                surface="user",
                session_key="session",
                status="running",
                checkpoint_json="{}",
            )
        )
    # 同一账号历史上可能残留管理面运行记录；成员工作台不能把它显示成贾维斯会话。
    db.add(
        AgentResponseRun(
            user_id=owner.id,
            run_id="own-admin-run",
            surface="admin",
            session_key="admin-session",
            status="running",
            checkpoint_json="{}",
        )
    )
    db.flush()
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *args: True)
    result = dashboard_service.get_running(db, owner)
    assert [r["id"] for r in result["reviews"]] == [visible.id]
    assert [r["run_id"] for r in result["agents"]] == ["own-run"]
    assert result["reviews"][0]["processed_files"] == 2


def test_running_no_capabilities_does_not_reveal_task_details(db, monkeypatch):
    owner, _, project, _ = seed(db)
    task(db, owner, project)
    db.add(
        AgentResponseRun(
            user_id=owner.id,
            run_id="own-run",
            surface="user",
            session_key="session",
            status="running",
            checkpoint_json="{}",
        )
    )
    db.flush()
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *args: False)
    assert dashboard_service.get_running(db, owner) == {"reviews": [], "agents": []}


def test_membership_revoked_immediately_even_when_statistics_cached(db, monkeypatch):
    owner, member, project, _ = seed(db)
    membership = ProjectMember(project_id=project.id, user_id=member.id, role_in_project="reviewer")
    db.add(membership)
    row = task(db, owner, project, status="success")
    db.add(
        ReviewIssue(
            task_id=row.id,
            file_id=None,
            issue_type="安全漏洞",
            severity="严重",
            description="发现",
            suggestion="修复",
            status="unfixed",
        )
    )
    db.commit()
    monkeypatch.setattr(dashboard_service.settings, "dashboard_stats_cache_seconds", 60)
    assert sum(r["count"] for r in dashboard_service.get_risk_distribution(db, member, 0)) == 1
    # 不调用本进程失效函数，模拟由另一个生产 worker 完成成员移除。
    db.delete(membership)
    db.commit()
    assert sum(r["count"] for r in dashboard_service.get_risk_distribution(db, member, 0)) == 0


def test_frequency_includes_today_and_cumulative_uses_actual_dates(db):
    owner, _, project, _ = seed(db)
    task(db, owner, project)
    now = datetime.now(timezone.utc)
    result = dashboard_service.get_review_frequency(db, owner, 7)
    assert len(result) == 7
    assert result[-1] == {"date": now.date().isoformat(), "count": 1}
    assert result[0]["date"] == (now.date() - timedelta(days=6)).isoformat()
    assert dashboard_service.get_review_frequency(db, owner, 0) == [{"date": now.date().isoformat(), "count": 1}]


def test_archive_counts_are_separate_from_active_code_files(db):
    from app.models.code_file import CodeFile
    from app.models.project_source_archive import ProjectSourceArchive
    from app.services.project_service import get_project, list_projects

    owner, _, project, _ = seed(db)
    db.add(CodeFile(project_id=project.id, file_name="active.py", language="python", content="x=1", status="active"))
    db.add(CodeFile(project_id=project.id, file_name="deleted.py", language="python", content="x=2", status="deleted"))
    archive_project = Project(user_id=owner.id, project_name="整包项目", status="active")
    db.add(archive_project)
    db.flush()
    archive = ProjectSourceArchive(
        project_id=archive_project.id,
        owner_id=owner.id,
        original_filename="source.zip",
        archive_sha256="a" * 64,
        compressed_size=10,
        expanded_size=20,
        file_count=4276,
        max_member_size=20,
        max_compression_ratio=2,
        storage_status="active",
        malware_status="clean",
        scan_summary_json="{}",
        archive_blob=b"archive",
    )
    db.add(archive)
    db.commit()
    summary = dashboard_service.get_summary(db, owner)
    assert summary["file_count"] == 1
    assert summary["archive_file_count"] == 4276
    items = {item["id"]: item for item in list_projects(db, owner)["items"]}
    assert items[project.id]["active_file_count"] == 1
    assert items[project.id]["archive_file_count"] == 0
    assert items[archive_project.id]["active_file_count"] == 0
    assert items[archive_project.id]["archive_file_count"] == 4276
    assert items[archive_project.id]["file_count"] == 4276
    assert get_project(db, owner, archive_project.id)["archive_file_count"] == 4276
    assert archive.file_count == 4276


def test_average_code_score_excludes_sandbox_pentest_and_unknown_types(db):
    owner, _, project, _ = seed(db)
    code = task(db, owner, project, status="success", score=0)
    sandbox = task(db, owner, project, status="success", score=100)
    sandbox.review_type = "sandbox_test"
    pentest = task(db, owner, project, status="success", score=100)
    pentest.review_type = "pentest"
    old = task(db, owner, project, status="success", score=100)
    old.review_type = "legacy_unknown"
    task(db, owner, project, status="success", score=101)
    db.flush()
    result = dashboard_service.get_summary(db, owner)
    assert result["review_count"] == 5
    assert result["code_review_count"] == 1
    assert result["avg_score"] == 0
    assert {r["id"]: r["review_type"] for r in result["recent_tasks"]}[sandbox.id] == "sandbox_test"
    assert {r["id"]: r["review_type"] for r in result["recent_tasks"]}[code.id] == "standard"


def test_sandbox_only_has_no_code_score_samples(db):
    owner, _, project, _ = seed(db)
    sandbox = task(db, owner, project, status="success", score=100)
    sandbox.review_type = "sandbox_test"
    db.flush()
    result = dashboard_service.get_summary(db, owner)
    assert result["review_count"] == 1
    assert result["code_review_count"] == 0
    assert result["avg_score"] == 0
