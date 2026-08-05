"""
代码文件管理API路由: 上传、新增、编辑、重命名、删除、版本管理、二进制下载
"""
from typing import List

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ValidationError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.code_file import (
    CodeFileDetailOut,
    CodeFileIn,
    CodeFileMetaOut,
    CodeFileOut,
    CodeFileUpdateIn,
    RenameIn,
)
from app.schemas.code_version import VersionDetailOut, VersionOut
from app.schemas.common import PageOut, Resp
from app.services import code_file_service

router = APIRouter()


@router.get("", response_model=Resp[PageOut[CodeFileOut]],
            dependencies=[Depends(require_permission(PermissionCode.FILE_VIEW))])
def list_files(
    project_id: int = Query(...),
    language: str = Query(""),
    keyword: str = Query(""),
    exclude_binary: bool = Query(False, description="排除图片/二进制文件,仅返回可审查的代码文件"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """文件列表"""
    result = code_file_service.list_files(
        db, user, project_id, language, keyword, exclude_binary, page, page_size,
    )
    return Resp(data=PageOut(**result))


@router.post("/upload", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.FILE_UPLOAD))])
def upload_code(
    project_id: int = Form(...),
    file: UploadFile = File(...),
    file_path: str = Form(None),
    language: str = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传代码文件(Multipart)"""
    try:
        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project_id,
            upload_file=file, file_path=file_path, language=language,
        )
    except ValueError as exc:
        # 恶意软件/归档安全扫描拒绝属于可预期的用户输入结果,不能升级为 500。
        raise ValidationError(str(exc), code=40001) from exc
    quarantined = lang == "quarantined"
    return Resp(data={
        "file_id": file_id,
        "language": lang,
        "version_no": ver,
        "quarantined": quarantined,
    })


@router.post("/upload-folder", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.FILE_UPLOAD))])
def upload_folder(
    project_id: int = Form(...),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量上传代码文件(支持文件夹上传)

    接收多个文件,逐个保存到指定项目中。每个文件自动检测语言。

    Args:
        project_id: 目标项目ID
        files: 上传的文件列表
        db: 数据库会话
        user: 当前登录用户

    Returns:
        Resp[dict]: 包含成功/失败计数和文件列表
    """
    results = []
    success_count = 0
    fail_count = 0
    errors: List[dict] = []
    logical_paths = code_file_service.normalize_folder_upload_paths([
        upload_file.filename or "" for upload_file in files
    ])

    for upload_file, logical_path in zip(files, logical_paths):
        try:
            file_id, lang, ver = code_file_service.upload(
                db=db, user=user, project_id=project_id,
                upload_file=upload_file, file_path=logical_path, language=None,
            )
            results.append({
                "file_name": upload_file.filename,
                "file_id": file_id,
                "language": lang,
                "version_no": ver,
            })
            success_count += 1
        except Exception as e:
            fail_count += 1
            errors.append({
                "file_name": upload_file.filename,
                "error": str(e),
            })

    return Resp(data={
        "success_count": success_count,
        "fail_count": fail_count,
        "files": results,
        "errors": errors,
    })


@router.post("", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.FILE_UPLOAD))])
def create_file(payload: CodeFileIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """在线新增代码文件"""
    file_id, lang, ver = code_file_service.create_file(db, user, payload)
    return Resp(data={"file_id": file_id, "language": lang, "version_no": ver})


@router.get("/{file_id}", response_model=Resp[CodeFileDetailOut],
            dependencies=[Depends(require_permission(PermissionCode.FILE_VIEW))])
def get_file(file_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """文件详情(含代码内容)"""
    code_file = code_file_service.get_file(db, user, file_id)
    return Resp(data=CodeFileDetailOut.model_validate(code_file))


@router.get("/{file_id}/meta", response_model=Resp[CodeFileMetaOut],
            dependencies=[Depends(require_permission(PermissionCode.FILE_VIEW))])
def get_file_meta(file_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """获取文件元信息(不含内容,含 MIME/MD5/SHA-256)

    v3 新增:二进制文件展示提示卡片时,通过此接口获取文件元数据,
    避免下载完整文件内容。MD5/SHA-256 后端实时计算,不入库存储。

    Args:
        file_id: 文件ID
        db: 数据库会话
        user: 当前用户

    Returns:
        Resp[CodeFileMetaOut]: 文件元信息(不含 content 字段)
    """
    meta = code_file_service.get_file_meta(db, user, file_id)
    return Resp(data=CodeFileMetaOut.model_validate(meta))


@router.put("/{file_id}", response_model=Resp[dict],
            dependencies=[Depends(require_permission(PermissionCode.FILE_EDIT))])
def update_file(file_id: int, payload: CodeFileUpdateIn,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新文件内容(生成新版本)"""
    ver = code_file_service.update_content(db, user, file_id, payload.content, payload.change_desc)
    return Resp(data={"version_no": ver})


@router.post("/{file_id}/rename", response_model=Resp[None],
             dependencies=[Depends(require_permission(PermissionCode.FILE_EDIT))])
def rename_file(file_id: int, payload: RenameIn,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """重命名文件"""
    code_file_service.rename_file(db, user, file_id, payload.file_name, payload.file_path)
    return Resp(data=None)


@router.delete("/{file_id}", response_model=Resp[None],
               dependencies=[Depends(require_permission(PermissionCode.FILE_DELETE))])
def delete_file(file_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """删除文件(软删除)"""
    code_file_service.delete_file(db, user, file_id)
    return Resp(data=None)


@router.get("/{file_id}/versions", response_model=Resp[PageOut[VersionOut]],
            dependencies=[Depends(require_permission(PermissionCode.FILE_VIEW))])
def list_versions(file_id: int, page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """文件版本历史列表"""
    result = code_file_service.list_versions(db, user, file_id, page, page_size)
    return Resp(data=PageOut(**result))


@router.get("/{file_id}/versions/{version_no}", response_model=Resp[VersionDetailOut],
            dependencies=[Depends(require_permission(PermissionCode.FILE_VIEW))])
def get_version(file_id: int, version_no: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """查看指定版本内容"""
    version = code_file_service.get_version(db, user, file_id, version_no)
    return Resp(data=VersionDetailOut.model_validate(version))


@router.post("/{file_id}/versions/{version_no}/restore", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.FILE_EDIT))])
def restore_version(file_id: int, version_no: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """回滚到指定历史版本"""
    ver = code_file_service.restore_version(db, user, file_id, version_no)
    return Resp(data={"version_no": ver})


@router.get("/{file_id}/download",
            dependencies=[Depends(require_permission(PermissionCode.FILE_DOWNLOAD))])
def download_binary_file(file_id: int, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """下载二进制文件原始字节

    v2 新增:二进制文件(图片/可执行文件等)不通过编辑器展示 base64,
    前端通过此接口下载原文件。

    Args:
        file_id: 文件ID
        db: 数据库会话
        user: 当前用户

    Returns:
        Response: 文件原始字节流(含 Content-Disposition 头)
    """
    import urllib.parse
    raw_bytes, file_name = code_file_service.get_binary_content(db, user, file_id)
    # RFC 5987 编码中文文件名,避免 Content-Disposition 乱码
    encoded_name = urllib.parse.quote(file_name)
    return Response(
        content=raw_bytes,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
        },
    )
