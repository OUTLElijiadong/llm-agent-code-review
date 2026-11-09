"""
代码文件管理API路由: 上传、新增、编辑、重命名、删除、版本管理、二进制下载
"""
import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AuthError, ConflictError, PermissionError, ServiceUnavailableError, ValidationError
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
from app.services.rbac_service import check_paid_permission_fenced
from app.utils.multipart_limits import parse_limited_multipart

router = APIRouter()
_folder_upload_capacity = threading.BoundedSemaphore(4)
_folder_upload_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="folder-upload")


@dataclass(frozen=True)
class _FolderActor:
    user_id: int
    token_version: int
    token_expires_at: int


@dataclass(frozen=True)
class _FolderAuthorization:
    user: User
    reviewer_evidence: object | None
    enforcement_ready: bool
    transaction: object


def _snapshot_folder_actor(user: User) -> _FolderActor:
    user_id = getattr(user, "id", None)
    version = getattr(user, "_authenticated_token_version", None)
    expires_at = getattr(user, "_authenticated_token_expires_at", None)
    if type(user_id) is not int or type(version) is not int or type(expires_at) is not int:
        raise AuthError("登录状态已失效，请重新登录")
    return _FolderActor(user_id, version, expires_at)


def _authorize_folder_actor(
    db: Session, actor: _FolderActor, *, permission_code: str = PermissionCode.FILE_UPLOAD, nowait: bool = False,
) -> _FolderAuthorization:
    if actor.token_expires_at <= int(datetime.now(timezone.utc).timestamp()):
        raise AuthError("登录状态已过期，请重新登录")
    evidence: list = []
    enforcement_ready = bool(settings.billing_entitlement_enforcement_ready)
    if not check_paid_permission_fenced(
        db, actor.user_id, permission_code, expected_token_version=actor.token_version,
        locked_reviewer_evidence=evidence,
        **({"nowait": True} if nowait else {}),
    ):
        raise PermissionError("登录状态或操作权限已失效")
    user = db.get(User, actor.user_id)
    if user is None:
        raise AuthError("登录状态已失效，请重新登录")
    if len(evidence) != 1:
        raise PermissionError("操作授权证据不可用")
    transaction = db.get_transaction()
    if transaction is None:
        raise PermissionError("操作授权事务已失效")
    return _FolderAuthorization(user, evidence[0], enforcement_ready, transaction)


class _FolderCommitGuard:
    """Recheck time-dependent evidence already locked by the current transaction."""

    def __init__(self, db: Session, actor: _FolderActor, authorization: _FolderAuthorization):
        self.db = db
        self.actor = actor
        self.authorization = authorization

    def after_rollback(self) -> None:
        # Quarantine starts a new transaction after the failed scan rolled back.
        self.authorization = _authorize_folder_actor(self.db, self.actor)

    def before_commit(self) -> None:
        if self.db.get_transaction() is not self.authorization.transaction:
            raise PermissionError("操作授权事务已失效")
        if self.actor.token_expires_at <= int(datetime.now(timezone.utc).timestamp()):
            raise AuthError("登录状态已过期，请重新登录")
        if bool(settings.billing_entitlement_enforcement_ready) != self.authorization.enforcement_ready:
            raise PermissionError("操作授权状态已变化，请重试")
        if self.authorization.reviewer_evidence is not None:
            from app.services.billing_entitlement_service import reviewer_entitlement_evidence_is_valid

            if not reviewer_entitlement_evidence_is_valid(self.authorization.reviewer_evidence):
                raise PermissionError("审查员权益已过期")


def _close_folder_form(form) -> None:
    """The submitted future owns every parsed file, even if it never starts."""
    for _, item in form.multi_items():
        if isinstance(item, StarletteUploadFile):
            try:
                item.file.close()
            except Exception as exc:
                # Keep closing remaining files even if one temporary file fails.
                logger.warning("目录上传临时文件关闭失败: {}", type(exc).__name__)


def _run_owned_folder_upload(engine: Engine, actor: _FolderActor, project_id: int,
                             files: list[StarletteUploadFile], cancelled: threading.Event) -> dict:
    # No request-scoped Session or ORM object is shared with this thread.
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        return _upload_folder_items(db, None, project_id, files, actor=actor, cancelled=cancelled)


