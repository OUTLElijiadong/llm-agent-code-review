"""小菱 Agent Mesh：会话发现、标准消息投递和链路追踪 API。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.agent_mesh import AgentMeshAckIn, AgentMeshHeartbeatIn, AgentMeshMessageIn
from app.schemas.common import Resp
from app.services import agent_mesh_service

router = APIRouter()


class AgentMeshSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    surface: Literal["user", "admin"]
    session_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    message: AgentMeshMessageIn


class AgentMeshArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    surface: Literal["user", "admin"]
    session_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def _raise_mesh_error(exc: agent_mesh_service.AgentMeshError) -> None:
    if isinstance(exc, agent_mesh_service.AgentMeshAccessError):
        raise ForbiddenError(str(exc), code=40321)
    if isinstance(exc, agent_mesh_service.AgentMeshTargetError):
        raise NotFoundError(str(exc), code=40421)
    raise ConflictError(str(exc), code=40921)


@router.post(
    "/conversations/heartbeat",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def heartbeat(
    payload: AgentMeshHeartbeatIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(
            data=agent_mesh_service.heartbeat(
                db,
                user,
                surface=payload.surface,
                session_key=payload.session_id,
                title=payload.title,
                active_run_id=payload.active_run_id,
                active_run_status=payload.active_run_status,
            )
        )
    except agent_mesh_service.AgentMeshError as exc:
        _raise_mesh_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/conversations/archive",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def archive_session(
    payload: AgentMeshArchiveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(
            data=agent_mesh_service.archive_conversation(
                db,
                user,
                surface=payload.surface,
                session_key=payload.session_id,
            )
        )
    except agent_mesh_service.AgentMeshError as exc:
        _raise_mesh_error(exc)
        raise AssertionError("unreachable")


@router.get(
    "/agents",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def list_agents(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    return Resp(data=agent_mesh_service.list_agents(db, user))


@router.post(
    "/messages",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def send_message(
    request: AgentMeshSendRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(
            data=agent_mesh_service.send_message(
                db,
                user,
                surface=request.surface,
                session_key=request.session_id,
                message=request.message,
            )
        )
    except agent_mesh_service.AgentMeshError as exc:
        _raise_mesh_error(exc)
        raise AssertionError("unreachable")


@router.get(
    "/inbox",
    response_model=Resp[list],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def pull_inbox(
    surface: Literal["user", "admin"] = Query(...),
    session_id: str = Query(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[list]:
    try:
        return Resp(
            data=agent_mesh_service.pull_inbox(
                db,
                user,
                surface=surface,
                session_key=session_id,
                limit=limit,
            )
        )
    except agent_mesh_service.AgentMeshError as exc:
        _raise_mesh_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/messages/{message_id}/ack",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def acknowledge_message(
    message_id: str,
    payload: AgentMeshAckIn,
    surface: Literal["user", "admin"] = Query(...),
    session_id: str = Query(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(
            data=agent_mesh_service.ack_message(
                db,
                user,
                message_id,
                surface=surface,
                session_key=session_id,
                acknowledgement=payload,
            )
        )
    except agent_mesh_service.AgentMeshError as exc:
        _raise_mesh_error(exc)
        raise AssertionError("unreachable")


@router.get(
    "/traces/{trace_id}",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def get_trace(
    trace_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(data=agent_mesh_service.get_trace(db, user, trace_id))
    except agent_mesh_service.AgentMeshError as exc:
        _raise_mesh_error(exc)
        raise AssertionError("unreachable")
