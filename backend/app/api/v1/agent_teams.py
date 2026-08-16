"""小菱动态子 Agent 团队 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.agent_team import AgentTeamArchiveIn, AgentTeamCancelIn, AgentTeamCreateIn, AgentTeamRetryIn
from app.schemas.common import Resp
from app.services import agent_team_service

router = APIRouter()


def _raise_team_error(exc: agent_team_service.AgentTeamError) -> None:
    if isinstance(exc, agent_team_service.AgentTeamNotFoundError):
        raise NotFoundError(str(exc), code=40431)
    if isinstance(exc, agent_team_service.AgentTeamAccessError):
        raise ForbiddenError(str(exc), code=40331)
    if isinstance(exc, agent_team_service.AgentTeamValidationError):
        raise ValidationError(str(exc), code=40031)
    raise ConflictError(str(exc), code=40931)


@router.post("", response_model=Resp[dict], dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))])
def create_team(
    payload: AgentTeamCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(data=agent_team_service.create_team(db, user, payload))
    except agent_team_service.AgentTeamError as exc:
        _raise_team_error(exc)
        raise AssertionError("unreachable")


@router.get("", response_model=Resp[dict], dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))])
def list_teams(
    surface: str = Query(default="", max_length=16),
    session_id: str = Query(default="", max_length=128),
    status: str = Query(default="", max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(
            data=agent_team_service.list_teams(
                db,
                user,
                surface=surface,
                session_id=session_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        )
    except agent_team_service.AgentTeamError as exc:
        _raise_team_error(exc)
        raise AssertionError("unreachable")


@router.get(
    "/{team_id}", response_model=Resp[dict], dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))]
)
def get_team(
    team_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(data=agent_team_service.get_team(db, user, team_id))
    except agent_team_service.AgentTeamError as exc:
        _raise_team_error(exc)
        raise AssertionError("unreachable")


@router.get(
    "/{team_id}/messages",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def list_team_messages(
    team_id: int,
    before_id: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(
            data=agent_team_service.list_team_messages(
                db,
                user,
                team_id,
                before_id=before_id,
                limit=limit,
            )
        )
    except agent_team_service.AgentTeamError as exc:
        _raise_team_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/{team_id}/cancel",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def cancel_team(
    team_id: int,
    payload: AgentTeamCancelIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(data=agent_team_service.cancel_team(db, user, team_id, reason=payload.reason))
    except agent_team_service.AgentTeamError as exc:
        _raise_team_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/{team_id}/retry", response_model=Resp[dict], dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))]
)
def retry_team(
    team_id: int,
    payload: AgentTeamRetryIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(
            data=agent_team_service.retry_team(
                db,
                user,
                team_id,
                task_keys=payload.task_keys,
                strategy_changes=payload.strategy_changes,
            )
        )
    except agent_team_service.AgentTeamError as exc:
        _raise_team_error(exc)
        raise AssertionError("unreachable")


@router.post(
    "/{team_id}/archive",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def archive_team(
    team_id: int,
    payload: AgentTeamArchiveIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    try:
        return Resp(data=agent_team_service.archive_team(db, user, team_id, reason=payload.reason))
    except agent_team_service.AgentTeamError as exc:
        _raise_team_error(exc)
        raise AssertionError("unreachable")
