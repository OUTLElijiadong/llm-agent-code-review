"""调度服务传给 Skill 的动作参数必须与执行器契约一致。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.models.agent_governance import AgentJob, AgentJobRun, AgentProfile
from app.models.ai_call_log import AiCallLog
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import scheduler_service


def test_disabled_skill_scheduler_does_not_create_default_skill_jobs(
    db: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "skill_scheduler_enabled", False)

    jobs = scheduler_service.ensure_default_jobs(db)

    assert jobs
    assert all(job.job_type not in {"skill_evolution", "skill_proactive"} for job in jobs)


def test_disabled_skill_scheduler_does_not_register_existing_skill_job(
    monkeypatch: Any,
) -> None:
    from app.services import agent_scheduler_runtime

    calls: list[dict[str, Any]] = []

    class FakeScheduler:
        def add_job(self, *_args: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

    job = SimpleNamespace(
        id=7,
        job_code="daily_skill_evolution_code_reviewer",
        job_type="skill_evolution",
        status="enabled",
        schedule="daily@03:00",
    )
    monkeypatch.setattr(settings, "skill_scheduler_enabled", False)

    registered = agent_scheduler_runtime._register_job_to_scheduler(FakeScheduler(), job)

    assert registered is False
    assert calls == []


def test_disabled_skill_scheduler_skips_existing_system_job(
    db: Any,
    monkeypatch: Any,
) -> None:
    job = AgentJob(
        job_code="daily_skill_evolution_code_reviewer",
        job_type="skill_evolution",
        agent_code="code_reviewer",
        schedule="daily@03:00",
        status="enabled",
    )
    db.add(job)
    db.commit()
    calls = {"execute": 0}

    def execute(*_args: Any) -> dict[str, Any]:
        calls["execute"] += 1
        return {"success": True}

    monkeypatch.setattr(settings, "skill_scheduler_enabled", False)
    monkeypatch.setattr(scheduler_service, "_execute_job", execute)

    run = scheduler_service.run_job(db, job.id, system_scheduled=True)

    assert run.status == "success"
    assert calls["execute"] == 0
    result = json.loads(run.result_json)
    assert result["skipped"] is True
    assert result["reason"] == "skill_scheduler_disabled"


def test_daily_budget_blocks_scheduled_skill_before_orchestrator(
    db: Any,
    monkeypatch: Any,
) -> None:
    now = datetime.now(timezone.utc)
    db.add_all([
        AgentProfile(
            code="code_reviewer",
            name="代码审查",
            category="quality",
            status="idle",
            is_enabled=1,
            budget_tokens_daily=100,
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="code_reviewer",
            status="success",
            total_tokens=100,
            create_time=now,
        ),
    ])
    db.commit()
    calls = {"invoke": 0}

    class FakeOrchestrator:
        def __init__(self, register: bool = False) -> None:
            self._db = None

        def invoke_skill(self, **_kwargs: Any) -> Any:
            calls["invoke"] += 1
            return SimpleNamespace(success=True, data={"effect": "no_op"}, duration_ms=1)

    monkeypatch.setattr("app.agents.orchestrator.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(settings, "skill_scheduler_enabled", True)
    job = SimpleNamespace(
        id=8,
        job_code="hourly_skill_proactive_code_reviewer",
        agent_code="code_reviewer",
        config_json='{"action": "check_proactive"}',
        schedule="hourly@*:00",
    )

    result = scheduler_service._execute_skill_proactive(db, job)

    assert result["success"] is False
    assert result["budget_blocked"] is True
    assert result["used_tokens"] == 100
    assert result["budget_tokens"] == 100
    assert calls["invoke"] == 0


def test_disabled_direct_skill_executor_skips_orchestrator(
    db: Any,
    monkeypatch: Any,
) -> None:
    calls = {"invoke": 0}

    class FakeOrchestrator:
        def __init__(self, register: bool = False) -> None:
            self._db = None

        def invoke_skill(self, **_kwargs: Any) -> Any:
            calls["invoke"] += 1
            return SimpleNamespace(success=True, data={"effect": "no_op"}, duration_ms=1)

    monkeypatch.setattr("app.agents.orchestrator.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(settings, "skill_scheduler_enabled", False)
    job = SimpleNamespace(
        id=9,
        job_code="daily_skill_evolution_code_reviewer",
        agent_code="code_reviewer",
        config_json='{"action": "evolve"}',
        schedule="daily@03:00",
    )

    result = scheduler_service._execute_skill_evolution(db, job)

    assert result["skipped"] is True
    assert result["reason"] == "skill_scheduler_disabled"
    assert calls["invoke"] == 0


def test_budget_block_is_persisted_as_failed_job_run(db: Any, monkeypatch: Any) -> None:
    job = AgentJob(
        job_code="hourly_skill_proactive_code_reviewer",
        job_type="skill_proactive",
        agent_code="code_reviewer",
        schedule="hourly@*:00",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr(settings, "skill_scheduler_enabled", True)
    monkeypatch.setattr(
        scheduler_service,
        "_execute_job",
        lambda *_args: {
            "success": False,
            "budget_blocked": True,
            "error": "Agent code_reviewer 当日自动任务 token 预算已用尽",
            "used_tokens": 100,
            "budget_tokens": 100,
        },
    )

    run = scheduler_service.run_job(db, job.id, system_scheduled=True)

    assert run.status == "failed"
    assert "预算已用尽" in run.error


def test_proactive_scheduler_uses_action_type(db: Any, monkeypatch: Any) -> None:
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
    monkeypatch.setattr(settings, "skill_scheduler_enabled", True)
    job = SimpleNamespace(
        id=1,
        job_code="proactive-test",
        agent_code="code_reviewer",
        config_json='{"action": "reflect_from_logs"}',
        schedule="0 0 * * *",
    )

    result = scheduler_service._execute_skill_proactive(db, job)

    assert result["success"] is True
    assert captured["params"] == {"action_type": "reflect_from_logs"}
    assert captured["trigger_type"] == "scheduled"


def test_self_improvement_scheduler_keeps_action_parameter(db: Any, monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeOrchestrator:
        def __init__(self, register: bool = False) -> None:
            self._db = None

        def invoke_skill(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(success=True, data={"effect": "no_op"}, duration_ms=1)

    monkeypatch.setattr("app.agents.orchestrator.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(settings, "skill_scheduler_enabled", True)
    job = SimpleNamespace(
        id=2,
        job_code="evolve-test",
        agent_code="code_reviewer",
        config_json='{"action": "evolve"}',
        schedule="0 1 * * *",
    )

    scheduler_service._execute_skill_evolution(db, job)
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


def test_unhealthy_ops_skips_background_model_diagnosis_by_default(db: Any, monkeypatch: Any) -> None:
    """健康巡检可生成告警,但默认不得隐式调用 LLM。"""
    calls = {"diagnose": 0}

    class FakeOperationsAgent:
        def execute_action(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(success=False, data={})

        def diagnose(self, *_args: Any, **_kwargs: Any) -> Any:
            calls["diagnose"] += 1
            return SimpleNamespace(data="不应被调用", error="")

    job = AgentJob(
        job_code="ops-health-cost-guard",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr("app.agents.operations_agent.OperationsAgent", FakeOperationsAgent)
    monkeypatch.setattr(scheduler_service, "_collect_application_health", lambda *_args: {"ok": False})
    monkeypatch.setattr(settings, "ops_health_diagnosis_enabled", False)

    result = scheduler_service._execute_ops_health_check(db, job)

    assert result["success"] is False
    assert result["diagnosis_skipped"] is True
    assert "成本保护" in result["diagnosis"]
    assert calls["diagnose"] == 0


def test_unhealthy_ops_can_explicitly_enable_model_diagnosis(db: Any, monkeypatch: Any) -> None:
    calls = {"diagnose": 0}

    class FakeOperationsAgent:
        def execute_action(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(success=False, data={})

        def diagnose(self, *_args: Any, **_kwargs: Any) -> Any:
            calls["diagnose"] += 1
            return SimpleNamespace(data="明确开启后的诊断", error="")

    job = AgentJob(
        job_code="ops-health-explicit-diagnosis",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr("app.agents.operations_agent.OperationsAgent", FakeOperationsAgent)
    monkeypatch.setattr(scheduler_service, "_collect_application_health", lambda *_args: {"ok": False})
    monkeypatch.setattr(settings, "ops_health_diagnosis_enabled", True)

    result = scheduler_service._execute_ops_health_check(db, job)

    assert result["diagnosis"] == "明确开启后的诊断"
    assert "diagnosis_skipped" not in result
    assert calls["diagnose"] == 1


def test_unhealthy_ops_budget_blocks_model_diagnosis(db: Any, monkeypatch: Any) -> None:
    calls = {"diagnose": 0}

    class FakeOperationsAgent:
        def execute_action(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(success=False, data={})

        def diagnose(self, *_args: Any, **_kwargs: Any) -> Any:
            calls["diagnose"] += 1
            return SimpleNamespace(data="不应被调用", error="")

    job = AgentJob(
        job_code="ops-health-budget-guard",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add_all([
        job,
        AgentProfile(
            code="operations",
            name="全服管理",
            category="operations",
            status="idle",
            is_enabled=1,
            budget_tokens_daily=50,
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="operations",
            status="success",
            total_tokens=50,
            create_time=datetime.now(timezone.utc),
        ),
    ])
    db.commit()
    monkeypatch.setattr("app.agents.operations_agent.OperationsAgent", FakeOperationsAgent)
    monkeypatch.setattr(scheduler_service, "_collect_application_health", lambda *_args: {"ok": False})
    monkeypatch.setattr(settings, "ops_health_diagnosis_enabled", True)

    result = scheduler_service._execute_ops_health_check(db, job)

    assert result["diagnosis_skipped"] is True
    assert result["diagnosis_budget_blocked"] is True
    assert result["diagnosis_budget"]["used_tokens"] == 50
    assert result["diagnosis_budget"]["budget_tokens"] == 50
    assert calls["diagnose"] == 0


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
