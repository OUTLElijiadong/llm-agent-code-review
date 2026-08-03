"""调度服务传给 Skill 的动作参数必须与执行器契约一致。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import ForbiddenError
from app.models.agent_governance import AgentJob, AgentJobRun, AgentProfile
from app.models.ai_call_log import AiCallLog
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import scheduler_service


def test_proactive_scheduler_uses_action_type(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeOrchestrator:
        def __init__(self, register: bool = False) -> None:
            self._db = None

        def invoke_skill(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                success=True,
                data={"effect": "no_op", "duration_ms": 2, "record_id": 9},
                duration_ms=2,
            )

    monkeypatch.setattr("app.agents.orchestrator.Orchestrator", FakeOrchestrator)
    job = SimpleNamespace(
        id=1,
        job_code="proactive-test",
        agent_code="code_reviewer",
        config_json='{"action": "reflect_from_logs"}',
        schedule="0 0 * * *",
    )

    result = scheduler_service._execute_skill_proactive(object(), job)

    assert result["success"] is True
    assert captured["params"] == {"action_type": "reflect_from_logs"}
    assert captured["trigger_type"] == "scheduled"


def test_self_improvement_scheduler_keeps_action_parameter(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeOrchestrator:
        def __init__(self, register: bool = False) -> None:
            self._db = None

        def invoke_skill(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(success=True, data={"effect": "no_op"}, duration_ms=1)

    monkeypatch.setattr("app.agents.orchestrator.Orchestrator", FakeOrchestrator)
    job = SimpleNamespace(
        id=2,
        job_code="evolve-test",
        agent_code="code_reviewer",
        config_json='{"action": "evolve"}',
        schedule="0 1 * * *",
    )

    scheduler_service._execute_skill_evolution(object(), job)
    assert captured["params"] == {"action": "evolve"}


def test_application_health_closes_model_task_agent_and_evolution_loop(db: Any) -> None:
    now = datetime.now(timezone.utc)
    current = AgentJob(
        job_code="ops_health_check",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    evolution = AgentJob(
        job_code="daily_skill_evolution_code_reviewer",
        job_type="skill_evolution",
        agent_code="code_reviewer",
        schedule="daily@03:00",
        status="enabled",
    )
    db.add_all([
        current,
        evolution,
        AgentProfile(code="code_reviewer", name="审查", category="quality", status="idle", is_enabled=1),
        AiCallLog(
            model_name="deepseek-chat",
            agent_label="manager",
            status="success",
            response="ok",
            create_time=now,
        ),
    ])
    db.commit()

    healthy = scheduler_service._collect_application_health(db, current.id)
    assert healthy["ok"] is True
    assert healthy["model_api"]["state"] == "healthy"
    assert healthy["evolution"]["enabled_jobs"] == 1

    db.add(AgentJobRun(
        job_id=evolution.id,
        status="failed",
        started_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=1),
        error="test failure",
    ))
    profile = db.query(AgentProfile).filter(AgentProfile.code == "code_reviewer").one()
    profile.status = "error"
    db.add(AiCallLog(
        model_name="deepseek-chat",
        agent_label="manager",
        status="failed",
        error_message="test failure",
        create_time=now + timedelta(seconds=1),
    ))
    db.commit()

    unhealthy = scheduler_service._collect_application_health(db, current.id)
    assert unhealthy["ok"] is False
    assert unhealthy["model_api"]["state"] == "error"
    assert unhealthy["task_queue"]["failed_jobs"] == [evolution.job_code]
    assert unhealthy["agents"]["error_agents"] == ["code_reviewer"]
    assert unhealthy["evolution"]["failed_jobs"] == [evolution.job_code]


def test_unhealthy_ops_result_marks_job_run_failed(db: Any, monkeypatch: Any) -> None:
    job = AgentJob(
        job_code="ops_health_check",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr(scheduler_service, "_execute_job", lambda _db, _job: {"success": False})

    run = scheduler_service.run_job(db, job.id, system_scheduled=True)

    assert run.status == "failed"
    assert run.error == "AI 自动运维巡检检测到不健康状态"


def test_only_unique_super_admin_can_control_or_run_ops_health_job(db: Any, monkeypatch: Any) -> None:
    job = AgentJob(
        job_code="ops_health_check",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    ordinary = User(username="manager", password="x", role="admin", status=1)
    super_user = User(username="admin", password="x", role="super_admin", status=1)
    super_role = Role(name="超级管理员", code="super_admin", status="active", is_builtin=1)
    db.add_all([job, ordinary, super_user, super_role])
    db.flush()
    db.add(UserRole(user_id=super_user.id, role_id=super_role.id))
    db.commit()

    with pytest.raises(ForbiddenError, match="超级管理员"):
        scheduler_service.update_job(db, job.id, {"status": "disabled"}, actor=ordinary)
    with pytest.raises(ForbiddenError, match="超级管理员"):
        scheduler_service.run_job(db, job.id, actor=ordinary)

    updated = scheduler_service.update_job(db, job.id, {"status": "disabled"}, actor=super_user)
    assert updated.status == "disabled"
    monkeypatch.setattr(scheduler_service, "_execute_job", lambda _db, _job: {"success": True})
    assert scheduler_service.run_job(db, job.id, actor=super_user).status == "success"
    assert scheduler_service.run_job(db, job.id, system_scheduled=True).status == "success"


def test_only_unique_super_admin_can_control_or_run_crawl_job(db: Any, monkeypatch: Any) -> None:
    """外部知识抓取与服务器巡检一样属于受限调度动作。"""
    job = AgentJob(
        job_code="daily_agent_knowledge_crawl",
        job_type="crawl",
        agent_code="knowledge_distiller",
        schedule="daily@02:00",
        status="enabled",
    )
    ordinary = User(username="manager", password="x", role="admin", status=1)
    super_user = User(username="admin", password="x", role="super_admin", status=1)
    super_role = Role(name="超级管理员", code="super_admin", status="active", is_builtin=1)
    db.add_all([job, ordinary, super_user, super_role])
    db.flush()
    db.add(UserRole(user_id=super_user.id, role_id=super_role.id))
    db.commit()

    with pytest.raises(ForbiddenError, match="超级管理员"):
        scheduler_service.update_job(db, job.id, {"status": "disabled"}, actor=ordinary)
    with pytest.raises(ForbiddenError, match="超级管理员"):
        scheduler_service.run_job(db, job.id, actor=ordinary)

    updated = scheduler_service.update_job(db, job.id, {"status": "disabled"}, actor=super_user)
    assert updated.status == "disabled"
    monkeypatch.setattr(scheduler_service, "_execute_job", lambda _db, _job: {"doc_count": 1})
    assert scheduler_service.run_job(db, job.id, actor=super_user).status == "success"
    assert scheduler_service.run_job(db, job.id, system_scheduled=True).status == "success"
