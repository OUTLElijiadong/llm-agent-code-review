"""动态子 Agent 团队持久化队列消费者。"""

from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.permission_codes import PermissionCode
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamTask
from app.models.user import User
from app.services import agent_team_service, rbac_service
from app.services.ai_usage_context import model_attribution, usage_context

_scheduler = None


def _candidate_teams(db: Session, limit: int) -> list[int]:
    rows = (
        db.query(AgentTeam.id)
        .filter(AgentTeam.status.in_(("queued", "running", "verifying")))
        .order_by(AgentTeam.priority.desc(), AgentTeam.create_time.asc(), AgentTeam.id.asc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    return [int(row[0]) for row in rows]


def _task_message(team: AgentTeam, claimed: dict[str, Any]) -> dict[str, Any]:
    raw_input = claimed.get("input") if isinstance(claimed.get("input"), dict) else {}
    request_message_id = str(claimed.get("request_message_id") or "")
    return {
        "message_id": request_message_id or f"team-task-{claimed['task_id']}-{claimed['attempt_count']}",
        "user_id": int(team.user_id),
        "trace_id": team.trace_id,
        "correlation_id": f"team:{team.id}:task:{claimed['task_id']}",
        "causation_id": "",
        "sent_from": f"session:{team.surface}:{team.session_key}",
        "send_to": claimed["address"],
        "message_type": "task.request",
        "payload": {
            **raw_input,
            "instructions": claimed.get("instructions") or "",
            "title": claimed.get("title") or "",
            "dependency_context": claimed.get("dependency_context") or {},
            "_agent_team": {
                "team_id": int(team.id),
                "task_id": int(claimed["task_id"]),
                "member_id": int(claimed["member_id"]),
                "attempt": int(claimed.get("attempt_count") or 0),
                "lease_token": str(claimed.get("lease_token") or ""),
                "request_message_id": request_message_id,
                "member_snapshot": claimed.get("member_snapshot") or {},
            },
        },
        "context": {
            "team_id": int(team.id),
            "agent_team_task_id": int(claimed["task_id"]),
            "member_id": int(claimed["member_id"]),
            "source_revision_id": raw_input.get("source_revision_id"),
            "run_id": team.trace_id,
            "attempt": int(claimed.get("attempt_count") or 0),
            "lease_token": str(claimed.get("lease_token") or ""),
        },
    }


def _owner_access_error(db: Session, owner_id: int, task_input: dict | None = None, *,
                        team_id: int | None = None, task_id: int | None = None) -> str:
    """用新事务观察撤权或停用；旧 Worker 的只读快照不能授权执行或回传。"""

    with Session(bind=db.get_bind(), autoflush=False) as access_db:
        owner = access_db.get(User, int(owner_id))
        if owner is None:
            return "团队所属账户不存在"
        if int(owner.status or 0) != 1:
            return "账户已停用或删除，团队任务已阻断"
        if not rbac_service.check_permission(access_db, int(owner.id), PermissionCode.AGENT_CHAT):
            return "账户的 agent:chat 权限已撤销，团队任务已阻断"
        from app.core.exceptions import AppError
        from app.services.project_member_service import require_scoped_project_execution
        try:
            require_scoped_project_execution(access_db, owner, task_input or {})
            if task_id is not None:
                task = access_db.get(AgentTeamTask, task_id, populate_existing=True)
                team = access_db.get(AgentTeam, team_id, populate_existing=True) if team_id is not None else None
                if task is None or team is None or task.team_id != team.id or team.user_id != owner.id:
                    return "团队任务资源范围已失效，任务已阻断"
                agent_team_service.require_task_project_execution(access_db, owner, task)
        except AppError:
            return "项目执行资格已撤销或资源范围已变化，团队任务已阻断"
    return ""


def _execute_claimed(team_id: int, claimed: dict[str, Any]) -> dict[str, bool]:
    """在独立 DB Session 中执行单个已获租约的任务。"""

    db = SessionLocal()
    try:
        try:
            team, _task, member = agent_team_service.require_active_task_lease(
                db, team_id=team_id, task_id=claimed["task_id"], lease_token=claimed["lease_token"],
            )
            if int(member.id) != int(claimed["member_id"]) or member.address != claimed["address"]:
                raise agent_team_service.AgentTeamLeaseError("团队成员声明与持久化任务不匹配")
        except agent_team_service.AgentTeamError as exc:
            db.rollback()
            logger.info("[agent-team-dispatcher] skip inactive team={} task={}: {}", team_id, claimed["task_id"], exc)
            return {"success": False}
        access_error = _owner_access_error(db, int(team.user_id), agent_team_service._unjson(_task.input_json, {}),
                                           team_id=int(team.id), task_id=int(_task.id))
        if access_error:
            try:
                agent_team_service.complete_task(
                    db,
                    team_id,
                    claimed["task_id"],
                    lease_token=claimed["lease_token"],
                    result={"status": "blocked", "summary": access_error, "retryable": False},
                    success=False,
                    error=access_error,
                )
            except agent_team_service.AgentTeamError:
                db.rollback()
            return {"success": False}
        user = db.get(User, int(team.user_id))
        try:
            from app.services import agent_supervisor_service

            task_input = claimed.get("input") if isinstance(claimed.get("input"), dict) else None
            if task_input is None:
                try:
                    task_input = json.loads(_task.input_json or "{}")
                except (TypeError, ValueError):
                    task_input = {}
            task_key = str(claimed.get("task_key") or _task.task_key)
            task_title = str(claimed.get("title") or _task.title)
            task_instructions = str(claimed.get("instructions") or _task.instructions)
            execution_strategy = task_input.get("_execution_strategy")
            execution_strategy = execution_strategy if isinstance(execution_strategy, dict) else {}
            strategy_instruction = str(execution_strategy.get("instruction") or "").strip()
            review_instructions = (
                f"{task_instructions}\n\n本次重试策略：{strategy_instruction}"
                if strategy_instruction else task_instructions
            )
            supervisor_review = agent_supervisor_service.review_team_task(
                address=str(claimed["address"]),
                task_key=task_key,
                title=task_title,
                instructions=review_instructions,
                task_input=task_input,
            )
            plan_events = (
                db.query(AgentTeamEvent)
                .filter(
                    AgentTeamEvent.team_id == int(team.id),
                    AgentTeamEvent.event_type.in_((
                        "supervisor.plan_reviewed",
                        "supervisor.retry_reauthorized",
                    )),
                )
                .order_by(AgentTeamEvent.id.asc())
                .all()
            )
            authorized_fingerprints: set[str] = set()
            for plan_event in plan_events:
                try:
                    plan_detail = json.loads(plan_event.detail_json or "{}")
                except (TypeError, ValueError):
                    continue
                if plan_detail.get("confirmed_by_user_id") != int(team.user_id):
                    continue
                authorized_fingerprints.update(
                    str(item) for item in plan_detail.get("authorized_high_risk_fingerprints") or [] if item
                )
            fingerprint = agent_supervisor_service.task_fingerprint(
                {
                    "task_key": task_key,
                    "member_key": str(member.member_key),
                    "title": task_title,
                    "instructions": task_instructions,
                    "input": task_input,
                },
                str(claimed["address"]),
            )
            high_risk_authorized = fingerprint in authorized_fingerprints
            review_data = agent_supervisor_service.public_review(supervisor_review)
            agent_team_service.record_supervisor_task_review(
                db,
                team_id=int(team.id),
                task_id=int(claimed["task_id"]),
                phase="before",
                review=review_data,
                detail={"attempt": int(claimed.get("attempt_count") or 0)},
            )
            if supervisor_review.needs_confirmation and not high_risk_authorized:
                blocked = {
                    "status": "blocked",
                    "summary": "监督子 Agent 要求当前账号确认该高风险任务；任务未执行",
                    "errors": [{
                        "code": "supervisor_confirmation_required",
                        "risk_level": supervisor_review.risk_level,
                    }],
                    "next_action": {"confirm_with_current_user": True},
                    "retryable": False,
                }
                agent_team_service.complete_task(
                    db, team_id, claimed["task_id"], lease_token=claimed["lease_token"],
                    result=blocked, success=False, error=blocked["summary"],
                )
                return {"success": False}

            # 只有持有团队租约的内部调度链可执行受治理沙箱 Agent。
            from app.services.agent_mesh_dispatcher import _handle

            fields = {
                **model_attribution(team),
                "agent_team_id": int(team.id),
                "agent_team_task_id": int(claimed["task_id"]),
                "agent_execution_event_id": claimed.get("execution_event_id"),
            }
            with usage_context(int(user.id), fields, db=db):
                _, result = _handle(
                    db,
                    user,
                    claimed["address"],
                    _task_message(team, claimed),
                    trusted_team_execution=True,
                )
            access_error = _owner_access_error(db, int(team.user_id), agent_team_service._unjson(_task.input_json, {}),
                                           team_id=int(team.id), task_id=int(_task.id))
            if access_error:
                result = {"status": "blocked", "summary": access_error, "retryable": False}
            result_review = agent_supervisor_service.review_task_result(result)
            agent_team_service.record_supervisor_task_review(
                db,
                team_id=int(team.id),
                task_id=int(claimed["task_id"]),
                phase="after",
                review=agent_supervisor_service.public_review(result_review),
                detail={
                    "reported_status": str(result.get("status") or ""),
                    "evidence_count": len(result.get("evidence") or [])
                    if isinstance(result.get("evidence"), list) else None,
                    "error_count": len(result.get("errors") or [])
                    if isinstance(result.get("errors"), list) else None,
                },
            )
            if result_review.needs_confirmation and str(result.get("status") or "") == "completed":
                result = {
                    **result,
                    "status": "blocked",
                    "summary": "监督子 Agent 未能验证完成结果的证据结构；请复核后再继续",
                    "errors": [*(result.get("errors") or []), {
                        "code": "supervisor_result_review_required",
                        "reason": result_review.reason,
                    }],
                    "retryable": False,
                }
            success = str(result.get("status") or "") == "completed"
            agent_team_service.complete_task(
                db,
                team_id,
                claimed["task_id"],
                lease_token=claimed["lease_token"],
                result=result,
                success=success,
                error=str(result.get("summary") or "任务执行未完成") if not success else "",
            )
            return {"success": success}
        except agent_team_service.AgentTeamError as exc:
            db.rollback()
            logger.warning(
                "[agent-team-dispatcher] team={} task={} state error: {}", team_id, claimed["task_id"], exc
            )
            return {"success": False}
        except Exception as exc:  # noqa: BLE001 - 单任务失败不能阻塞队列
            db.rollback()
            try:
                agent_team_service.complete_task(
                    db,
                    team_id,
                    claimed["task_id"],
                    lease_token=claimed["lease_token"],
                    result={
                        "status": "failed",
                        "summary": str(exc)[:500],
                        "errors": [{"code": "unhandled_dispatch_error"}],
                        # 未分类异常可能发生在外部副作用之后；原样重派可能重复执行。
                        "retryable": False,
                    },
                    success=False,
                    error=str(exc),
                )
            except Exception:  # noqa: BLE001 - 下轮租约恢复负责兜底
                db.rollback()
            logger.warning("[agent-team-dispatcher] team={} task={} failed: {}", team_id, claimed["task_id"], exc)
            return {"success": False}
    finally:
        db.close()


def dispatch_once(*, limit: int = 20) -> dict[str, int]:
    """恢复过期租约并公平消费团队任务队列。"""

    stats = {
        "teams": 0,
        "claimed": 0,
        "completed": 0,
        "failed": 0,
        "recovered": 0,
        "expired": 0,
        "cleaned": 0,
    }
    if not settings.agent_team_enabled:
        return stats
    db = SessionLocal()
    try:
        stats["expired"] = agent_team_service.expire_due_teams(db)
        stats["recovered"] = agent_team_service.recover_expired_leases(db)
        stats["cleaned"] = agent_team_service.cleanup_terminal_team_resources(db)
        team_ids = _candidate_teams(db, limit)
        stats["teams"] = len(team_ids)
        worker_count = max(1, min(int(settings.agent_team_max_active_children), 32))
        # 全量沙箱验证可等待到 agent_full_validation_wait_seconds；租约必须覆盖
        # 这段时间和提交余量，否则恢复器会把仍在运行的任务重复入队。
        effective_lease_seconds = max(
            int(settings.agent_team_task_lease_seconds),
            int(settings.agent_full_validation_wait_seconds) + 60,
        )
        remaining_claims = max(1, min(int(limit), 100)) * worker_count
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="agent-team") as executor:
            future_map = {}
            team_cursor = 0
            while remaining_claims > 0 or future_map:
                # 仅补满空闲槽；每完成一个 Future 就重新读取依赖并立即补槽，
                # 不等待本批次/波次里其他较慢的任务结束。
                while remaining_claims > 0 and len(future_map) < worker_count and team_ids:
                    claimed_item = None
                    team_id = 0
                    # 按团队轮转，保持多团队公平；若当前均无可运行节点则等待
                    # 任一在途节点完成后再刷新其提交结果并重试领取。
                    for _ in range(len(team_ids)):
                        team_id = team_ids[team_cursor]
                        team_cursor = (team_cursor + 1) % len(team_ids)
                        claimed_item = agent_team_service.claim_next_task(
                            db,
                            team_id,
                            lease_seconds=effective_lease_seconds,
                        )
                        if claimed_item is not None:
                            break
                    if claimed_item is None:
                        break
                    future = executor.submit(_execute_claimed, team_id, claimed_item)
                    future_map[future] = (team_id, claimed_item["task_id"])
                    stats["claimed"] += 1
                    remaining_claims -= 1

                if not future_map:
                    break
                completed, _ = wait(tuple(future_map), return_when=FIRST_COMPLETED)
                for future in completed:
                    team_id, task_id = future_map.pop(future)
                    try:
                        outcome = future.result()
                        stats["completed" if outcome.get("success") else "failed"] += 1
                    except Exception as exc:  # noqa: BLE001 - worker 崩溃由租约恢复兜底
                        logger.warning(
                            "[agent-team-dispatcher] team={} task={} worker crashed: {}", team_id, task_id, exc
                        )
                        stats["failed"] += 1
                # worker 使用独立 Session 提交；主 Session 在下一次 claim 前清理
                # 快照，让依赖更新及其他进程的队列变化可见。
                db.rollback()
                db.expire_all()
        return stats
    finally:
        db.close()


def start_agent_team_dispatcher() -> None:
    global _scheduler
    if not settings.agent_team_enabled:
        logger.info("[agent-team-dispatcher] disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        dispatch_once,
        "interval",
        id="agent-team-dispatch",
        seconds=max(1, int(settings.agent_team_dispatch_interval_seconds)),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[agent-team-dispatcher] started")


def stop_agent_team_dispatcher() -> None:
    global _scheduler
    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        logger.info("[agent-team-dispatcher] stopped")
