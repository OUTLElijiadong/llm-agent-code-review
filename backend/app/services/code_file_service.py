"""
代码文件管理服务模块
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.ai.language_detector import detect_language
from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.user import User
from app.schemas.code_file import CodeFileIn
from app.utils.encoding_utils import to_utf8
from app.utils.file_validator import validate_filename, validate_size


def list_files(db: Session, user: User, project_id: int = None, language: str = "",
               keyword: str = "", exclude_binary: bool = False,
               page: int = 1, page_size: int = 20) -> dict:
    """查询代码文件列表

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID(必填)
        language: 语言过滤
        keyword: 文件名搜索
        exclude_binary: 排除图片/二进制等不可审查文件
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    from app.core.exceptions import ValidationError
    from app.core.pagination import Pagination

    BINARY_LANGS = {"binary", "unknown", "image", "jpeg", "jpg", "png", "gif", "svg"}

    # 防御性编程: 必须限定项目,否则会跨用户返回全库文件。
    if not project_id:
        raise ValidationError("project_id 必填", code=40001)

    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)

    q = db.query(CodeFile).filter(
        CodeFile.status == "active", CodeFile.project_id == project_id)
    if exclude_binary:
        q = q.filter(~CodeFile.language.in_(BINARY_LANGS))
    if language:
        q = q.filter(CodeFile.language == language)
    if keyword:
        q = q.filter(CodeFile.file_name.contains(keyword))

    total = q.count()
    pagination = Pagination(page, page_size, total)
    items = q.order_by(CodeFile.create_time.desc()).offset(pagination.offset).limit(pagination.page_size).all()
    return pagination.to_dict(items)


def upload(db: Session, user: User, project_id: int, upload_file: UploadFile,
           file_path: Optional[str] = None, language: Optional[str] = None) -> tuple:
    """上传代码文件(Multipart)

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        upload_file: 上传的文件对象
        file_path: 可选逻辑路径
        language: 可选语言标识,为空则自动识别

    Returns:
        tuple[int, str, int]: (文件ID, 语言, 版本号)
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)

    raw = upload_file.file.read()
    validate_size(len(raw), settings.max_upload_size)
    safe_name = validate_filename(upload_file.filename, settings.allowed_extensions)
    text = to_utf8(raw)
    lang = language or detect_language(safe_name)

    return _create_file(db, user, project_id, safe_name, file_path, lang, text, "初始上传")


def create_file(db: Session, user: User, payload: CodeFileIn) -> tuple:
    """在线新增代码文件

    Args:
        db: 数据库会话
        user: 当前用户
        payload: 文件创建请求体

    Returns:
        tuple[int, str, int]: (文件ID, 语言, 版本号)
    """
    project = db.get(Project, payload.project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)

    safe_name = validate_filename(payload.file_name, settings.allowed_extensions)
    lang = payload.language or detect_language(safe_name)

    return _create_file(db, user, payload.project_id, safe_name, payload.file_path, lang, payload.content, "在线创建")


def _create_file(db: Session, user: User, project_id: int, file_name: str,
                 file_path: Optional[str], language: str, content: str, change_desc: str) -> tuple:
    """内部: 创建文件及其初始版本"""
    code_file = CodeFile(
        project_id=project_id,
        file_name=file_name,
        file_path=file_path or file_name,
        language=language,
        size_bytes=len(content.encode("utf-8")),
        line_count=content.count("\n") + 1,
        version_no=1,
        content=content,
        status="active",
    )
    db.add(code_file)
    db.flush()
    db.add(CodeVersion(
        file_id=code_file.id,
        version_no=1,
        content=content,
        change_desc=change_desc,
        operator_id=user.id,
        create_time=datetime.now(timezone.utc),
    ))
    db.commit()
    db.refresh(code_file)
    return code_file.id, language, 1


def get_file(db: Session, user: User, file_id: int) -> CodeFile:
    """获取代码文件详情(含内容)

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID

    Returns:
        CodeFile: 文件ORM对象
    """
    code_file = db.get(CodeFile, file_id)
    if not code_file or code_file.status == "deleted":
        raise NotFoundError("文件不存在", code=40400)
    project = db.get(Project, code_file.project_id)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)
    return code_file


def update_content(db: Session, user: User, file_id: int, content: str, change_desc: Optional[str] = None) -> int:
    """更新文件内容并生成新版本

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID
        content: 新的代码内容
        change_desc: 修改说明

    Returns:
        int: 新版本号
    """
    code_file = get_file(db, user, file_id)
    code_file.content = content
    code_file.size_bytes = len(content.encode("utf-8"))
    code_file.line_count = content.count("\n") + 1
    code_file.version_no += 1
    db.add(CodeVersion(
        file_id=code_file.id,
        version_no=code_file.version_no,
        content=content,
        change_desc=change_desc or "更新",
        operator_id=user.id,
        create_time=datetime.now(timezone.utc),
    ))
    db.commit()
    return code_file.version_no


def rename_file(db: Session, user: User, file_id: int, file_name: str, file_path: Optional[str] = None) -> None:
    """重命名文件

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID
        file_name: 新文件名
        file_path: 可选新逻辑路径
    """
    code_file = get_file(db, user, file_id)
    validate_filename(file_name, settings.allowed_extensions)
    code_file.file_name = file_name
    if file_path is not None:
        code_file.file_path = file_path
    db.commit()


def delete_file(db: Session, user: User, file_id: int) -> None:
    """软删除文件

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID
    """
    code_file = get_file(db, user, file_id)
    code_file.status = "deleted"
    db.commit()


def list_versions(db: Session, user: User, file_id: int, page: int = 1, page_size: int = 20) -> dict:
    """获取文件版本历史列表

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    from app.core.pagination import Pagination

    get_file(db, user, file_id)
    q = db.query(CodeVersion).filter(CodeVersion.file_id == file_id)
    total = q.count()
    pagination = Pagination(page, page_size, total)
    items = q.order_by(CodeVersion.version_no.desc()).offset(pagination.offset).limit(pagination.page_size).all()
    return pagination.to_dict(items)


def get_version(db: Session, user: User, file_id: int, version_no: int) -> CodeVersion:
    """获取指定版本的内容

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID
        version_no: 版本号

    Returns:
        CodeVersion: 版本ORM对象
    """
    get_file(db, user, file_id)
    version = db.query(CodeVersion).filter(
        CodeVersion.file_id == file_id, CodeVersion.version_no == version_no).first()
    if not version:
        raise NotFoundError("版本不存在", code=40400)
    return version


def restore_version(db: Session, user: User, file_id: int, version_no: int) -> int:
    """回滚到指定历史版本(作为新版本写入)

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID
        version_no: 要回滚到的版本号

    Returns:
        int: 新版本号
    """
    version = get_version(db, user, file_id, version_no)
    return update_content(db, user, file_id, version.content, f"回滚到版本v{version_no}")
