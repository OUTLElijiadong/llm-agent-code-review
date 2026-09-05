"""调度配置必须同步持久化状态与当前进程的后台计划。"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import Mock

import pytest
from apscheduler.events import EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, ValidationError
from app.models.agent_governance import AgentJob, AgentJobRun
from app.services import agent_scheduler_runtime as runtime
from app.services import scheduler_service


@pytest.fixture
def scheduler(monkeypatch):
    instance = BackgroundScheduler(timezone="UTC")
    instance.start(paused=True)
    monkeypatch.setattr(runtime, "_scheduler", instance)
    monkeypatch.setattr(settings, "agent_governance_scheduler_enabled", True)
    monkeypatch.setattr(settings, "skill_scheduler_enabled", True)
    try:
        yield instance
    finally:
        instance.shutdown(wait=True)


@pytest.fixture
def job(db):
    item = AgentJob(
        job_code="configuration-closure",
        job_type="evolution",
        agent_code="evolution",
        schedule="interval@5m",
        status="enabled",
        config_json='{"before": true}',
    )
    db.add(item)
    db.commit()
    return item


def registered_job(scheduler, job):
    return scheduler.get_job(f"agent-governance-{job.id}")


def test_disable_removes_live_registration_and_enable_needs_no_restart(db, scheduler, job):
    runtime._register_job_to_scheduler(scheduler, job)

    scheduler_service.update_job(db, job.id, {"status": "disabled"})
    assert registered_job(scheduler, job) is None
    assert db.get(AgentJob, job.id).status == "disabled"

    scheduler_service.update_job(db, job.id, {"status": "enabled"})
    assert registered_job(scheduler, job).trigger.interval == timedelta(minutes=5)
    assert len(scheduler.get_jobs()) == 1


@pytest.mark.parametrize("schedule, interval", [("interval@2m", 120), ("interval@7s", 7)])
def test_interval_update_changes_live_trigger(db, scheduler, job, schedule, interval):
    runtime._register_job_to_scheduler(scheduler, job)

    scheduler_service.update_job(db, job.id, {"schedule": schedule})

    active = registered_job(scheduler, job)
    assert active.trigger.interval == timedelta(seconds=interval)
    assert active.args == (job.id, schedule)
    assert active.max_instances == 1
    assert active.coalesce is True


@pytest.mark.parametrize("schedule", ["daily@21:17", "hourly@*:23", "hourly@23", "*/7 * * * *"])
def test_cron_and_legacy_schedules_update_live(db, scheduler, job, schedule):
    runtime._register_job_to_scheduler(scheduler, job)

    scheduler_service.update_job(db, job.id, {"schedule": schedule})

    active = registered_job(scheduler, job)
    assert active.trigger.__class__.__name__ == "CronTrigger"
    assert active.args == (job.id, schedule)
    assert active.next_run_time is not None


def test_retry_and_config_only_update_preserve_next_run_time(db, scheduler, job):
    runtime._register_job_to_scheduler(scheduler, job)
    next_run_time = registered_job(scheduler, job).next_run_time

    for _attempt in range(3):
        scheduler_service.update_job(
            db, job.id, {"schedule": job.schedule, "status": "enabled", "config_json": {"after": True}}
        )

    assert len(scheduler.get_jobs()) == 1
    assert registered_job(scheduler, job).next_run_time == next_run_time
    assert json.loads(job.config_json) == {"after": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"schedule": ""}, {"schedule": "garbage"}, {"schedule": "daily@25:00"},
        {"schedule": "hourly@60"}, {"schedule": "interval@0m"}, {"schedule": "interval@86401s"},
        {"schedule": "60 * * * *"}, {"schedule": "0 0 30 2 *"}, {"status": "paused"},
    ],
)
def test_invalid_update_is_rejected_without_mutation(db, scheduler, job, payload):
    runtime._register_job_to_scheduler(scheduler, job)
    next_run_time = registered_job(scheduler, job).next_run_time

    with pytest.raises(ValidationError):
        scheduler_service.update_job(db, job.id, payload)

    db.refresh(job)
    assert job.schedule == "interval@5m"
    assert job.status == "enabled"
    assert registered_job(scheduler, job).next_run_time == next_run_time


def test_manual_schedule_removes_automatic_registration(db, scheduler, job):
    runtime._register_job_to_scheduler(scheduler, job)

    scheduler_service.update_job(db, job.id, {"schedule": "manual"})

    assert registered_job(scheduler, job) is None
    assert job.status == "enabled"


@pytest.mark.parametrize("status", ["enabled", "disabled"])
def test_registration_failure_rolls_back_database_and_is_retryable(db, scheduler, job, monkeypatch, status):
    job.status = status
    db.commit()
    runtime._register_job_to_scheduler(scheduler, job)
    previous = registered_job(scheduler, job)
    previous_time = previous.next_run_time if previous else None
    original_add = scheduler.add_job

    def fail_after_register(*args, **kwargs):
        original_add(*args, **kwargs)
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(scheduler, "add_job", fail_after_register)
    with pytest.raises(ServiceUnavailableError):
        scheduler_service.update_job(db, job.id, {"schedule": "interval@11m", "status": "enabled"})

    db.refresh(job)
    assert job.schedule == "interval@5m"
    assert job.status == status
    if previous:
        assert registered_job(scheduler, job).trigger.interval == timedelta(minutes=5)
        assert registered_job(scheduler, job).next_run_time == previous_time
    else:
        assert registered_job(scheduler, job) is None

    monkeypatch.setattr(scheduler, "add_job", original_add)
    scheduler_service.update_job(db, job.id, {"schedule": "interval@11m", "status": "enabled"})
    assert len(scheduler.get_jobs()) == 1
    assert registered_job(scheduler, job).trigger.interval == timedelta(minutes=11)


def test_remove_failure_rolls_back_database(db, scheduler, job, monkeypatch):
    runtime._register_job_to_scheduler(scheduler, job)
    original_remove = scheduler.remove_job

    def fail_after_remove(*args, **kwargs):
        original_remove(*args, **kwargs)
        raise RuntimeError("injected remove failure")

    monkeypatch.setattr(scheduler, "remove_job", fail_after_remove)
    with pytest.raises(ServiceUnavailableError):
        scheduler_service.update_job(db, job.id, {"status": "disabled"})

    db.refresh(job)
    assert job.status == "enabled"
    assert registered_job(scheduler, job) is not None


@pytest.mark.parametrize("payload", [{"schedule": "interval@9m"}, {"status": "disabled"}])
def test_database_commit_failure_restores_runtime(db, scheduler, job, monkeypatch, payload):
    runtime._register_job_to_scheduler(scheduler, job)
    next_run_time = registered_job(scheduler, job).next_run_time
    monkeypatch.setattr(db, "commit", Mock(side_effect=RuntimeError("injected commit failure")))

    with pytest.raises(RuntimeError, match="injected commit failure"):
        scheduler_service.update_job(db, job.id, payload)

    db.refresh(job)
    assert job.status == "enabled"
    assert job.schedule == "interval@5m"
    assert registered_job(scheduler, job).trigger.interval == timedelta(minutes=5)
    assert registered_job(scheduler, job).next_run_time == next_run_time


def test_missing_runtime_cannot_claim_live_update_success(db, job, monkeypatch):
    monkeypatch.setattr(runtime, "_scheduler", None)
    monkeypatch.setattr(settings, "agent_governance_scheduler_enabled", True)

    with pytest.raises(ServiceUnavailableError):
        scheduler_service.update_job(db, job.id, {"schedule": "interval@12m"})

    db.refresh(job)
    assert job.schedule == "interval@5m"


def test_explicit_manual_only_configuration_can_be_saved(db, job, monkeypatch):
    monkeypatch.setattr(runtime, "_scheduler", None)
    monkeypatch.setattr(settings, "agent_governance_scheduler_enabled", False)

    scheduler_service.update_job(db, job.id, {"schedule": "interval@12m"})

    assert job.schedule == "interval@12m"


def test_disabling_legacy_invalid_schedule_removes_old_registration(db, scheduler, job):
    runtime._register_job_to_scheduler(scheduler, job)
    job.schedule = "legacy-invalid"
    db.commit()

    scheduler_service.update_job(db, job.id, {"status": "disabled"})

    assert job.status == "disabled"
    assert registered_job(scheduler, job) is None


def test_skill_kill_switch_removes_registration_on_update(db, scheduler, job, monkeypatch):
    job.job_type = "skill_evolution"
    db.commit()
    runtime._register_job_to_scheduler(scheduler, job)
    monkeypatch.setattr(settings, "skill_scheduler_enabled", False)

    scheduler_service.update_job(db, job.id, {"schedule": "interval@4m"})

    assert registered_job(scheduler, job) is None


def test_failed_flush_does_not_change_runtime(db, scheduler, job, monkeypatch):
    runtime._register_job_to_scheduler(scheduler, job)
    previous_time = registered_job(scheduler, job).next_run_time
    with monkeypatch.context() as failure:
        failure.setattr(db, "flush", Mock(side_effect=RuntimeError("injected flush failure")))
        with pytest.raises(RuntimeError, match="injected flush failure"):
            scheduler_service.update_job(db, job.id, {"schedule": "interval@4m"})

    db.refresh(job)
    assert job.schedule == "interval@5m"
    assert registered_job(scheduler, job).next_run_time == previous_time


def test_compensation_failure_is_not_success_and_retry_repairs_runtime(db, scheduler, job, monkeypatch):
    runtime._register_job_to_scheduler(scheduler, job)
    with monkeypatch.context() as failure:
        failure.setattr(db, "commit", Mock(side_effect=RuntimeError("injected commit failure")))
        failure.setattr(scheduler, "modify_job", Mock(side_effect=RuntimeError("injected compensation failure")))
        with pytest.raises(ServiceUnavailableError, match="恢复失败"):
            scheduler_service.update_job(db, job.id, {"schedule": "interval@4m"})

    db.refresh(job)
    assert job.schedule == "interval@5m"
    scheduler_service.update_job(db, job.id, {"schedule": "interval@5m"})
    assert len(scheduler.get_jobs()) == 1
    assert registered_job(scheduler, job).trigger.interval == timedelta(minutes=5)


def test_queued_disabled_callback_is_skipped_but_manual_run_remains_available(db, scheduler, job, monkeypatch):
    runtime._register_job_to_scheduler(scheduler, job)
    queued = registered_job(scheduler, job)
    execute = Mock(return_value={"success": True})
    monkeypatch.setattr(scheduler_service, "_execute_job", execute)
    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(runtime, "SessionLocal", sessions)

    scheduler_service.update_job(db, job.id, {"status": "disabled"})
    queued.func(*queued.args, **queued.kwargs)
    execute.assert_not_called()

    manual = scheduler_service.run_job(db, job.id)
    assert manual.status == "success"
    assert execute.call_count == 1


def test_scheduled_run_refreshes_stale_orm_state(db, scheduler, job, monkeypatch):
    execute = Mock(return_value={"success": True})
    monkeypatch.setattr(scheduler_service, "_execute_job", execute)
    with sessionmaker(bind=db.get_bind(), expire_on_commit=False)() as other:
        other.get(AgentJob, job.id).status = "disabled"
        other.commit()

    run = scheduler_service.run_job(db, job.id, system_scheduled=True)

    execute.assert_not_called()
    assert json.loads(run.result_json)["reason"] == "job_disabled"


@pytest.mark.parametrize("operation", ["update", "scheduled_run"])
def test_job_configuration_uses_current_read_not_mysql_transaction_snapshot(db, scheduler, job, monkeypatch, operation):
    statements = []

    def capture(state):
        mapper = state.bind_arguments.get("mapper")
        if state.is_select and mapper is not None and mapper.class_ is AgentJob:
            statements.append(str(state.statement.compile(dialect=mysql.dialect())))

    monkeypatch.setattr(scheduler_service, "_execute_job", Mock(return_value={"success": True}))
    event.listen(db, "do_orm_execute", capture)
    try:
        if operation == "update":
            scheduler_service.update_job(db, job.id, {"status": "disabled"})
        else:
            scheduler_service.run_job(db, job.id, system_scheduled=True)
    finally:
        event.remove(db, "do_orm_execute", capture)

    assert statements
    assert "FOR UPDATE" in statements[0]


def test_old_cadence_callback_cannot_run_after_rescheduling(db, scheduler, job, monkeypatch):
    runtime._register_job_to_scheduler(scheduler, job)
    queued = registered_job(scheduler, job)
    execute = Mock(return_value={"success": True})
    monkeypatch.setattr(scheduler_service, "_execute_job", execute)
    monkeypatch.setattr(runtime, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))

    scheduler_service.update_job(db, job.id, {"schedule": "interval@13m"})
    queued.func(*queued.args, **queued.kwargs)
    execute.assert_not_called()

    active = registered_job(scheduler, job)
    active.func(*active.args, **active.kwargs)
    assert execute.call_count == 1


def test_configuration_update_from_existing_event_loop(db, scheduler, job):
    runtime._register_job_to_scheduler(scheduler, job)

    async def update():
        scheduler_service.update_job(db, job.id, {"schedule": "interval@14m"})

    asyncio.run(update())
    assert registered_job(scheduler, job).trigger.interval == timedelta(minutes=14)


def test_configuration_update_from_worker_thread(scheduler, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}", connect_args={"check_same_thread": False})
    AgentJob.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with sessions() as session:
            item = AgentJob(job_code="thread-job", job_type="evolution", schedule="interval@5m", status="enabled")
            session.add(item)
            session.commit()
            runtime._register_job_to_scheduler(scheduler, item)

        def update():
            with sessions() as session:
                scheduler_service.update_job(session, item.id, {"schedule": "interval@15m"})

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(update).result(timeout=5)

        assert registered_job(scheduler, item).trigger.interval == timedelta(minutes=15)
    finally:
        engine.dispose()


def test_real_timer_applies_cadence_disable_enable_and_current_config(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'timer.db'}", connect_args={"check_same_thread": False})
    AgentJob.__table__.create(engine)
    AgentJobRun.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    instance = BackgroundScheduler(timezone="UTC")
    finished = Event()
    observed = []
    instance.add_listener(lambda _event: finished.set(), EVENT_JOB_EXECUTED)
    monkeypatch.setattr(runtime, "_scheduler", instance)
    monkeypatch.setattr(runtime, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "agent_governance_scheduler_enabled", True)

    def execute(_session, item):
        observed.append(json.loads(item.config_json))
        return {"success": True}

    monkeypatch.setattr(scheduler_service, "_execute_job", execute)
    try:
        with sessions() as session:
            item = AgentJob(job_code="real-timer", job_type="evolution", schedule="interval@60s", status="enabled")
            session.add(item)
            session.commit()
            runtime._register_job_to_scheduler(instance, item)
        instance.start()
        with sessions() as session:
            scheduler_service.update_job(session, item.id, {"schedule": "interval@1s", "config_json": {"version": 1}})
        assert finished.wait(timeout=5)
        assert observed == [{"version": 1}]

        with sessions() as session:
            scheduler_service.update_job(session, item.id, {"status": "disabled"})
        finished.clear()
        assert not finished.wait(timeout=1.2)

        with sessions() as session:
            scheduler_service.update_job(session, item.id, {"status": "enabled", "config_json": {"version": 2}})
        assert finished.wait(timeout=5)
        assert observed == [{"version": 1}, {"version": 2}]
        with sessions() as session:
            persisted = session.get(AgentJob, item.id)
            assert persisted.status == "enabled"
            assert persisted.schedule == "interval@1s"
            assert json.loads(persisted.config_json) == {"version": 2}
            assert session.query(AgentJobRun).filter(AgentJobRun.status == "failed").count() == 0
    finally:
        if instance.running:
            instance.shutdown(wait=True)
        engine.dispose()
