"""
项目管理API路由
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.project import ProjectDetailOut, ProjectIn, ProjectOut, ProjectUpdateIn
from app.services import project_service

router = APIRouter()


@router.get("", response_model=Resp[PageOut[ProjectOut]])
def list_projects(
    keyword: str = Query(""),
    language: str = Query(""),
    status: str = Query("active"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目列表"""
    result = project_service.list_projects(db, user, keyword, language, status, page, page_size)
    return Resp(data=PageOut(**result))


@router.post("", response_model=Resp[dict])
def create_project(payload: ProjectIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """创建项目"""
    project = project_service.create_project(db, user, payload)
    return Resp(data={"id": project.id})


@router.get("/{project_id}", response_model=Resp[ProjectDetailOut])
def get_project(project_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """项目详情"""
    data = project_service.get_project(db, user, project_id)
    return Resp(data=ProjectDetailOut(**data))


@router.put("/{project_id}", response_model=Resp[None])
def update_project(project_id: int, payload: ProjectUpdateIn,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新项目"""
    project_service.update_project(db, user, project_id, payload)
    return Resp(data=None)


@router.delete("/{project_id}", response_model=Resp[None])
def delete_project(project_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """删除项目(软删除)"""
    project_service.delete_project(db, user, project_id)
    return Resp(data=None)
