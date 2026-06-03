"""
代码文件管理API路由: 上传、新增、编辑、重命名、删除、版本管理
"""
from typing import List

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.code_file import CodeFileDetailOut, CodeFileIn, CodeFileOut, CodeFileUpdateIn, RenameIn
from app.schemas.code_version import VersionDetailOut, VersionOut
from app.schemas.common import PageOut, Resp
from app.services import code_file_service

router = APIRouter()


@router.get("", response_model=Resp[PageOut[CodeFileOut]])
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


@router.post("/upload", response_model=Resp[dict])
def upload_code(
    project_id: int = Form(...),
    file: UploadFile = File(...),
    file_path: str = Form(None),
    language: str = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传代码文件(Multipart)"""
    file_id, lang, ver = code_file_service.upload(
        db=db, user=user, project_id=project_id,
        upload_file=file, file_path=file_path, language=language,
    )
    return Resp(data={"file_id": file_id, "language": lang, "version_no": ver})


@router.post("/upload-folder", response_model=Resp[dict])
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

    for upload_file in files:
        try:
            file_id, lang, ver = code_file_service.upload(
                db=db, user=user, project_id=project_id,
                upload_file=upload_file, file_path=None, language=None,
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


@router.post("", response_model=Resp[dict])
def create_file(payload: CodeFileIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """在线新增代码文件"""
    file_id, lang, ver = code_file_service.create_file(db, user, payload)
    return Resp(data={"file_id": file_id, "language": lang, "version_no": ver})


@router.get("/{file_id}", response_model=Resp[CodeFileDetailOut])
def get_file(file_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """文件详情(含代码内容)"""
    code_file = code_file_service.get_file(db, user, file_id)
    return Resp(data=CodeFileDetailOut.model_validate(code_file))


@router.put("/{file_id}", response_model=Resp[dict])
def update_file(file_id: int, payload: CodeFileUpdateIn,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新文件内容(生成新版本)"""
    ver = code_file_service.update_content(db, user, file_id, payload.content, payload.change_desc)
    return Resp(data={"version_no": ver})


@router.post("/{file_id}/rename", response_model=Resp[None])
def rename_file(file_id: int, payload: RenameIn,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """重命名文件"""
    code_file_service.rename_file(db, user, file_id, payload.file_name, payload.file_path)
    return Resp(data=None)


@router.delete("/{file_id}", response_model=Resp[None])
def delete_file(file_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """删除文件(软删除)"""
    code_file_service.delete_file(db, user, file_id)
    return Resp(data=None)


@router.get("/{file_id}/versions", response_model=Resp[PageOut[VersionOut]])
def list_versions(file_id: int, page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """文件版本历史列表"""
    result = code_file_service.list_versions(db, user, file_id, page, page_size)
    return Resp(data=PageOut(**result))


@router.get("/{file_id}/versions/{version_no}", response_model=Resp[VersionDetailOut])
def get_version(file_id: int, version_no: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """查看指定版本内容"""
    version = code_file_service.get_version(db, user, file_id, version_no)
    return Resp(data=VersionDetailOut.model_validate(version))


@router.post("/{file_id}/versions/{version_no}/restore", response_model=Resp[dict])
def restore_version(file_id: int, version_no: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """回滚到指定历史版本"""
    ver = code_file_service.restore_version(db, user, file_id, version_no)
    return Resp(data={"version_no": ver})
