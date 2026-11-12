"""企业项目目录和项目级成员授权 API。"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import enterprise_project_service as service

router = APIRouter(prefix="/enterprises/{enterprise_id}/projects", tags=["企业项目"])


class BindProjectIn(StrictInputModel):
    project_id: int = Field(gt=0, strict=True)
    group_id: int = Field(gt=0, strict=True)


class ProjectGrantIn(StrictInputModel):
    role: Literal["viewer", "reviewer", "editor"]


@router.get("", response_model=Resp[list[dict]])
def list_projects(
    enterprise_id: int,
    group_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=service.list_projects(db, enterprise_id, int(user.id), group_id=group_id))


@router.post("", response_model=Resp[dict])
def bind_owned_project(
    enterprise_id: int,
    payload: BindProjectIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=service.bind_owned_project(
        db,
        enterprise_id,
        payload.project_id,
        group_id=payload.group_id,
        actor_id=int(user.id),
    ))


@router.put("/{project_id}/grants/{user_id}", response_model=Resp[dict])
def set_project_grant(
    enterprise_id: int,
    project_id: int,
    user_id: int,
    payload: ProjectGrantIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = service.set_project_grant(
        db,
        enterprise_id,
        project_id,
        user_id,
        actor_id=int(user.id),
        role=payload.role,
    )
    return Resp(data={"project_id": project_id, "user_id": user_id, "role": row.role, "status": row.status})


@router.delete("/{project_id}/grants/{user_id}", response_model=Resp[None])
def revoke_project_grant(
    enterprise_id: int,
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service.revoke_project_grant(db, enterprise_id, project_id, user_id, actor_id=int(user.id))
    return Resp(data=None)
