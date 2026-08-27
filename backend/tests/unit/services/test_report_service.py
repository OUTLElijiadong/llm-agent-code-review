"""报告服务回归测试。"""
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.services.report_service import get_report_detail, list_reports


def _make_project(db, admin_user):
    """创建报告测试项目。

    Args:
        db: 数据库会话。
        admin_user: 管理员测试用户。

    Returns:
        Project: 已持久化的项目。
    """
    project = Project(user_id=admin_user.id, project_name="报告项目", language="python")
    db.add(project)
    db.commit()
    return project


def _make_file(db, project, name):
    """创建报告测试代码文件。

    Args:
        db: 数据库会话。
        project: 所属测试项目。
        name: 文件名。

    Returns:
        CodeFile: 已持久化的代码文件。
    """
    code_file = CodeFile(
        project_id=project.id,
        file_name=name,
        file_path=name,
        language="python",
        content="print('qa')\n",
    )
    db.add(code_file)
    db.commit()
    return code_file


def test_report_detail_lists_linked_files_including_clean_file(db, admin_user):
    """报告应列出任务关联的全部文件，包括没有问题的文件。"""
    project = _make_project(db, admin_user)
    flagged = _make_file(db, project, "flagged.py")
    clean = _make_file(db, project, "clean.py")
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        status="success",
        total_files=2,
        processed_files=2,
        total_issues=1,
        severe_issues=1,
        score=85,
        rules_snapshot=[{"code": "security", "name": "安全漏洞"}],
    )
    db.add(task)
    db.commit()
    db.add_all([
        ReviewTaskFile(task_id=task.id, file_id=flagged.id),
        ReviewTaskFile(task_id=task.id, file_id=clean.id),
        ReviewIssue(
            task_id=task.id,
            file_id=flagged.id,
            file_name=flagged.file_name,
            issue_type="安全漏洞",
            severity="严重",
            title="风险",
            description="desc",
            status="fixed",
        ),
    ])
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)

    assert [row["file_name"] for row in detail["files"]] == ["flagged.py", "clean.py"]
    assert detail["files"][0]["issue_count"] == 1
    assert detail["files"][1]["issue_count"] == 0
    assert detail["stats"]["fixed"] == 1
    assert detail["stats"]["severity"] == {"严重": 1}


def test_report_detail_falls_back_to_issue_files_for_legacy_task(db, admin_user):
    """旧任务没有关联记录时仍应按历史问题展示文件。"""
    project = _make_project(db, admin_user)
    code_file = _make_file(db, project, "legacy.py")
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        status="success",
        total_files=1,
        processed_files=1,
        total_issues=1,
        high_issues=1,
        score=92,
    )
    db.add(task)
    db.commit()
    db.add(ReviewIssue(
        task_id=task.id,
        file_id=code_file.id,
        file_name=code_file.file_name,
        issue_type="潜在Bug",
        severity="高",
        title="旧数据问题",
        description="desc",
    ))
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)

    assert len(detail["files"]) == 1
    assert detail["files"][0]["file_name"] == "legacy.py"
    assert detail["files"][0]["language"] == "python"


def test_report_detail_uses_final_issue_set_for_all_score_facts(db, admin_user):
    """详情不得继续返回任务表中的旧问题数、旧评分或旧 breakdown。"""
    project = _make_project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="事实源任务",
        status="success",
        total_issues=99,
        severe_issues=0,
        high_issues=99,
        medium_issues=0,
        low_issues=0,
        score=5,
        score_version="legacy-v0",
        score_breakdown={"score": 5, "risk_level": "低风险"},
    )
    db.add(task)
    db.commit()
    db.add_all([
        ReviewIssue(
            task_id=task.id,
            issue_type="安全漏洞",
            severity="严重",
            title="严重问题",
            description="desc",
            status="fixed",
        ),
        ReviewIssue(
            task_id=task.id,
            issue_type="潜在Bug",
            severity="高",
            title="高风险问题",
            description="desc",
            status="unfixed",
        ),
    ])
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)
    listed = next(item for item in list_reports(db, admin_user)["items"] if item["task_id"] == task.id)

    assert detail["task"]["task_name"] == "事实源任务"
    assert detail["task"]["name"] == "事实源任务"
    assert detail["task"]["score"] == 77
    assert detail["stats"]["total_issues"] == 2
    assert detail["stats"]["score"] == 77
    assert detail["stats"]["severe"] == 1
    assert detail["stats"]["high"] == 1
    assert detail["stats"]["medium"] == 0
    assert detail["stats"]["low"] == 0
    assert detail["stats"]["fixed"] == 1
    assert detail["stats"]["score_version"] == "severity-deduction-v1"
    assert detail["stats"]["score_breakdown"]["weights"] == {
        "严重": 15,
        "高": 8,
        "中": 3,
        "低": 1,
    }
    assert detail["stats"]["score_breakdown"]["score_source"] == "review_issues"
    assert detail["stats"]["risk_level"] == "中风险"
    assert listed["total_issues"] == detail["stats"]["total_issues"] == 2
    assert listed["score"] == detail["stats"]["score"] == 77


def test_report_detail_preserves_explicit_score_for_empty_historical_task(db, admin_user):
    """无问题历史任务的显式 100 分必须保持 100，不能被默认值覆盖。"""
    project = _make_project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="无问题任务",
        status="success",
        total_issues=0,
        score=100,
    )
    db.add(task)
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)

    assert detail["task"]["score"] == 100
    assert detail["stats"]["total_issues"] == 0
    assert detail["stats"]["score"] == 100
    assert detail["stats"]["score_breakdown"]["score"] == 100
    assert detail["stats"]["score_breakdown"]["score_source"] == "task_explicit_empty_report"
    assert detail["stats"]["risk_level"] == "低风险"
