"""
项目管理API路由
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
import urllib.parse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.project import ProjectDetailOut, ProjectIn, ProjectOut, ProjectUpdateIn, RemoteProjectImportIn
from app.services import project_source_service
from app.services import project_service

router = APIRouter()


@router.get("", response_model=Resp[PageOut[ProjectOut]],
            dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))])
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


@router.post("", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.PROJECT_CREATE))])
def create_project(payload: ProjectIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """创建项目"""
    project = project_service.create_project(db, user, payload)
    return Resp(data={"id": project.id})


@router.post("/import-remote", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.PROJECT_IMPORT))])
def import_remote_project(payload: RemoteProjectImportIn, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """导入公开 HTTPS 源码归档，入库为普通项目文件。"""
    data = project_source_service.import_remote_project(
        db, user, url=payload.url, project_name=payload.project_name,
        description=payload.description or "", language=payload.language,
    )
    return Resp(data=data)


@router.get("/{project_id}", response_model=Resp[ProjectDetailOut],
            dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))])
def get_project(project_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """项目详情"""
    data = project_service.get_project(db, user, project_id)
    return Resp(data=ProjectDetailOut(**data))


@router.get("/{project_id}/source-archive",
            dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))])
def download_project_source(project_id: int, db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """下载当前用户可访问项目的完整源码归档。"""
    content, filename = project_source_service.build_source_archive(db, user, project_id)
    encoded = urllib.parse.quote(filename)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@router.put("/{project_id}", response_model=Resp[None],
            dependencies=[Depends(require_permission(PermissionCode.PROJECT_UPDATE))])
def update_project(project_id: int, payload: ProjectUpdateIn,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新项目"""
    project_service.update_project(db, user, project_id, payload)
    return Resp(data=None)


@router.delete("/{project_id}", response_model=Resp[None],
               dependencies=[Depends(require_permission(PermissionCode.PROJECT_DELETE))])
def delete_project(project_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """删除项目(软删除)"""
    project_service.delete_project(db, user, project_id)
    return Resp(data=None)
