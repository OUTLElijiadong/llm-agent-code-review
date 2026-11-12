"""Tenant-scoped progress and controls for enterprise Agent teams."""

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_agent_team_service as service
from app.services import enterprise_security_artifact_service as security_artifacts

router = APIRouter(prefix="/enterprises/{enterprise_id}/agent-teams", tags=["enterprise-agent-teams"])


class TeamCancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(default="用户停止", max_length=500)


class TeamRetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: int = Field(gt=0)


def _session_factory(db: Session):
    bind = db.get_bind()
    return sessionmaker(bind=getattr(bind, "engine", bind), autocommit=False, autoflush=False, expire_on_commit=False)


@router.get("", response_model=Resp[dict])
def list_enterprise_agent_teams(
    enterprise_id: int,
    group_id: int | None = Query(None, gt=0),
    conversation_id: int | None = Query(None, gt=0),
    before_id: int | None = Query(None, gt=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List minimum operational metadata; team results require the source conversation API."""

    return Resp(
        data=service.list_team_summaries(
            db,
            enterprise_id=int(enterprise_id),
            user_id=int(user.id),
            group_id=group_id,
            conversation_id=conversation_id,
            before_id=before_id,
            limit=limit,
        )
    )


@router.get("/{team_id}", response_model=Resp[dict])
def get_enterprise_agent_team(
    enterprise_id: int,
    team_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return task results only while the caller can read the originating conversation."""
    return Resp(
        data=service.get_team_detail(
            db,
            enterprise_id=int(enterprise_id),
            user_id=int(user.id),
            team_id=int(team_id),
        )
    )


@router.get("/{team_id}/security-artifacts/{artifact_id}", response_model=Resp[dict])
def get_enterprise_security_artifact(
    enterprise_id: int,
    team_id: int,
    artifact_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a report only when current team-result and project-view scopes intersect."""
    return Resp(
        data=security_artifacts.get_team_security_artifact(
            db,
            enterprise_id=int(enterprise_id),
            user_id=int(user.id),
            team_id=int(team_id),
            artifact_id=int(artifact_id),
        )
    )


@router.head("/{team_id}/security-artifacts/{artifact_id}", status_code=204)
def check_enterprise_security_artifact_access(
    enterprise_id: int,
    team_id: int,
    artifact_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revalidate report access without loading or returning the report body."""
    security_artifacts.check_team_security_artifact_access(
        db,
        enterprise_id=int(enterprise_id),
        user_id=int(user.id),
        team_id=int(team_id),
        artifact_id=int(artifact_id),
    )
    return Response(status_code=204)


@router.post("/{team_id}/cancel", response_model=Resp[dict])
def cancel_enterprise_agent_team(
    enterprise_id: int,
    team_id: int,
    payload: TeamCancelBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = runtime.cancel_team(
        _session_factory(db),
        enterprise_id=int(enterprise_id),
        team_id=int(team_id),
        actor_user_id=int(user.id),
        reason=payload.reason,
    )
    from app.services.enterprise_agent_team_dispatcher import _publish_terminal_receipt

    _publish_terminal_receipt(
        _session_factory(db),
        enterprise_id=int(enterprise_id),
        team_id=int(team_id),
    )
    return Resp(data=result)


@router.post("/{team_id}/retry", response_model=Resp[dict])
def retry_enterprise_agent_team_task(
    enterprise_id: int,
    team_id: int,
    payload: TeamRetryBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(
        data=runtime.retry_task(
            _session_factory(db),
            enterprise_id=int(enterprise_id),
            team_id=int(team_id),
            task_id=int(payload.task_id),
            actor_user_id=int(user.id),
        )
    )
