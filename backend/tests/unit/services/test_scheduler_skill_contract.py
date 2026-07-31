"""调度服务传给 Skill 的动作参数必须与执行器契约一致。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.models.agent_governance import AgentJob, AgentJobRun, AgentProfile
from app.models.ai_call_log import AiCallLog
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

    run = scheduler_service.run_job(db, job.id)

    assert run.status == "failed"
    assert run.error == "AI 自动运维巡检检测到不健康状态"
