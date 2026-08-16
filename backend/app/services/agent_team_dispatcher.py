"""动态子 Agent 团队持久化队列消费者。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.agent_team import AgentTeam
from app.models.user import User
from app.services import agent_team_service

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


def _execute_claimed(team_id: int, claimed: dict[str, Any]) -> dict[str, bool]:
    """在独立 DB Session 中执行单个已获租约的任务。"""

    db = SessionLocal()
    try:
        team = db.get(AgentTeam, int(team_id))
        user = db.get(User, int(team.user_id)) if team else None
        if team is None or user is None:
            try:
                agent_team_service.complete_task(
                    db,
                    team_id,
                    claimed["task_id"],
                    lease_token=claimed["lease_token"],
                    result={"status": "blocked", "summary": "团队所属账户不存在"},
                    success=False,
                    error="团队所属账户不存在",
                )
            except agent_team_service.AgentTeamError:
                db.rollback()
            return {"success": False}
        try:
            # 只有持有团队租约的内部调度链可执行受治理沙箱 Agent。
            from app.services.agent_mesh_dispatcher import _handle

            _, result = _handle(
                db,
                user,
                claimed["address"],
                _task_message(team, claimed),
                trusted_team_execution=True,
            )
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
                    result={"status": "failed", "summary": str(exc)},
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
        task_budget = max(1, min(int(limit), 100)) * worker_count
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="agent-team") as executor:
            while task_budget > 0:
                wave: list[tuple[int, dict[str, Any]]] = []
                # 按团队轮转领取，避免单个大团队长期占满全局 worker。
                while len(wave) < min(worker_count, task_budget):
                    progressed = False
                    for team_id in team_ids:
                        if len(wave) >= min(worker_count, task_budget):
                            break
                        claimed = agent_team_service.claim_next_task(
                            db,
                            team_id,
                            lease_seconds=effective_lease_seconds,
                        )
                        if claimed is not None:
                            wave.append((team_id, claimed))
                            stats["claimed"] += 1
                            progressed = True
                    if not progressed:
                        break
                if not wave:
                    break

                future_map = {
                    executor.submit(_execute_claimed, team_id, claimed): (team_id, claimed["task_id"])
                    for team_id, claimed in wave
                }
                for future in as_completed(future_map):
                    team_id, task_id = future_map[future]
                    try:
                        outcome = future.result()
                        stats["completed" if outcome.get("success") else "failed"] += 1
                    except Exception as exc:  # noqa: BLE001 - worker 崩溃由租约恢复兜底
                        logger.warning(
                            "[agent-team-dispatcher] team={} task={} worker crashed: {}", team_id, task_id, exc
                        )
                        stats["failed"] += 1
                task_budget -= len(wave)
                # 使后续波次看到 worker Session 已提交的依赖结果。
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
