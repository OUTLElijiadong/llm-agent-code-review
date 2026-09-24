"""讨论模式审查 REST API (v2.3 M7)

POST /discuss/start  — 启动讨论模式审查(preflight)
     → 返回 session_id → 前端连接 WebSocket → 讨论开始
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case
from sqlalchemy.orm import Session, sessionmaker

from app.agents.discussion_bus import DiscussionBus, utc_timestamp, visible_roundtable_progress
from app.api.v1.ws_discussion import purge_stale_pending, register_pending
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.roundtable import RoundtableSession, RoundtableTurn
from app.models.user import User
from app.schemas.common import Resp
from app.services import rule_service
from app.services.ai_usage_context import current_attribution
from app.services.project_member_service import require_project_access
from app.services.review_input_service import validate_review_input

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


def _gen_session_id() -> str:
    return f"disc_{uuid.uuid4().hex[:10]}"


@router.post("/discuss/start", response_model=Resp[dict])
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
):
    """预检并注册讨论,返回 session_id 和基本信息。

    前端调用此接口后:
    1. 连接到 ws://host:8000/api/ws/discuss/{session_id}?token=xxx
    2. 讨论自动开始,所有 Agent 发言实时推送到前端
    3. 用户可通过 WebSocket 发送 {action:"user_input",content:"..."} 参与讨论
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    require_project_access(db, project_id, user, need_write=False)

    code_file = db.query(CodeFile).filter(
        CodeFile.id == file_id,
        CodeFile.project_id == project_id,
        CodeFile.status == "active",
    ).first()
    if not code_file:
        raise NotFoundError("文件不存在", code=40400)
    validate_review_input(code_file)

    from app.ai.multi_agent import get_discussion_agent_profiles
    profiles = get_discussion_agent_profiles()
    agent_list = [
        {"code": p.code, "name": p.name, "focus": p.focus}
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
    )

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

    return Resp(data={
        "session_id": session_id,
        "ws_url": f"/api/ws/discuss/{session_id}",
        "file_name": code_file.file_name,
        "language": project.language or code_file.language or "plaintext",
        "review_type": review_type,
        "continued_from_session_id": str(continued_from_session_id or "")[:64],
        "agents": agent_list,
        "rules_count": len(rules),
    })
