"""把受信团队任务连接到正式审查，复用冻结输入、并行审查及报告落库链路。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permission_codes import PermissionCode
from app.models.agent_team import AgentTeam
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services import agent_team_service, review_service
from app.services.ai_usage_context import current_attribution, model_attribution, usage_context


def cancel_team_reviews(db: Session, *, team_id: int, reason: str, task_id: int | None = None) -> int:
    """仅停止有持久化团队归因的运行审查；换发 token 阻止旧 Worker 写入。"""
    team = db.get(AgentTeam, team_id)
    if team is None:
        return 0
    query = db.query(ReviewTask).filter(
        ReviewTask.agent_team_id == team_id, ReviewTask.user_id == team.user_id, ReviewTask.status == "running",
    )
    if task_id is not None:
        query = query.filter(ReviewTask.agent_team_task_id == task_id)
    rows = query.populate_existing().with_for_update().all()
    for review in rows:
        review.status = "cancelled"
        review.execution_token = uuid.uuid4().hex
        review.end_time = datetime.now(timezone.utc)
        review.error_message = reason[:500]
        review.coverage = {**(review.coverage or {}), "stage": "cancelled", "reason": reason[:500]}
        review_service._update_issue_counts(db, review)
        review_service._update_duration(review)
    if rows:
        db.commit()
    return len(rows)


def _require_actor(db: Session, user_id: int, project_id: int) -> User:
    from app.services.project_member_service import require_project_execution
    from app.services.rbac_service import check_permission

    user = db.get(User, user_id, populate_existing=True)
    if user is None or int(user.status or 0) != 1:
        raise agent_team_service.AgentTeamAccessError("账户已停用或删除，正式审查已停止")
    if not check_permission(db, user_id, PermissionCode.REVIEW_START):
        raise agent_team_service.AgentTeamAccessError("当前账户已无发起审查的权限")
    require_project_execution(db, project_id, user)
    return user


def _review_result(
    review: ReviewTask, *, db: Session, status: str | None = None, summary: str = "",
) -> dict[str, Any]:
    coverage = review.coverage if isinstance(review.coverage, dict) else {}
    complete = (
        review.status == "success" and int(review.total_files or 0) > 0
        and review.processed_files == review.total_files and coverage.get("stage") == "complete"
    )
    state = status or ("completed" if complete else "failed")
    query = db.query(
        ReviewIssue.id, ReviewIssue.file_id, ReviewIssue.file_name, ReviewIssue.line_number,
        ReviewIssue.title, ReviewIssue.severity, ReviewIssue.issue_type,
    ).filter(ReviewIssue.task_id == review.id)
    finding_count_total = query.count()
    findings = [
        {"issue_id": row.id, "project_id": int(review.project_id), "task_id": int(review.id),
         "file_id": row.file_id, "file_name": row.file_name, "line_number": row.line_number,
         "title": row.title, "severity": row.severity, "issue_type": row.issue_type}
        for row in query.order_by(ReviewIssue.id.asc()).all()
    ]
    detail = {
        "task_id": int(review.id), "project_id": int(review.project_id), "status": review.status,
        "review_type": review.review_type, "coverage": coverage, "total_files": review.total_files,
        "processed_files": review.processed_files, "total_issues": review.total_issues,
        "task_url": f"/reviews/{review.id}", "report_url": f"/reports/{review.id}" if complete else None,
    }
    return {
        "status": state, "task_id": int(review.id), "project_id": int(review.project_id), "coverage": coverage,
        "findings": findings, "finding_count_total": finding_count_total,
        "findings_truncated": False,
        "summary": summary or (review.summary if complete else review.error_message)
        or ("正式并行审查与证据聚合已完成" if complete else "正式审查未完整完成，请查看任务覆盖记录"),
        "evidence": [{"source": "review_task", "data": detail}],
        "artifacts": [{"type": "review_task", **detail}],
        "errors": [] if complete and state == "completed" else [{"code": "review_incomplete", "status": state}],
        "retryable": False,
    }


def run_team_review(
    db: Session, user: User, *, team_id: int, task_id: int, lease_token: str, project_id: int,
    file_ids: list[int] | None = None, review_type: str = "full", task_name: str = "",
) -> dict[str, Any]:
    """受信 Handler 专用：在有效团队租约下创建一次正式审查，并等待真实终态。"""
    user_id = int(user.id)
    team, team_task, member = agent_team_service.require_active_task_lease(
        db, team_id=team_id, task_id=task_id, lease_token=lease_token, owner_user_id=user_id, lock=True,
    )
    raw = json.loads(team_task.input_json or "{}")
    if member.address != "agent:review_orchestrator" or raw.get("operation") != "run_review":
        raise agent_team_service.AgentTeamAccessError("团队任务未授权执行正式审查")
    texts = "\n".join((team.objective or "", team_task.instructions or ""))
    if raw.get("execution_mode") == "read_only" or any(
        marker in texts for marker in agent_team_service._READONLY_TEAM_MARKERS
    ):
        raise agent_team_service.AgentTeamAccessError("只读团队不能发起新的正式审查")
    if (raw.get("project_id") != project_id or raw.get("review_type", "full") != review_type
            or raw.get("file_ids") != file_ids):
        raise agent_team_service.AgentTeamAccessError("审查参数与已授权的团队任务不一致")
    if review_type not in {"full", "security"} or raw.get("source_revision_id") is not None:
        raise agent_team_service.AgentTeamValidationError("团队正式审查必须使用代码中心的 full 或 security 审查")
    actor = _require_actor(db, user_id, project_id)
    review = db.query(ReviewTask).filter(
        ReviewTask.agent_team_id == team_id, ReviewTask.agent_team_task_id == task_id,
        ReviewTask.user_id == user_id,
    ).order_by(ReviewTask.id.asc()).first()
    if review is not None and (review.project_id != project_id or review.review_type != review_type):
        raise agent_team_service.AgentTeamStateError("团队任务已关联不同范围的正式审查")
    if review is None:
        selected_ids = file_ids
        exclusions = []
        if selected_ids is None:
            from app.services.review_input_service import select_project_review_inputs

            selected_ids, exclusions = select_project_review_inputs(db, project_id)
        if not selected_ids:
            raise agent_team_service.AgentTeamValidationError("项目没有可审查的代码文件，请先导入代码中心")
        fields = {**model_attribution(team), **current_attribution(user_id),
                  "agent_team_id": team_id, "agent_team_task_id": task_id}
        with usage_context(user_id, fields, db=db):
            review = review_service.start(db, actor, ReviewStartIn(
                project_id=project_id, file_ids=selected_ids, review_type=review_type,
                task_name=task_name or team_task.title,
            ), input_exclusions=exclusions)
    review_id = int(review.id)
    db.commit()  # 释放创建/复用时的团队行锁，等待期间不占事务。
    deadline = time.monotonic() + int(settings.agent_full_validation_wait_seconds)
    while True:
        db.rollback()
        db.expire_all()
        review = db.get(ReviewTask, review_id)
        if review is None:
            raise agent_team_service.AgentTeamStateError("正式审查任务已不存在")
        try:
            agent_team_service.require_active_task_lease(
                db, team_id=team_id, task_id=task_id, lease_token=lease_token, owner_user_id=user_id,
            )
        except agent_team_service.AgentTeamLeaseError:
            db.rollback()
            current_team = db.get(AgentTeam, team_id, populate_existing=True)
            if current_team is not None and current_team.status in {"cancelled", "expired", "failed"}:
                cancel_team_reviews(db, team_id=team_id, task_id=task_id, reason="所属团队已停止")
                db.refresh(review)
            return _review_result(review, db=db, status="cancelled", summary="团队租约已失效，当前等待者停止")
        try:
            _require_actor(db, user_id, project_id)
        except Exception:
            db.rollback()
            cancel_team_reviews(db, team_id=team_id, task_id=task_id, reason="账户或项目审查权限已失效")
            db.refresh(review)
            return _review_result(review, db=db, status="blocked", summary="账户或项目审查权限已失效")
        if review.status not in {"running", "pending"}:
            return _review_result(review, db=db)
        if time.monotonic() >= deadline:
            cancel_team_reviews(db, team_id=team_id, task_id=task_id, reason="团队等待正式审查超时，已停止执行")
            db.refresh(review)
            return _review_result(
                review, db=db, status="failed", summary="正式审查未在团队等待期限内完成，保留已取得证据",
            )
        db.rollback()
        time.sleep(1)


_ROUNDTABLE_TERMINAL = {"concluded", "partial", "failed", "cancelled", "interrupted"}


def _roundtable_result(row: Any, *, status: str | None = None, summary: str = "") -> dict[str, Any]:
    """把圆桌会话终态转换成团队任务结果；只有已结论的圆桌算完成。"""
    session_status = str(getattr(row, "status", "") or "")
    state = status or ("completed" if session_status == "concluded" else "failed")
    report_task_id = int(getattr(row, "report_task_id", 0) or getattr(row, "task_id", 0) or 0)
    detail = {
        "session_id": str(getattr(row, "session_id", "") or ""),
        "status": session_status,
        "project_id": int(getattr(row, "project_id", 0) or 0),
        "file_id": int(getattr(row, "file_id", 0) or 0),
        "file_name": str(getattr(row, "file_name", "") or ""),
        "report_task_id": report_task_id or None,
        "open_path": f"/agents?discuss_session={getattr(row, 'session_id', '')}",
        "report_url": f"/reports/{report_task_id}" if report_task_id and state == "completed" else None,
    }
    default_summary = {
        "concluded": "圆桌讨论已形成结论",
        "partial": "圆桌讨论只形成部分结论",
        "cancelled": "圆桌讨论已停止",
        "interrupted": "圆桌讨论被中断",
    }.get(session_status, "圆桌讨论未完成")
    return {
        "status": state,
        "summary": summary or default_summary,
        "evidence": [{"source": "roundtable_session", "data": detail}],
        "artifacts": [{"type": "roundtable_session", **detail}],
        "errors": [] if state == "completed" else [{"code": "roundtable_incomplete", "status": session_status}],
        "retryable": False,
    }


def run_team_roundtable(
    db: Session, user: User, *, team_id: int, task_id: int, lease_token: str,
    project_id: int, file_id: int, temporary_experts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """受信 Handler 专用：在有效团队租约下启动单文件圆桌，并等待真实终态。

    圆桌复用小菱工具同一套预检（项目执行权、review:start、文件有效性），编排任务投递回
    Web 主事件循环；团队停止或租约失效时请求圆桌停止。
    """
    from app.agents.discussion_bus import DiscussionBus
    from app.api.v1.discussion import start_discussion
    from app.api.v1.ws_discussion import launch_pending_discussion_from_thread, take_pending
    from app.models.roundtable import RoundtableSession

    user_id = int(user.id)
    team, team_task, member = agent_team_service.require_active_task_lease(
        db, team_id=team_id, task_id=task_id, lease_token=lease_token, owner_user_id=user_id, lock=True,
    )
    raw = json.loads(team_task.input_json or "{}")
    if member.address != "agent:review_orchestrator" or raw.get("operation") != "start_roundtable":
        raise agent_team_service.AgentTeamAccessError("团队任务未授权启动圆桌讨论")
    if raw.get("project_id") != project_id or raw.get("file_id") != file_id:
        raise agent_team_service.AgentTeamAccessError("圆桌参数与已授权的团队任务不一致")
    texts = "\n".join((team.objective or "", team_task.instructions or ""))
    if raw.get("execution_mode") == "read_only" or any(
        marker in texts for marker in agent_team_service._READONLY_TEAM_MARKERS
    ):
        raise agent_team_service.AgentTeamAccessError("只读团队不能发起新的圆桌讨论")
    actor = _require_actor(db, user_id, project_id)

    # 同一团队任务重派时复用已经启动的圆桌，避免重复开会。
    session_id = str((raw.get("_roundtable") or {}).get("session_id") or "")
    if not session_id:
        response = start_discussion(
            project_id=project_id, file_id=file_id, review_type="full", db=db, user=actor,
            temporary_experts_json=json.dumps(temporary_experts or [], ensure_ascii=False),
        )
        session_id = str((response.data or {}).get("session_id") or "")
        pending = take_pending(session_id)
        if not session_id or pending is None:
            raise agent_team_service.AgentTeamStateError("圆桌讨论上下文创建失败")
        if not launch_pending_discussion_from_thread(pending):
            raise agent_team_service.AgentTeamStateError("圆桌会话已结束或已被停止，未启动")
        team_task.input_json = json.dumps({**raw, "_roundtable": {"session_id": session_id}}, ensure_ascii=False)
    db.commit()  # 释放团队行锁，等待期间不占事务。

    bus = DiscussionBus.instance()
    deadline = time.monotonic() + int(settings.agent_full_validation_wait_seconds)
    while True:
        db.rollback()
        db.expire_all()
        row = db.get(RoundtableSession, session_id)
        try:
            agent_team_service.require_active_task_lease(
                db, team_id=team_id, task_id=task_id, lease_token=lease_token, owner_user_id=user_id,
            )
        except agent_team_service.AgentTeamLeaseError:
            db.rollback()
            bus.request_stop(session_id, "所属团队任务已停止")
            return _roundtable_result(row, status="cancelled", summary="团队任务已停止，圆桌讨论随之停止")
        session = bus.get_session(session_id, owner_user_id=user_id)
        live_status = str(getattr(session, "status", "") or getattr(row, "status", "") or "")
        if live_status in _ROUNDTABLE_TERMINAL:
            db.expire_all()
            row = db.get(RoundtableSession, session_id) or session
            if row is not None and str(getattr(row, "status", "") or "") not in _ROUNDTABLE_TERMINAL:
                row = session
            return _roundtable_result(row)
        if time.monotonic() >= deadline:
            bus.request_stop(session_id, "团队等待圆桌讨论超时")
            return _roundtable_result(
                row or session, status="failed", summary="圆桌讨论未在团队等待期限内完成，已请求停止并保留已有发言",
            )
        db.rollback()
        time.sleep(2)
