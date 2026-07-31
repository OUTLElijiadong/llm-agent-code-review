"""真实数据源回归测试"""
from datetime import datetime, timezone

import pytest

from app.agents.orchestrator import Orchestrator
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import dashboard_service


def test_dashboard_recent_tasks_use_persisted_project_and_task_fields(db):
    """仪表盘最近活动必须来自数据库任务与项目字段"""
    user = User(
        id=101,
        username="real-user",
        password="x",
        email="real@example.com",
        role="user",
        status=1,
    )
    project = Project(
        id=201,
        user_id=101,
        project_name="真实业务项目",
        description="真实源项目",
        language="python",
        status="active",
    )
    deleted_project = Project(
        id=202,
        user_id=101,
        project_name="已删除项目",
        description="不应进入仪表盘",
        language="python",
        status="deleted",
    )
    task = ReviewTask(
        id=301,
        user_id=101,
        project_id=201,
        task_name="生产代码安全审查",
        review_type="security",
        status="success",
        score=86,
        create_time=datetime(2026, 7, 9, 8, 0, tzinfo=timezone.utc),
    )
    deleted_task = ReviewTask(
        id=302,
        user_id=101,
        project_id=202,
        task_name="删除项目旧任务",
        review_type="security",
        status="success",
        score=12,
        create_time=datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc),
    )
    db.add_all([user, project, deleted_project, task, deleted_task])
    db.commit()

    summary = dashboard_service.get_summary(db, user)

    assert summary["recent_tasks"] == [
        {
            "id": 301,
            "task_name": "生产代码安全审查",
            "project_id": 201,
            "project_name": "真实业务项目",
            "status": "success",
            "score": 86,
            "create_time": task.create_time.isoformat(),
        }
    ]


def test_orchestrator_rejects_missing_real_user(db):
    """请求级 Orchestrator 禁止回退到演示或伪 admin 用户"""
    orch = Orchestrator(register=False)

    with pytest.raises(ValueError, match="真实用户"):
        orch.inject_db(db)