async def _close_unsubmitted_folder_form(form) -> None:
    """Release a parsed form even if cancellation interrupts its caller."""
    try:
        future = _folder_upload_executor.submit(_close_folder_form, form)
    except BaseException:
        try:
            _close_folder_form(form)
        finally:
            _folder_upload_capacity.release()
        raise
    future.add_done_callback(lambda _future: _folder_upload_capacity.release())
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    await asyncio.shield(wrapped)


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
    actor = _snapshot_folder_actor(user)
    authorization = _authorize_folder_actor(db, actor)
    guard = _FolderCommitGuard(db, actor, authorization)
    try:
        file_id, lang, ver = code_file_service.upload(
            db=db, user=authorization.user, project_id=project_id,
            upload_file=file, file_path=file_path, language=language,
            before_commit=guard.before_commit, after_rollback=guard.after_rollback,
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


def _upload_folder_items(
    db: Session,
    user: User | None,
    project_id: int,
    files: list[StarletteUploadFile],
    *,
    actor: _FolderActor | None = None,
    cancelled: threading.Event | None = None,
) -> dict:
    """在线程池内沿用既有逐文件上传事务与结果合同。"""
    results = []
    success_count = 0
    fail_count = 0
    errors: List[dict] = []
    logical_paths = code_file_service.normalize_folder_upload_paths([
        upload_file.filename or "" for upload_file in files
    ])

    for upload_file, logical_path in zip(files, logical_paths):
        if cancelled is not None and cancelled.is_set():
            break
        guard = None
        if actor is not None:
            authorization = _authorize_folder_actor(db, actor)
            user = authorization.user
            guard = _FolderCommitGuard(db, actor, authorization)
        if cancelled is not None and cancelled.is_set():
            db.rollback()
            break
        if user is None:
            raise AuthError("登录状态已失效，请重新登录")
        try:
            file_id, lang, ver = code_file_service.upload(
                db=db, user=user, project_id=project_id,
                upload_file=upload_file, file_path=logical_path, language=None,
                before_commit=guard.before_commit if guard else None,
                after_rollback=guard.after_rollback if guard else None,
            )
            results.append({
                "file_name": upload_file.filename,
                "file_id": file_id,
                "language": lang,
                "version_no": ver,
            })
            success_count += 1
        except Exception as exc:
            fail_count += 1
            errors.append({
                "file_name": upload_file.filename,
                "error": str(exc),
            })

    return {
        "success_count": success_count,
        "fail_count": fail_count,
        "files": results,
        "errors": errors,
    }


@router.post("/upload-folder", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.FILE_UPLOAD))])
async def upload_folder(
    request: Request,
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
    if not _folder_upload_capacity.acquire(blocking=False):
        raise ServiceUnavailableError("当前目录上传较多，请稍后重试")
    form = None
    transferred = False
    cancelled = threading.Event()
    try:
        actor = _snapshot_folder_actor(user)
        engine = db.get_bind()
        if not isinstance(engine, Engine):
            raise ServiceUnavailableError("目录上传需要独立数据库连接")
        form = await parse_limited_multipart(
            request.headers,
            request.stream(),
            max_file_bytes=settings.max_upload_size,
            max_files=1000,
        )
        raw_project_id = form.get("project_id")
        try:
            project_id = int(str(raw_project_id))
        except (TypeError, ValueError) as exc:
            raise ValidationError("project_id 必须是整数") from exc
        if project_id <= 0:
            raise ValidationError("project_id 必须大于 0")
        files = [item for item in form.getlist("files") if isinstance(item, StarletteUploadFile)]
        if not files:
            raise ValidationError("至少上传一个文件")
        future = _folder_upload_executor.submit(_run_owned_folder_upload, engine, actor, project_id, files, cancelled)
        transferred = True

        def cleanup(_future: Future) -> None:
            try:
                _close_folder_form(form)
            finally:
                _folder_upload_capacity.release()

        future.add_done_callback(cleanup)
        wrapped = asyncio.wrap_future(future)
        # A cancelled request may no longer await the worker's result.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            result = await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            cancelled.set()
            future.cancel()
            raise
        return Resp(data=result)
    finally:
        if not transferred:
            if form is None:
                _folder_upload_capacity.release()
            else:
                await _close_unsubmitted_folder_form(form)


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
    if payload.expected_version is None:
        raise ConflictError(
            "保存请求缺少文件版本号，请刷新编辑器后重试",
            code=40904,
            next_action="刷新编辑器后重新应用本地草稿；服务器文件未被修改",
        )
    ver = code_file_service.update_content(
        db,
        user,
        file_id,
        payload.content,
        payload.change_desc,
        expected_version=payload.expected_version,
    )
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
