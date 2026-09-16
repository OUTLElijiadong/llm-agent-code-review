"""仪表盘聚合缓存:一次工作台加载内三接口共享计算,TTL/失效语义正确。"""

from datetime import datetime, timezone

import pytest

from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import dashboard_service


@pytest.fixture
def cache_user(db):
    user = User(username="cache-user", password="x", role="user", status=1)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def cache_project(db, cache_user):
    project = Project(user_id=cache_user.id, project_name="缓存项目", status="active")
    db.add(project)
    db.flush()
    return project


def _success_task(db, user, project, *, score=90):
    task = ReviewTask(
        user_id=user.id,
        project_id=project.id,
        task_name="缓存任务",
        review_type="standard",
        status="success",
        score=score,
        create_time=datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()
    return task


def test_ttl_zero_always_recomputes(db, cache_user, cache_project, monkeypatch):
    """conftest 已把 TTL 归零:写后读必须立刻可见(防测试/防陈旧兜底)。"""
    _success_task(db, cache_user, cache_project, score=88)
    db.commit()
    assert dashboard_service.get_summary(db, cache_user)["avg_score"] == 88.0

    _success_task(db, cache_user, cache_project, score=92)
    db.commit()
    assert dashboard_service.get_summary(db, cache_user)["avg_score"] == 90.0


def test_ttl_cached_until_invalidate(db, cache_user, cache_project, monkeypatch):
    """TTL 内复用同份计算;失效调用后重新计算。"""
    monkeypatch.setattr(dashboard_service.settings, "dashboard_stats_cache_seconds", 60)
    dashboard_service.invalidate_dashboard_stats()
    _success_task(db, cache_user, cache_project, score=80)
    db.commit()

    calls = {"n": 0}
    real = dashboard_service.load_task_issue_stats

    def counting(db_, tasks, *, since=None):
        calls["n"] += 1
        return real(db_, tasks, since=since)

    monkeypatch.setattr(dashboard_service, "load_task_issue_stats", counting)

    assert dashboard_service.get_summary(db, cache_user)["avg_score"] == 80.0
    assert calls["n"] == 1
    # 第二次(summary)与图表口径请求在 TTL 内不再重复逐任务解析
    dashboard_service.get_summary(db, cache_user)
    assert calls["n"] == 1

    # 新数据 + 显式失效后立刻可见
    _success_task(db, cache_user, cache_project, score=100)
    db.commit()
    dashboard_service.invalidate_dashboard_stats(user_id=cache_user.id)
    assert dashboard_service.get_summary(db, cache_user)["avg_score"] == 90.0
    assert calls["n"] == 2


def test_issue_stats_returns_restricted_entities(db, cache_user, cache_project):
    """聚合路径(load_only)正常产出统计,不因缺列报错。"""
    _success_task(db, cache_user, cache_project)
    db.commit()
    stats = dashboard_service._issue_stats(db, cache_user)
    assert stats and stats[0]["total_issues"] == 0
    assert stats[0]["source"]["type"] == "standard"


def test_visible_ids_cached_per_user(db, cache_user, cache_project, monkeypatch):
    monkeypatch.setattr(dashboard_service.settings, "dashboard_stats_cache_seconds", 60)
    dashboard_service.invalidate_dashboard_stats()
    ids1 = dashboard_service._visible_project_ids(db, cache_user)
    assert ids1 == [cache_project.id]
    # TTL 内直接复用
    assert dashboard_service._visible_project_ids(db, cache_user) == ids1
