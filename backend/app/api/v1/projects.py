"""
项目管理API路由
"""
import hashlib
import ipaddress
import json
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.observability import observe_event
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.project import (
    ProjectDetailOut,
    ProjectIn,
    ProjectOut,
    ProjectUpdateIn,
    RemoteProjectImportCancelIn,
    RemoteProjectImportIn,
    RemoteProjectImportTaskOut,
)
from app.services import (
    audit_service,
    project_import_service,
    project_service,
    project_source_revision_service,
    project_source_service,
)

router = APIRouter()


def _request_ip(request: Request) -> Optional[str]:
    """仅当直连对端为内网网关时，才信任 Nginx 覆盖写入的 X-Real-IP。"""

    def parse(candidate: str):
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            return None

    peer = parse(request.client.host if request.client else "")
    if peer is None:
        return None
    if peer.is_private or peer.is_loopback or peer.is_link_local:
        forwarded = parse(request.headers.get("x-real-ip", "").strip())
        if forwarded is not None:
            return str(forwarded)
    return str(peer)


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
def import_remote_project(
    payload: RemoteProjectImportIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """兼容旧同步导入调用；第一方客户端已经迁移到异步任务接口。"""
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Wed, 30 Sep 2026 00:00:00 GMT"
    response.headers["Link"] = '</api/projects/remote-imports>; rel="successor-version"'
    observe_event(
        "project_remote_import_legacy_sync_called",
        labels={"role": str(user.role or "unknown"), "surface": "api"},
    )
    data = project_source_service.import_remote_project(
        db, user, url=payload.url, project_name=payload.project_name,
        description=payload.description or "", language=payload.language,
        audit_mode=payload.audit_mode,
    )
    return Resp(data=data)


@router.post(
    "/remote-imports",
    response_model=Resp[RemoteProjectImportTaskOut],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission(PermissionCode.PROJECT_IMPORT))],
)
def queue_remote_project_import(
    payload: RemoteProjectImportIn,
    response: Response,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建可恢复的远程导入任务；显式幂等键可安全重放。"""

    data = project_import_service.create_import_task(
        db,
        user,
        url=payload.url,
        project_name=payload.project_name,
        description=payload.description or "",
        language=payload.language,
        audit_mode=payload.audit_mode,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = f"/api/projects/remote-imports/{data['task_id']}"
    response.headers["Retry-After"] = str(settings.project_import_dispatch_interval_seconds)
    return Resp(data=RemoteProjectImportTaskOut(**data))


@router.get(
    "/remote-imports/{task_id}",
    response_model=Resp[RemoteProjectImportTaskOut],
    dependencies=[Depends(require_permission(PermissionCode.PROJECT_IMPORT))],
)
def get_remote_project_import(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询当前用户创建的远程导入任务和可操作失败原因。"""

    data = project_import_service.get_import_task(db, user, task_id)
    return Resp(data=RemoteProjectImportTaskOut(**data))


@router.post(
    "/remote-imports/{task_id}/cancel",
    response_model=Resp[RemoteProjectImportTaskOut],
    dependencies=[Depends(require_permission(PermissionCode.PROJECT_IMPORT))],
)
def cancel_remote_project_import(
    task_id: str,
    payload: RemoteProjectImportCancelIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """由任务所有者取消排队、下载、扫描或入库中的远程导入。"""

    data = project_import_service.cancel_import_task(
        db,
        user,
        task_id,
        reason=payload.reason,
    )
    return Resp(data=RemoteProjectImportTaskOut(**data))


@router.get("/{project_id}", response_model=Resp[ProjectDetailOut],
            dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))])
def get_project(project_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """项目详情"""
    data = project_service.get_project(db, user, project_id)
    return Resp(data=ProjectDetailOut(**data))


@router.delete("/{project_id}/source-revisions/{revision_id}",
                dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))],
                status_code=200)
def delete_source_revision(project_id: int, revision_id: int,
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """删除项目的一个源码修复副本(原始归档不受影响)。"""
    project_source_revision_service.delete_revision(db, user, project_id, revision_id)
    audit_service.log(
        db, user, "source_revision_delete",
        target_type="project_source_revision",
        target_id=str(revision_id),
        detail=f"project={project_id}",
        commit=True,
    )
    return Resp(data={"deleted": revision_id})


@router.get("/{project_id}/source-archive",
            dependencies=[
                Depends(require_permission(PermissionCode.PROJECT_VIEW)),
                Depends(require_permission(PermissionCode.FILE_DOWNLOAD)),
            ])
def download_project_source(project_id: int, request: Request, db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """下载当前用户可访问项目的完整源码归档。"""
    content, filename = project_source_service.build_source_archive(db, user, project_id)
    metadata = project_source_service.get_source_archive_metadata(db, user, project_id)
    encoded = urllib.parse.quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    media_type = "application/zip"
    if metadata:
        media_type = "application/octet-stream"
        headers.update({
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Prism-Source-SHA256": metadata["archive_sha256"],
            "X-Prism-Malware-Status": metadata["malware_status"],
        })
    archive_sha256 = (
        metadata["archive_sha256"] if metadata else hashlib.sha256(content).hexdigest()
    )
    audit_service.log(
        db,
        user,
        "project_source_download",
        target_type="project",
        target_id=str(project_id),
        detail=json.dumps(
            {
                "archive_sha256": archive_sha256,
                "byte_size": len(content),
                "filename": filename,
                "malware_status": metadata["malware_status"] if metadata else "not_scanned",
                "project_id": project_id,
                "result": "prepared",
                "threat_count": int(metadata.get("threat_count", 0)) if metadata else 0,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        ip=_request_ip(request),
    )
    return Response(
        content=content,
        media_type=media_type,
        headers=headers,
    )


@router.post(
    "/{project_id}/audit-source-archive",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.FILE_UPLOAD))],
)
def upload_audit_source_archive(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传待审计源码归档；命中恶意规则时保留隔离证据。"""
    data = project_source_service.ingest_source_archive_bytes(
        db,
        user,
        project_id,
        raw=file.file.read(),
        filename=file.filename or "",
    )
    return Resp(data=data)


@router.get(
    "/{project_id}/audit-source-archive",
    response_model=Resp[Optional[dict]],
    dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))],
)
def get_audit_source_archive(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询隔离归档的存储、恶意代码和审计状态。"""
    return Resp(data=project_source_service.get_source_archive_metadata(db, user, project_id))


@router.get(
    "/{project_id}/audit-source-archive/result",
    response_model=Resp[Optional[dict]],
    dependencies=[
        Depends(require_permission(PermissionCode.PROJECT_VIEW)),
        Depends(require_permission(PermissionCode.SECURITY_VIEW)),
    ],
)
def get_audit_source_archive_result(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """读取与当前隔离原包 SHA-256 绑定的持久化白盒审计结果。"""
    return Resp(data=project_source_service.get_source_archive_audit_result(db, user, project_id))


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
