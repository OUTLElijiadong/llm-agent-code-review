"""进程重启后恢复 Responses 运行时，Mesh 回执也必须收敛。"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.agent_mesh import AgentMeshMessage
from app.models.agent_response_run import AgentResponseRun
from app.models.user import User
from app.services import background_task_recovery


def _seed_recoverable_run(db, *, status="completed", raise_error=False):
    user = User(
        username="recovery-owner",
        password="x" * 60,
        role="user",
        status=1,
    )
    db.add(user)
    db.flush()

    session_key = "recovery-session"
    message = AgentMeshMessage(
        message_id="recovery-message",
        user_id=user.id,
        idempotency_key="recovery-idempotency",
        trace_id="recovery-trace",
        sent_from="agent:code_reviewer",
        send_to=f"session:user:{session_key}",
        message_type="task.result",
        subject="审查结果",
        payload_json="{}",
        context_json="{}",
        artifacts_json="[]",
        errors_json="[]",
        status="processing",
        attempt_count=1,
        max_attempts=3,
    )
    run = AgentResponseRun(
        run_id="recoverable-run",
        user_id=user.id,
        surface="user",
        session_key=session_key,
        mesh_message_id=message.message_id,
        status="failed",
        checkpoint_json=json.dumps({"recovery_requested": True}),
        version=1,
    )
    db.add_all([message, run])
    db.commit()
    return user, run, message, status, raise_error


@pytest.mark.parametrize("status, expected_message_status", [
    ("completed", "completed"),
    ("failed", "queued"),
])
def test_recovered_terminal_run_finishes_its_processing_mesh_message(
    db, monkeypatch, status, expected_message_status,
):
    user, _run, _message, status, _raise_error = _seed_recoverable_run(db, status=status)
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)
    finish_calls = []
    original_finish = background_task_recovery._finish_recovered_session_message

    def track_finish(*args, **kwargs):
        finish_calls.append((args[2].mesh_message_id, kwargs.get("success")))
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(background_task_recovery, "_finish_recovered_session_message", track_finish)

    class CompletedResume:
        def __init__(self, session, owner, *, surface, session_key):
            self.session = session

        async def resume(self, *, run_id, action):
            row = self.session.query(AgentResponseRun).filter_by(run_id=run_id).one()
            row.status = status
            self.session.commit()
            return SimpleNamespace(
                status=status,
                output_text="恢复后的结果",
                error="恢复失败" if status == "failed" else "",
            )

    monkeypatch.setattr(background_task_recovery, "AgentResponsesService", CompletedResume)

    background_task_recovery._resume_agent_run(background_task_recovery.AgentRunRecovery(
        run_id="recoverable-run",
        user_id=int(user.id),
        surface="user",
        session_key="recovery-session",
    ))

    message = db.query(AgentMeshMessage).filter_by(message_id="recovery-message").one()
    db.refresh(message)
    assert finish_calls == [("recovery-message", status == "completed")]
    assert message.status == expected_message_status


@pytest.mark.parametrize("status", ["waiting_approval", "waiting_input"])
def test_recovered_waiting_run_keeps_mesh_message_processing(db, monkeypatch, status):
    user, _run, _message, _status, _raise_error = _seed_recoverable_run(db, status=status)
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)

    class PausedResume:
        def __init__(self, session, owner, *, surface, session_key):
            self.session = session

        async def resume(self, *, run_id, action):
            row = self.session.query(AgentResponseRun).filter_by(run_id=run_id).one()
            row.status = status
            self.session.commit()
            return SimpleNamespace(status=status, output_text="", error="")

    monkeypatch.setattr(background_task_recovery, "AgentResponsesService", PausedResume)
    background_task_recovery._resume_agent_run(background_task_recovery.AgentRunRecovery(
        run_id="recoverable-run",
        user_id=int(user.id),
        surface="user",
        session_key="recovery-session",
    ))

    message = db.query(AgentMeshMessage).filter_by(message_id="recovery-message").one()
    db.refresh(message)
    assert message.status == "processing"


def test_recovery_exception_releases_processing_mesh_message_for_retry(db, monkeypatch):
    user, _run, _message, _status, _raise_error = _seed_recoverable_run(db, raise_error=True)
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)

    class FailedResume:
        def __init__(self, session, owner, *, surface, session_key):
            self.session = session

        async def resume(self, *, run_id, action):
            raise RuntimeError("恢复执行异常")

    monkeypatch.setattr(background_task_recovery, "AgentResponsesService", FailedResume)
    background_task_recovery._resume_agent_run(background_task_recovery.AgentRunRecovery(
        run_id="recoverable-run",
        user_id=int(user.id),
        surface="user",
        session_key="recovery-session",
    ))

    message = db.query(AgentMeshMessage).filter_by(message_id="recovery-message").one()
    db.refresh(message)
    assert message.status == "queued"


def test_terminal_run_retries_mesh_receipt_after_first_persistence_failure(db, monkeypatch):
    user, _run, _message, _status, _raise_error = _seed_recoverable_run(db)
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)
    finish_calls = []
    original_finish = background_task_recovery._finish_recovered_session_message

    class CompletedResume:
        def __init__(self, session, owner, *, surface, session_key):
            self.session = session

        async def resume(self, *, run_id, action):
            row = self.session.query(AgentResponseRun).filter_by(run_id=run_id).one()
            row.status = "completed"
            row.checkpoint_json = json.dumps({
                "status": "completed",
                "output_text": "已完成并持久化的恢复结果",
                "error": "",
            })
            self.session.commit()
            return SimpleNamespace(
                status="completed",
                output_text="已完成并持久化的恢复结果",
                error="",
            )

    def fail_once(*args, **kwargs):
        finish_calls.append(kwargs.get("success"))
        if len(finish_calls) == 1:
            raise RuntimeError("Mesh 回执提交临时失败")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(background_task_recovery, "AgentResponsesService", CompletedResume)
    monkeypatch.setattr(background_task_recovery, "_finish_recovered_session_message", fail_once)

    background_task_recovery._resume_agent_run(background_task_recovery.AgentRunRecovery(
        run_id="recoverable-run",
        user_id=int(user.id),
        surface="user",
        session_key="recovery-session",
    ))

    message = db.query(AgentMeshMessage).filter_by(message_id="recovery-message").one()
    db.refresh(message)
    assert finish_calls == [True, True]
    assert message.status == "completed"


def test_startup_scan_picks_up_terminal_run_with_unfinished_mesh_receipt(db, monkeypatch):
    user, run, _message, _status, _raise_error = _seed_recoverable_run(db)
    run.status = "completed"
    run.checkpoint_json = json.dumps({
        "status": "completed",
        "output_text": "已完成但尚未回写的结果",
        "error": "",
    })
    db.commit()
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)

    recoveries = background_task_recovery.find_interrupted_agent_runs()

    assert recoveries == [background_task_recovery.AgentRunRecovery(
        run_id="recoverable-run",
        user_id=int(user.id),
        surface="user",
        session_key="recovery-session",
    )]


def test_terminal_failed_run_releases_mesh_message_after_receipt_retry(db, monkeypatch):
    user, _run, _message, _status, _raise_error = _seed_recoverable_run(db)
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)
    finish_calls = []
    original_finish = background_task_recovery._finish_recovered_session_message

    class FailedResume:
        def __init__(self, session, owner, *, surface, session_key):
            self.session = session

        async def resume(self, *, run_id, action):
            row = self.session.query(AgentResponseRun).filter_by(run_id=run_id).one()
            row.status = "failed"
            row.checkpoint_json = json.dumps({
                "status": "failed",
                "output_text": "",
                "error": "最终运行失败",
            })
            self.session.commit()
            return SimpleNamespace(status="failed", output_text="", error="最终运行失败")

    def fail_once(*args, **kwargs):
        finish_calls.append(kwargs.get("success"))
        if len(finish_calls) == 1:
            raise RuntimeError("Mesh 回执提交临时失败")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(background_task_recovery, "AgentResponsesService", FailedResume)
    monkeypatch.setattr(background_task_recovery, "_finish_recovered_session_message", fail_once)

    background_task_recovery._resume_agent_run(background_task_recovery.AgentRunRecovery(
        run_id="recoverable-run",
        user_id=int(user.id),
        surface="user",
        session_key="recovery-session",
    ))

    message = db.query(AgentMeshMessage).filter_by(message_id="recovery-message").one()
    db.refresh(message)
    assert finish_calls == [False, False]
    assert message.status == "queued"


def test_periodic_receipt_scan_reconciles_after_two_attempts_fail_without_restarting_service(db, monkeypatch):
    user, run, message, _status, _raise_error = _seed_recoverable_run(db)
    run.status = "completed"
    run.checkpoint_json = json.dumps({"output_text": "审查结论已保存"})
    db.commit()
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)
    original_finish = background_task_recovery._finish_recovered_session_message
    finish_calls = 0

    def fail_first_two(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls <= 2:
            raise RuntimeError("数据库瞬时不可用")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(background_task_recovery, "_finish_recovered_session_message", fail_first_two)

    assert background_task_recovery.recover_pending_terminal_receipts_once() == 0
    db.refresh(message)
    assert message.status == "processing"
    assert finish_calls == 2

    assert background_task_recovery.recover_pending_terminal_receipts_once() == 1
    db.refresh(message)
    assert message.status == "completed"
    assert finish_calls == 3


def test_recovery_exception_does_not_update_run_from_another_session(db, monkeypatch):
    user, run, _message, _status, _raise_error = _seed_recoverable_run(db, raise_error=True)
    original_checkpoint = run.checkpoint_json
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(background_task_recovery, "SessionLocal", Session)

    class FailedResume:
        def __init__(self, session, owner, *, surface, session_key):
            self.session = session

        async def resume(self, *, run_id, action):
            raise RuntimeError("恢复执行异常")

    monkeypatch.setattr(background_task_recovery, "AgentResponsesService", FailedResume)
    background_task_recovery._resume_agent_run(background_task_recovery.AgentRunRecovery(
        run_id="recoverable-run",
        user_id=int(user.id),
        surface="user",
        session_key="different-session",
    ))

    db.refresh(run)
    message = db.query(AgentMeshMessage).filter_by(message_id="recovery-message").one()
    db.refresh(message)
    assert run.checkpoint_json == original_checkpoint
    assert message.status == "processing"
