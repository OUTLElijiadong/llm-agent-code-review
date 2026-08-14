"""讨论模式审查 REST API (v2.3 M7)

GET  /discuss/start  — 启动讨论模式审查(preflight)
     → 返回 session_id → 前端连接 WebSocket → 讨论开始
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agents.discussion_bus import DiscussionBus
from app.api.v1.ws_discussion import register_pending
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.schemas.common import Resp
from app.services import rule_service

router = APIRouter()


def _gen_session_id() -> str:
    return f"disc_{uuid.uuid4().hex[:10]}"


@router.get("/discuss/start", response_model=Resp[dict])
def start_discussion(
    project_id: int = Query(...),
    file_id: int = Query(...),
    review_type: str = Query("full"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    origin_surface: str = "",
    origin_session_key: str = "",
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
    if project.user_id != user.id and user.role not in {"admin", "super_admin"}:
        raise ForbiddenError("无访问权限", code=40300)

    code_file = db.query(CodeFile).filter(
        CodeFile.id == file_id,
        CodeFile.project_id == project_id,
        CodeFile.status == "active",
    ).first()
    if not code_file:
        raise NotFoundError("文件不存在", code=40400)

    from app.ai.multi_agent import get_discussion_agent_profiles
    profiles = get_discussion_agent_profiles()
    rules = rule_service.get_enabled_rules(
        db, user.id, language=(project.language or "").strip().lower(),
    )

    session_id = _gen_session_id()
    rounds = 2
    bus = DiscussionBus.instance()
    bus.create_session(
        session_id=session_id,
        task_id=0,
        file_name=code_file.file_name,
        owner_user_id=user.id,
        max_rounds=rounds,
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
        # 小菱在会话内启动时记录来源会话,讨论结束后把结论回投给该会话自动汇报。
        origin_surface=str(origin_surface or "")[:24],
        origin_session_key=str(origin_session_key or "")[:128],
    )

    agent_list = [
        {"code": p.code, "name": p.name, "focus": p.focus}
        for p in profiles
    ]

    return Resp(data={
        "session_id": session_id,
        "ws_url": f"/api/ws/discuss/{session_id}",
        "file_name": code_file.file_name,
        "language": project.language or code_file.language or "plaintext",
        "review_type": review_type,
        "agents": agent_list,
        "rules_count": len(rules),
    })
