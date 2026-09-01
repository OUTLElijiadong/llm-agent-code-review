"""调度服务传给 Skill 的动作参数必须与执行器契约一致。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.models.agent_governance import AgentAlert, AgentJob, AgentJobRun, AgentProfile
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


def test_scheduler_registers_second_interval_job() -> None:
    from app.services import agent_scheduler_runtime

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class FakeScheduler:
        def add_job(self, *args: Any, **kwargs: Any) -> None:
            calls.append((args, kwargs))

    job = SimpleNamespace(
        id=8,
        job_code="sandbox_heartbeat",
        job_type="sandbox_heartbeat",
        status="enabled",
        schedule="interval@30s",
    )

    registered = agent_scheduler_runtime._register_job_to_scheduler(FakeScheduler(), job)

    assert registered is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == "interval"
    assert kwargs["seconds"] == 30
    assert "minutes" not in kwargs


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


def test_security_monitor_partial_failure_does_not_block_scheduler(db: Any, monkeypatch: Any) -> None:
    """安全监控单项采集失败保留在结果中，但不阻断后续调度。"""
    job = AgentJob(
        job_code="security-monitor-failure",
        job_type="security_monitor",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr(
        scheduler_service,
        "_execute_job",
        lambda _db, _job: {
            "success": False,
            "partial_failure": True,
            "errors": [{"action": "nginx_attack_events", "error": "暂时不可用"}],
        },
    )

    run = scheduler_service.run_job(db, job.id, system_scheduled=True)

    assert run.status == "success"
    assert run.error is None
    assert json.loads(run.result_json)["errors"][0]["action"] == "nginx_attack_events"


def test_security_monitor_total_failure_is_persisted_as_failed(db: Any, monkeypatch: Any) -> None:
    """所有安全数据源都失败时不能伪装成成功。"""
    job = AgentJob(
        job_code="security-monitor-total-failure",
        job_type="security_monitor",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr(
        scheduler_service,
        "_execute_job",
        lambda _db, _job: {
            "success": False,
            "fatal_failure": True,
            "error": "安全监控所有数据源均不可用",
            "errors": [{"action": "status", "error": "执行器不可用"}],
        },
    )

    run = scheduler_service.run_job(db, job.id, system_scheduled=True)

    assert run.status == "failed"
    assert "所有数据源" in (run.error or "")


def test_failed_skill_result_is_persisted_as_failed(db: Any, monkeypatch: Any) -> None:
    """非安全监控任务的真实失败仍应在运行记录中可见。"""
    job = AgentJob(
        job_code="skill-failure-visible",
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
        lambda _db, _job: {"success": False, "error": "模型暂时不可用"},
    )

    run = scheduler_service.run_job(db, job.id, system_scheduled=True)

    assert run.status == "failed"
    assert run.error == "模型暂时不可用"


def test_reconcile_stale_scheduler_runs_closes_only_old_orphans(db: Any) -> None:
    """旧进程遗留的调度运行可收敛，最近运行不能被误伤。"""
    now = datetime.now(timezone.utc)
    job = AgentJob(
        job_code="orphan-recovery",
        job_type="archive_empty_sessions",
        agent_code="monitor",
        schedule="interval@30m",
        status="enabled",
    )
    db.add(job)
    db.flush()
    stale = AgentJobRun(
        job_id=job.id,
        status="running",
        started_at=now - timedelta(hours=8),
        result_json=json.dumps({"started_by": "old-worker"}),
    )
    recent = AgentJobRun(
        job_id=job.id,
        status="running",
        started_at=now - timedelta(minutes=5),
    )
    db.add_all([stale, recent])
    db.commit()

    recovered_ids = scheduler_service.reconcile_stale_job_runs(
        db,
        now=now,
        max_age_minutes=360,
    )

    assert recovered_ids == [stale.id]
    assert stale.status == "failed"
    assert stale.finished_at == now
    assert "管理员重新运行" in (stale.error or "")
    recovery = json.loads(stale.result_json)
    assert recovery["recovered"] is True
    assert recovery["recovery_reason"] == "orphaned_scheduler_run"
    assert recent.status == "running"
    assert scheduler_service.reconcile_stale_job_runs(db, now=now, max_age_minutes=360) == []


def test_stuck_scheduler_runs_are_degraded_but_can_continue(db: Any) -> None:
    """卡住记录需要人工处理，但不能把整个业务判成不可继续。"""
    now = datetime.now(timezone.utc)
    current = AgentJob(
        job_code="current-health",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    other = AgentJob(
        job_code="stuck-job",
        job_type="archive_empty_sessions",
        agent_code="monitor",
        schedule="interval@30m",
        status="enabled",
    )
    db.add_all([
        current,
        other,
        AiCallLog(
            model_name="deepseek-chat",
            agent_label="manager",
            status="success",
            response="ok",
            create_time=now,
        ),
    ])
    db.flush()
    db.add(AgentJobRun(
        job_id=other.id,
        status="running",
        started_at=now - timedelta(hours=1),
    ))
    db.commit()

    health = scheduler_service._collect_application_health(db, current.id)

    assert health["ok"] is False
    assert health["can_continue"] is True
    assert health["status"] == "degraded"
    assert health["task_queue"]["stuck_runs"] == 1
    assert health["task_queue"]["actions"][0]["code"] == "review_stuck_scheduler_runs"


def test_application_degradation_keeps_ops_health_running(db: Any, monkeypatch: Any) -> None:
    """应用侧可恢复降级也要留下人工动作，而不是把巡检变成失败。"""

    class FakeOperationsAgent:
        name = "operations"

        def execute_action(self, *_args: Any, action: str, **_kwargs: Any) -> Any:
            if action == "status":
                checks = {
                    "status": "ok",
                    "can_continue": True,
                    "checks": {"backup": {"ok": True, "status": "ok"}},
                }
                return SimpleNamespace(
                    success=True,
                    data={"id": 201, "status": "success", "result": {"ok": True, "result": {"checks": checks}}},
                )
            return SimpleNamespace(
                success=True,
                data={"id": 202, "status": "success", "result": {"ok": True, "result": {"valid_for_30_days": True}}},
            )

    job = AgentJob(
        job_code="ops-health-application-degraded",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr("app.agents.operations_agent.OperationsAgent", FakeOperationsAgent)
    monkeypatch.setattr(
        scheduler_service,
        "_collect_application_health",
        lambda *_args: {
            "ok": False,
            "can_continue": True,
            "status": "degraded",
            "task_queue": {
                "actions": [{
                    "code": "review_stuck_scheduler_runs",
                    "label": "查看卡住的自动任务",
                    "requires_human": True,
                }],
            },
        },
    )

    result = scheduler_service._execute_ops_health_check(db, job)

    assert result["success"] is True
    assert result["status"] == "degraded"
    assert result["can_continue"] is True
    assert result["recommended_actions"][0]["code"] == "review_stuck_scheduler_runs"


def test_recovered_scheduler_failure_is_visible_without_blocking_health(db: Any) -> None:
    """已自动收敛的孤儿记录保留在人可读的恢复清单中，不再重复阻断巡检。"""
    now = datetime.now(timezone.utc)
    current = AgentJob(
        job_code="current-health-recovered",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    other = AgentJob(
        job_code="recovered-job",
        job_type="archive_empty_sessions",
        agent_code="monitor",
        schedule="interval@30m",
        status="enabled",
    )
    db.add_all([
        current,
        other,
        AiCallLog(
            model_name="deepseek-chat",
            agent_label="manager",
            status="success",
            response="ok",
            create_time=now,
        ),
    ])
    db.flush()
    db.add(AgentJobRun(
        job_id=other.id,
        status="failed",
        started_at=now - timedelta(hours=8),
        finished_at=now,
        result_json=json.dumps({
            "recovered": True,
            "recovery_reason": "orphaned_scheduler_run",
        }),
        error="调度进程中断导致运行遗留，已自动回收；可由管理员重新运行",
    ))
    db.commit()

    health = scheduler_service._collect_application_health(db, current.id)

    assert health["ok"] is False
    assert health["can_continue"] is True
    assert health["status"] == "degraded"
    assert health["task_queue"]["failed_jobs"] == []
    assert health["task_queue"]["recovered_jobs"] == [other.job_code]
    assert health["task_queue"]["actions"][0]["code"] == "review_recovered_scheduler_runs"


def test_degraded_ops_health_warns_but_does_not_fail_the_job(db: Any, monkeypatch: Any) -> None:
    """可继续的资源压力应该交给人处置，不应堵死定时任务。"""

    class FakeOperationsAgent:
        name = "operations"

        def execute_action(self, *_args: Any, action: str, **_kwargs: Any) -> Any:
            if action == "status":
                checks = {
                    "status": "degraded",
                    "can_continue": True,
                    "summary": "磁盘使用率偏高，请人工审阅。",
                    "actions": [{
                        "code": "disk_cleanup_review",
                        "label": "审阅磁盘清理",
                        "message": "清理前先审阅可回收文件",
                        "requires_human": True,
                    }],
                    "checks": {
                        "backup": {"ok": True, "status": "ok"},
                        "containers": {"services": {"backend": "healthy"}},
                    },
                }
                return SimpleNamespace(
                    success=True,
                    data={
                        "id": 101,
                        "status": "success",
                        "result": {"ok": True, "result": {"checks": checks}},
                    },
                )
            return SimpleNamespace(
                success=True,
                data={
                    "id": 102,
                    "status": "success",
                    "result": {"ok": True, "result": {"valid_for_30_days": True}},
                },
            )

        def diagnose(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("可继续的降级状态不应隐式调用模型诊断")

    job = AgentJob(
        job_code="ops-health-degraded",
        job_type="ops_health_check",
        agent_code="operations",
        schedule="interval@5m",
        status="enabled",
    )
    db.add(job)
    db.commit()
    monkeypatch.setattr("app.agents.operations_agent.OperationsAgent", FakeOperationsAgent)
    monkeypatch.setattr(scheduler_service, "_collect_application_health", lambda *_args: {"ok": True})

    result = scheduler_service._execute_ops_health_check(db, job)

    assert result["success"] is True
    assert result["status"] == "degraded"
    assert result["requires_attention"] is True
    assert result["recommended_actions"] == [{
        "code": "disk_cleanup_review",
        "label": "审阅磁盘清理",
        "message": "清理前先审阅可回收文件",
        "requires_human": True,
    }]
    alert = db.query(AgentAlert).filter_by(title="AI 自动运维巡检异常").one()
    assert alert.severity == "warning"


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
