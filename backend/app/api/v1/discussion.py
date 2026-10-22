"""讨论模式审查 REST API (v2.3 M7)

（POST /discuss/start 已在 4.1.20 下线，圆桌由小菱团队成员 review_orchestrator 发起）
     → 返回 session_id → 前端连接 WebSocket → 讨论开始
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case
from sqlalchemy.orm import Session, sessionmaker

from app.agents.discussion_bus import DiscussionBus, utc_timestamp, visible_roundtable_progress
from app.api.v1.ws_discussion import (
    _fail_pending_discussion,
    _publish_cancelled_terminal,
    cancel_pending,
    purge_stale_pending,
    register_pending,
)
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.roundtable import RoundtableSession, RoundtableTurn
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.discussion import RoundtableTemporaryExpert
from app.services import rule_service
from app.services.ai_usage_context import current_attribution
from app.services.project_member_service import require_project_execution
from app.services.review_input_service import validate_review_input
from app.services.roundtable_report_lifecycle import cancel_running_discussion_report

router = APIRouter()


def _bus_for_db(db: Session) -> DiscussionBus:
    """使用本次请求的数据库绑定启用会话账本，隔离 SQLite 测试环境。"""
    bus = DiscussionBus.instance()
    if isinstance(bus, DiscussionBus):
        bus.enable_persistence(sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    return bus


def _session_data(db: Session, row: RoundtableSession) -> dict:
    """只返回前端重开所需的会话元数据，不在列表泄露聊天正文。"""
    progress = visible_roundtable_progress(db, row)
    return {
        "session_id": row.session_id,
        "ws_url": f"/api/ws/discuss/{row.session_id}",
        "file_name": row.file_name,
        "status": row.status,
        "max_rounds": row.max_rounds,
        "report_task_id": row.report_task_id,
        "agents": list(row.agents or []),
        "progress": progress,
        "turn_count": row.last_turn_seq,
        "continued_from_session_id": row.continued_from_session_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "followup_until": (
            utc_timestamp(row.closed_at) + 300
            if row.status == "concluded" and row.closed_at and row.report_task_id
            and "followup_start_seq" in progress
            and progress.get("phase") == "completed" else 0
        ),
    }


@router.get("/discuss/sessions", response_model=Resp[dict])
def list_discussions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """当前账号的圆桌按运行中优先、更新时间倒序分页。"""
    bus = _bus_for_db(db)
    purge_stale_pending()
    active_first = case((RoundtableSession.status.in_(["active", "paused"]), 0), else_=1)
    rows = db.query(RoundtableSession).filter(
        RoundtableSession.owner_user_id == int(user.id),
    ).order_by(
        active_first, RoundtableSession.updated_at.desc(), RoundtableSession.session_id.desc(),
    ).offset(offset).limit(limit + 1).all()
    for row in rows[:limit]:
        if row.status in {"active", "paused"}:
            bus.get_session(row.session_id, owner_user_id=int(user.id))
            db.refresh(row)
    return Resp(data={
        "items": [_session_data(db, row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    })


@router.get("/discuss/sessions/{session_id}", response_model=Resp[dict])
def get_discussion(
    session_id: str,
    limit: int = Query(100, ge=1, le=100),
    before_seq: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """读取当前账号圆桌元数据与有界发言，旧发言用 before_seq 翻页。"""
    bus = _bus_for_db(db)
    purge_stale_pending()
    row = db.query(RoundtableSession).filter(
        RoundtableSession.session_id == session_id,
        RoundtableSession.owner_user_id == int(user.id),
    ).one_or_none()
    if row is None:
        raise NotFoundError("圆桌讨论不存在", code=40400)
    bus.get_session(session_id, owner_user_id=int(user.id))
    db.refresh(row)
    query = db.query(RoundtableTurn).filter(
        RoundtableTurn.session_id == session_id,
        RoundtableTurn.owner_user_id == int(user.id),
    )
    if before_seq is not None:
        query = query.filter(RoundtableTurn.seq < before_seq)
    page = query.order_by(RoundtableTurn.seq.desc()).limit(limit + 1).all()
    result = _session_data(db, row)
    result["turns"] = [dict(item.turn) for item in reversed(page[:limit])]
    result["has_earlier"] = len(page) > limit
    result["next_before_seq"] = page[limit - 1].seq if len(page) > limit else None
    return Resp(data=result)


@router.post("/discuss/sessions/{session_id}/stop", response_model=Resp[dict])
async def stop_discussion(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """通过已鉴权 REST 通道可靠停止本账号圆桌，断开的 WebSocket 不影响停止。"""
    bus = _bus_for_db(db)
    row = db.query(RoundtableSession).filter(
        RoundtableSession.session_id == session_id,
        RoundtableSession.owner_user_id == int(user.id),
    ).one_or_none()
    if row is None:
        raise NotFoundError("圆桌讨论不存在", code=40400)

    session = bus.get_session(session_id, owner_user_id=int(user.id))
    if session is None:
        raise NotFoundError("圆桌讨论不存在", code=40400)

    terminal_states = {"concluded", "partial", "failed", "cancelled", "interrupted"}
    stop_requested = False
    if session.status not in terminal_states:
        stop_reason = "圆桌讨论已由用户停止"
        report_task_id = int(
            session.report_task_id or session.task_id
            or row.report_task_id or row.task_id or 0
        )
        if report_task_id:
            report_disposition = cancel_running_discussion_report(
                db,
                task_id=report_task_id,
                owner_user_id=int(user.id),
            )
            if report_disposition == "terminal":
                # 报告收尾已先取得报告行锁并提交；不要再把对应圆桌改为
                # cancelled，也不要取消编排任务。它会按已提交报告发布终态。
                db.refresh(row)
                session = bus.get_session(session_id, owner_user_id=int(user.id))
                status = str(session.status if session is not None else row.status)
                return Resp(data={
                    "session_id": session_id,
                    "status": status,
                    "stop_requested": False,
                    "terminal": status in terminal_states,
                    "provider_request_may_finish": False,
                })

        cancel_pending(session_id, int(user.id))
        if report_task_id:
            bus.link_report_task(session_id, report_task_id)
        stop_requested = bus.request_stop(session_id, reason=stop_reason)
        if stop_requested:
            task = bus.get_discussion_task(session_id)
            if task is not None:
                bus.cancel_discussion_task(session_id)
                await asyncio.wait({task}, timeout=2.0)
            _publish_cancelled_terminal(
                bus,
                session_id,
                error=stop_reason,
                task_id=report_task_id,
            )
        else:
            # 另一 worker 可能已提交成功/失败终态；只同步终态，不取消其任务。
            _publish_cancelled_terminal(
                bus,
                session_id,
                error=stop_reason,
                task_id=report_task_id,
            )

    db.refresh(row)
    session = bus.get_session(session_id, owner_user_id=int(user.id))
    status = str(session.status if session is not None else row.status)
    return Resp(data={
        "session_id": session_id,
        "status": status,
        "stop_requested": stop_requested,
        "terminal": status in terminal_states,
        "provider_request_may_finish": bool(stop_requested),
    })


def _gen_session_id() -> str:
    return f"disc_{uuid.uuid4().hex[:10]}"


# 4.1.20 起不再暴露为 HTTP 路由：圆桌只能由小菱的 start_roundtable_discussion 工具发起，
# 本函数保留为同一套预检，供小菱在后台复用。
def start_discussion(
    project_id: int = Query(...),
    file_id: int = Query(...),
    review_type: str = Query("full"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    origin_surface: str = "",
    origin_session_key: str = "",
    continued_from_session_id: str = "",
    continuation_context: str = "",
    temporary_experts_json: str = "",
):
    """预检并注册讨论,返回 session_id 和基本信息(仅供小菱工具内部调用)。

    小菱启动后,前端按返回的 ws_url 连接 /api/ws/discuss/{session_id} 观看实时发言,
    并可通过 WebSocket 发送 {action:"user_input",content:"..."} 参与讨论。
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    require_project_execution(db, project_id, user)
    from app.core.exceptions import ForbiddenError
    from app.core.permission_codes import PermissionCode
    from app.services.rbac_service import check_permission
    if not check_permission(db, int(user.id), PermissionCode.REVIEW_START):
        raise ForbiddenError("当前账户没有发起审查的权限", code=40300)

    code_file = db.query(CodeFile).filter(
        CodeFile.id == file_id,
        CodeFile.project_id == project_id,
        CodeFile.status == "active",
    ).first()
    if not code_file:
        raise NotFoundError("文件不存在", code=40400)
    validate_review_input(code_file)

    from app.ai.multi_agent import get_discussion_agent_profiles
    profiles = list(get_discussion_agent_profiles())
    if temporary_experts_json:
        if len(temporary_experts_json) > 8192:
            raise ValidationError("圆桌临时专家参数过长", code=40001)
        try:
            raw_temporary_experts = json.loads(temporary_experts_json)
            if not isinstance(raw_temporary_experts, list) or len(raw_temporary_experts) > 3:
                raise ValueError("最多添加 3 位临时专家")
            temporary_experts = [
                RoundtableTemporaryExpert.model_validate(item)
                for item in raw_temporary_experts
            ]
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"圆桌临时专家参数无效: {exc}", code=40001) from exc

        from app.ai.multi_agent import ReviewAgentProfile

        profiles.extend(
            ReviewAgentProfile(
                code=f"temporary_{index}",
                name=expert.name,
                focus=expert.focus,
                issue_types=tuple(expert.issue_types) or ("其他",),
                instruction=expert.instruction,
                is_custom=True,
            )
            for index, expert in enumerate(temporary_experts, start=1)
        )
    profiles = tuple(profiles)
    agent_list = [
        {
            "code": p.code,
            "name": p.name,
            "focus": p.focus,
            "temporary": p.code.startswith("temporary_"),
        }
        for p in profiles
    ]
    rules = rule_service.get_enabled_rules(
        db, user.id, language=(project.language or "").strip().lower(),
    )

    session_id = _gen_session_id()
    rounds = 2
    bus = _bus_for_db(db)
    bus.create_session(
        session_id=session_id,
        task_id=0,
        file_name=code_file.file_name,
        owner_user_id=user.id,
        max_rounds=rounds,
        project_id=project_id,
        file_id=file_id,
        review_type=review_type,
        origin_surface=origin_surface,
        origin_session_key=origin_session_key,
        continued_from_session_id=continued_from_session_id,
        agents=agent_list,
        status="pending",
    )
    try:
        session = bus.get_session(session_id)
        if session:
            session.status = "pending"
            session.stage = "pending"
            session.stage_message = "等待圆桌连接启动；当前任务已排队，尚未消耗 Agent 讨论调用"
            session.expected_turns = rounds * len(profiles)
            session.participants = agent_list
            session.progress = {
                **session.progress,
                "phase": "pending",
                "stage": "pending",
                "stage_message": session.stage_message,
                "completed_units": 0,
                "total_units": session.expected_turns,
                "expected_turns": session.expected_turns,
            }
            # create_session 已以 pending 落库；返回 session_id 前补齐预检进度，
            # 确保服务重启后的恢复逻辑只启动具有完整上下文的会话。
            if getattr(bus, "_persist", False):
                # 总线用独立事务插入；MySQL RR 的旧快照可能看不到新行。
                # 此处随即写预检进度，必须锁定当前行并刷新 ORM 缓存。
                persisted = db.query(RoundtableSession).filter_by(
                    session_id=session_id,
                    owner_user_id=int(user.id),
                ).with_for_update().populate_existing().one_or_none()
                if persisted is None:
                    raise RuntimeError("圆桌预检会话未能写入当前账号账本")
                if DiscussionBus._terminal_status_for_row(persisted):
                    # 已提交的终态优先于本进程预检；不得复活或注册迟到上下文。
                    DiscussionBus._apply_persisted_terminal(session, persisted)
                    result = _session_data(db, persisted)
                    db.commit()
                    return Resp(data=result)
                persisted.status = "pending"
                persisted.progress = dict(session.progress)
                persisted.agents = agent_list
                persisted.updated_at = datetime.now(timezone.utc)
                db.commit()

        register_pending(
            session_id=session_id,
            profiles=profiles,
            code=code_file.content or "",
            language=project.language or code_file.language or "plaintext",
            file_name=code_file.file_name,
            user_id=user.id,
            project_id=project_id,
            file_id=file_id,
            review_type=review_type,
            max_rounds=rounds,
            session_token_version=int(getattr(user, "token_version", 0) or 0),
            usage_origin=current_attribution(int(user.id)),
            # 小菱在会话内启动时记录来源会话,讨论结束后把结论回投给该会话自动汇报。
            origin_surface=str(origin_surface or "")[:24],
            origin_session_key=str(origin_session_key or "")[:128],
            continued_from_session_id=str(continued_from_session_id or "")[:64],
            continuation_context=str(continuation_context or ""),
        )
    except Exception:
        # 预检事务与上下文注册共用收口，不能遗留缺少恢复快照的 pending。
        db.rollback()
        cancel_pending(session_id, owner_user_id=int(user.id))
        _fail_pending_discussion(bus, session_id, "圆桌启动预检或上下文注册失败")
        raise

    return Resp(data={
        "session_id": session_id,
        "ws_url": f"/api/ws/discuss/{session_id}",
        "file_name": code_file.file_name,
        "language": project.language or code_file.language or "plaintext",
        "review_type": review_type,
        "continued_from_session_id": str(continued_from_session_id or "")[:64],
        "agents": agent_list,
        "participants": session.participants if session else agent_list,
        "status": "pending",
        "completed_turns": 0,
        "expected_turns": rounds * len(profiles),
        "rules_count": len(rules),
    })
