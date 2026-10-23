"""普通审查报告的有界留存清理；任务元数据、源码和审计记录保留。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.pentest import PentestEngagement
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.roundtable import RoundtableSession

# 派生报告必须由其父业务维护；未知类型也默认保留。
_REVIEW_TYPES = ("quick", "standard", "full", "security", "discuss", "repo_discuss")
_SESSION_TERMINAL = ("concluded", "partial", "failed", "cancelled")
_scheduler = None


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _cutoff(status: str, now: datetime) -> datetime | None:
    days = (
        settings.report_success_retention_days
        if status == "success"
        else settings.report_failed_retention_days
    )
    return now - timedelta(days=days) if days > 0 else None


def sweep_expired_reports(
    db: Session, *, dry_run: bool = False, now: datetime | None = None,
) -> dict[str, int]:
    """每批最多处理配置数量的终态报告，删除前重新锁定并核验父业务及时间。

    只物理回收报告正文、问题和审查文件快照；任务留作 deleted 墓碑，换发
    execution_token 阻止迟到 worker 写回。不会删除 CodeFile 或任何审计表。
    """
    now = _naive_utc(now or datetime.now(timezone.utc))
    result = {"candidates": 0, "eligible": 0, "purged": 0, "reports": 0, "issues": 0, "snapshots": 0, "skipped": 0}
    ended = func.coalesce(ReviewTask.end_time, ReviewTask.update_time, ReviewTask.create_time)
    policies = []
    for statuses in (("success",), ("failed", "cancelled", "expired", "partial", "blocked")):
        cutoff = _cutoff(statuses[0], now)
        if cutoff is not None:
            protected = db.query(RoundtableSession.session_id).filter(
                or_(RoundtableSession.task_id == ReviewTask.id, RoundtableSession.report_task_id == ReviewTask.id),
                or_(
                    RoundtableSession.status.notin_(_SESSION_TERMINAL), RoundtableSession.updated_at >= cutoff,
                    RoundtableSession.progress["phase"].as_string() == "interrupted",
                ),
            ).exists()
            policies.append(and_(
                ReviewTask.status.in_(statuses), ended < cutoff, ReviewTask.update_time < cutoff, ~protected,
            ))
    if not policies:
        return result

    # 先投影 ID，避免扫描时载入大 JSON；专属生命周期委托关联的任务始终排除。
    parent_exists = db.query(PentestEngagement.id).filter(
        PentestEngagement.report_task_id == ReviewTask.id,
    ).exists()
    ids = [row[0] for row in db.query(ReviewTask.id).filter(
        ReviewTask.review_type.in_(_REVIEW_TYPES), or_(*policies), ~parent_exists,
        ReviewTask.agent_team_id.is_(None), ReviewTask.agent_team_task_id.is_(None),
        func.coalesce(ReviewTask.coverage["stage"].as_string(), "") != "interrupted",
    ).order_by(ended.asc(), ReviewTask.id.asc()).limit(settings.report_retention_batch_size).all()]
    result["candidates"] = len(ids)
    for task_id in ids:
        try:
            # 与追问持久化复用会话行锁，最近访问/追问及活动会话都保留。
            sessions = db.query(RoundtableSession).filter(or_(
                RoundtableSession.task_id == task_id,
                RoundtableSession.report_task_id == task_id,
            )).order_by(RoundtableSession.session_id).with_for_update().populate_existing().all()
            task = db.query(ReviewTask).filter_by(id=task_id).with_for_update().populate_existing().one_or_none()
            cutoff = _cutoff(task.status, now) if task is not None else None
            terminal = task is not None and task.status in {
                "success", "failed", "cancelled", "expired", "partial", "blocked",
            }
            ended_at = (task.end_time or task.update_time or task.create_time) if task is not None else None
            protected_session = any(
                row.status not in _SESSION_TERMINAL
                or (row.progress or {}).get("phase") == "interrupted"
                or _naive_utc(row.updated_at) >= cutoff
                for row in sessions
            ) if cutoff is not None else True
            if (
                not terminal or cutoff is None or ended_at is None
                or _naive_utc(ended_at) >= cutoff
                or _naive_utc(task.update_time) >= cutoff
                or task.review_type not in _REVIEW_TYPES or protected_session
                or task.agent_team_id is not None or task.agent_team_task_id is not None
                or (task.coverage or {}).get("stage") == "interrupted"
                or db.query(PentestEngagement.id).filter_by(report_task_id=task_id).first() is not None
            ):
                result["skipped"] += 1
                db.rollback()
                continue
            result["eligible"] += 1
            if dry_run:
                db.rollback()
                continue
            counts = {
                "reports": db.query(ReviewReport).filter_by(task_id=task_id).delete(synchronize_session=False),
                "issues": db.query(ReviewIssue).filter_by(task_id=task_id).delete(synchronize_session=False),
                "snapshots": db.query(ReviewTaskFile).filter_by(task_id=task_id).delete(synchronize_session=False),
            }
            previous_status = task.status
            task.status = "deleted"
            task.execution_token = uuid.uuid4().hex
            task.summary = None
            task.rules_snapshot = None
            task.score_breakdown = None
            task.coverage = {
                "stage": "deleted", "retention_cleanup": True, "previous_status": previous_status,
                "purged_at": now.replace(tzinfo=timezone.utc).isoformat(),
            }
            db.commit()
            result["purged"] += 1
            for key, count in counts.items():
                result[key] += count
            logger.info("[report-retention] purged task={} previous_status={}", task_id, previous_status)
        except Exception:  # noqa: BLE001 - 单条失败回滚，下轮继续，不影响业务
            db.rollback()
            result["skipped"] += 1
            logger.exception("[report-retention] purge failed task={}", task_id)
    return result


def _sweep_once() -> None:
    with SessionLocal() as db:
        try:
            logger.info("[report-retention] sweep result={}", sweep_expired_reports(db))
        except Exception:  # noqa: BLE001 - 维护失败下轮重试
            db.rollback()
            logger.exception("[report-retention] sweep failed")


def start_report_retention_sweeper() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _sweep_once, "interval", seconds=3600, id="review-report-retention",
        max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    _scheduler.start()
    logger.info(
        "[report-retention] started success={}d failed={}d batch={} interval=3600s",
        settings.report_success_retention_days, settings.report_failed_retention_days,
        settings.report_retention_batch_size,
    )


def stop_report_retention_sweeper() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
