"""报告留存只清理到期终态负载，源码/审计及派生业务不受影响。"""
from datetime import datetime, timedelta

from app.models.pentest import PentestEngagement
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.roundtable import RoundtableSession
from app.services import report_retention_service as service

NOW = datetime(2026, 10, 23, 12)


def _task(db, *, status="success", days=31, kind="standard"):
    old = NOW - timedelta(days=days)
    task = ReviewTask(user_id=1, project_id=1, review_type=kind, status=status,
                      create_time=old, update_time=old, end_time=old,
                      coverage={"large": "old"}, summary="old report", rules_snapshot={"old": True})
    db.add(task)
    db.flush()
    db.add_all([
        ReviewReport(task_id=task.id, user_id=1, content_json={"body": "old"}, create_time=old),
        ReviewIssue(task_id=task.id, issue_type="security", severity="high", description="old"),
        ReviewTaskFile(task_id=task.id, file_id=1, file_snapshot={"content": "old"}),
    ])
    db.commit()
    return task


def test_expired_payload_is_removed_and_tombstone_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(service.settings, "report_success_retention_days", 30)
    task = _task(db)
    before = service.sweep_expired_reports(db, now=NOW, dry_run=True)
    assert before["eligible"] == 1 and before["purged"] == 0
    assert db.query(ReviewReport).count() == 1
    result = service.sweep_expired_reports(db, now=NOW)
    db.refresh(task)
    assert result["purged"] == result["reports"] == result["issues"] == result["snapshots"] == 1
    assert task.status == "deleted" and task.execution_token
    assert task.coverage["previous_status"] == "success"
    assert task.summary is None and task.rules_snapshot is None
    assert service.sweep_expired_reports(db, now=NOW)["purged"] == 0


def test_active_recent_special_and_parent_linked_reports_are_protected(db, monkeypatch):
    monkeypatch.setattr(service.settings, "report_success_retention_days", 30)
    monkeypatch.setattr(service.settings, "report_failed_retention_days", 7)
    tasks = [
        _task(db, status="running", days=60), _task(db, status="failed", days=6),
        _task(db, days=30), _task(db, days=60, kind="sandbox_test"),
        _task(db, days=60, kind="pentest"), _task(db, days=60, kind="unknown"),
        _task(db, days=60), _task(db, days=60),
    ]
    db.add(RoundtableSession(session_id="active", owner_user_id=1, task_id=tasks[-2].id,
                            status="active", file_name="main.py", agents=[], progress={},
                            created_at=NOW-timedelta(days=60), updated_at=NOW-timedelta(days=60)))
    db.add(PentestEngagement(public_id="parent", user_id=1, project_id=1, target_type="web",
                            status="running", report_task_id=tasks[-1].id))
    db.commit()
    assert service.sweep_expired_reports(db, now=NOW)["purged"] == 0
    assert db.query(ReviewReport).count() == len(tasks)


def test_failed_policy_batch_boundary_and_disabled_policy(db, monkeypatch):
    monkeypatch.setattr(service.settings, "report_success_retention_days", 0)
    monkeypatch.setattr(service.settings, "report_failed_retention_days", 7)
    monkeypatch.setattr(service.settings, "report_retention_batch_size", 1)
    success = _task(db, days=90)
    failed = _task(db, status="failed", days=8)
    cancelled = _task(db, status="cancelled", days=9)
    assert service.sweep_expired_reports(db, now=NOW)["purged"] == 1
    assert service.sweep_expired_reports(db, now=NOW)["purged"] == 1
    db.refresh(success)
    db.refresh(failed)
    db.refresh(cancelled)
    assert success.status == "success"
    assert failed.status == cancelled.status == "deleted"
    monkeypatch.setattr(service.settings, "report_failed_retention_days", 0)
    assert service.sweep_expired_reports(db, now=NOW)["candidates"] == 0
