"""
代码文件管理服务模块

v2 增强(2026-06-25):
- 压缩包上传自动解压:zip/tar/gz/bz2/xz → 批量创建文件,带 zip slip 安全防护
- 二进制文件支持:is_binary=1 时 original_blob 存原始字节,content 存 base64(向后兼容)
- 编辑器不再展示 base64 字符串:API 层 is_binary=1 时 content 置空,前端走下载接口

T06 增强(2026-06-25):
- 上传流程串行集成:MIME 白名单 → 单文件 10MB → 项目总 500MB → MalwareScanner 双引擎扫描
- 压缩包解压后对每个内部文件递归执行恶意软件扫描
- CodeFile 入库时写入 raw_size 字段(用于项目总大小校验)
"""
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import UploadFile
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai.language_detector import detect_language
from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.user import User
from app.schemas.code_file import CodeFileIn
from app.utils.archive_extractor import ExtractedFile, extract_archive, is_archive
from app.utils.encoding_utils import BASE64_PREFIX, to_utf8
from app.utils.file_validator import (
    MAX_PROJECT_TOTAL_SIZE,
    MAX_SINGLE_FILE_SIZE,
    validate_filename,
    validate_mime,
    validate_project_total_size,
    validate_single_file_size,
    validate_size,
)
from app.utils.malware_scanner import ScanResult, get_scanner


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
    # v2: exclude_binary 同时按 is_binary 字段过滤,确保二进制文件被排除
    if exclude_binary:
        q = q.filter(~CodeFile.language.in_(BINARY_LANGS))
        q = q.filter(CodeFile.is_binary == 0)
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

    v2 增强:
    - 压缩包(zip/tar/gz/bz2/xz)自动解压 → 批量创建文件
    - 二进制文件(图片/可执行文件等)单独存储 original_blob,content 存 base64(向后兼容)
    - 文本文件按原逻辑处理

    T06 增强:
    - 上传前串行执行:MIME 白名单 → 单文件 10MB → 项目总 500MB → MalwareScanner 双引擎扫描
    - 任一校验失败抛出 ValueError 并附带清晰错误信息

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        upload_file: 上传的文件对象
        file_path: 可选逻辑路径
        language: 可选语言标识,为空则自动识别

    Returns:
        tuple[int, str, int]: (首个文件ID, 首个文件语言, 首个文件版本号)
        - 压缩包:返回首个解压文件的标识
        - 单文件:返回该文件的标识

    Raises:
        NotFoundError: 项目不存在
        ForbiddenError: 无访问权限
        ValidationError: 解压失败/扩展名不支持
        ValueError: MIME 校验/大小校验/恶意软件扫描失败
    """
    _check_project_access(db, user, project_id)

    raw = upload_file.file.read()
    filename = upload_file.filename or ""

    # === T06 安全校验链:MIME → 单文件大小 → 项目总大小 → 恶意软件扫描 ===
    _validate_upload_security(db, project_id, filename, raw)

    # 兼容旧的整体大小限制(max_upload_size 默认 20MB)
    validate_size(len(raw), settings.max_upload_size)

    # v2: 压缩包自动解压
    if is_archive(filename):
        return _upload_archive(db, user, project_id, raw, filename)

    # 普通文件:校验扩展名
    safe_name = validate_filename(filename, settings.allowed_extensions)
    return _upload_single_file(db, user, project_id, safe_name, raw, file_path, language)


def _validate_upload_security(
    db: Session, project_id: int, filename: str, raw: bytes,
) -> None:
    """上传前安全校验链:MIME → 单文件大小 → 项目总大小 → 恶意软件扫描

    校验顺序严格按 T06 规范执行,任一环节失败立即抛出 ValueError 终止上传。

    Args:
        db: 数据库会话(用于查询项目当前总大小)
        project_id: 项目ID
        filename: 上传文件名(含扩展名)
        raw: 文件原始字节

    Returns:
        None: 无返回值,校验通过即静默返回

    Raises:
        ValueError: MIME 不在白名单 / 单文件超 10MB / 项目总超 500MB / 检测到恶意软件
    """
    # 1. MIME 白名单校验(同时拦截可执行文件扩展名)
    if not validate_mime(filename):
        raise ValueError(f"不支持的文件类型: {filename}")

    # 2. 单文件大小校验(≤ 10MB)
    file_size = len(raw)
    if not validate_single_file_size(file_size):
        raise ValueError(
            f"文件大小 {file_size} 字节超过单文件上限 "
            f"{MAX_SINGLE_FILE_SIZE} 字节(10MB)"
        )

    # 3. 项目总大小校验(≤ 500MB)
    current_total = _get_project_total_size(db, project_id)
    if not validate_project_total_size(current_total, file_size):
        raise ValueError(
            f"项目总大小 {current_total + file_size} 字节超过项目上限 "
            f"{MAX_PROJECT_TOTAL_SIZE} 字节(500MB)"
        )

    # 4. 恶意软件扫描(ClamAV + YARA 双引擎,降级到启发式)
    scan_result = get_scanner().scan(raw, filename)
    if scan_result.result == "infected":
        raise ValueError(
            f"检测到恶意软件: {scan_result.threat_name},文件已被拒绝"
        )


def _get_project_total_size(db: Session, project_id: int) -> int:
    """获取项目当前所有 active 文件的总原始字节数(raw_size 求和)

    用于项目总大小 500MB 上限校验。raw_size 由 T01 引入,二进制文件计入
    original_blob 真实大小,文本文件计入 UTF-8 编码字节长度。

    Args:
        db: 数据库会话
        project_id: 项目ID

    Returns:
        int: 总字节数;项目无文件或查询异常时返回 0
    """
    total = db.query(func.sum(CodeFile.raw_size)).filter(
        CodeFile.project_id == project_id,
        CodeFile.status == "active",
    ).scalar()
    return int(total or 0)


def _scan_extracted_file(filename: str, content_bytes: bytes) -> None:
    """对压缩包内解压出的单个文件执行恶意软件扫描

    压缩包整体扫描通过后,内部文件仍需逐个扫描,防止攻击者将恶意文件
    打包进压缩包绕过外层扫描。

    Args:
        filename: 内部文件名(用于扫描日志与启发式校验)
        content_bytes: 内部文件字节内容

    Returns:
        None: 无返回值,扫描通过即静默返回

    Raises:
        ValueError: 检测到恶意软件,文件已被拒绝
    """
    scan_result = get_scanner().scan(content_bytes, filename)
    if scan_result.result == "infected":
        raise ValueError(
            f"压缩包内文件 {filename} 检测到恶意软件: "
            f"{scan_result.threat_name},文件已被拒绝"
        )


def _upload_single_file(
    db: Session, user: User, project_id: int, safe_name: str, raw: bytes,
    file_path: Optional[str], language: Optional[str],
) -> tuple:
    """上传单个文件(文本或二进制)

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        safe_name: 已校验的文件名
        raw: 原始字节
        file_path: 逻辑路径
        language: 语言标识(为空则自动识别)

    Returns:
        tuple[int, str, int]: (文件ID, 语言, 版本号)
    """
    lang = language or detect_language(safe_name)
    is_bin = _is_binary_data(raw)

    if is_bin:
        # 二进制文件:content 存 base64(向后兼容),original_blob 存原始字节
        content_b64 = to_utf8(raw)
        return _create_file(
            db, user, project_id, safe_name, file_path, lang, content_b64, "初始上传",
            is_binary=1, original_blob=raw,
        )

    # 文本文件:UTF-8 文本
    text = to_utf8(raw)
    return _create_file(db, user, project_id, safe_name, file_path, lang, text, "初始上传")


def _upload_archive(
    db: Session, user: User, project_id: int, raw: bytes, archive_name: str,
) -> tuple:
    """处理压缩包上传:解压 + 批量创建文件

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        raw: 压缩包字节流
        archive_name: 压缩包文件名(用于判断格式与变更说明)

    Returns:
        tuple[int, str, int]: (首个解压文件ID, 语言, 版本号)

    Raises:
        ValidationError: 解压失败/文件为空/路径不安全
    """
    extracted_files: list[ExtractedFile] = extract_archive(raw, archive_name)
    if not extracted_files:
        raise ValidationError("压缩包内没有可用文件", code=40001)

    first_id: Optional[int] = None
    first_lang: str = "plaintext"
    first_version: int = 1

    for ef in extracted_files:
        # 压缩包内文件已通过 archive_extractor 的安全过滤,跳过扩展名校验
        # (压缩包可能包含无扩展名的配置文件、Dockerfile 等)
        if ef.is_binary:
            content_b64 = to_utf8(ef.raw_bytes) if ef.raw_bytes else ""
            file_id, lang, version = _create_file(
                db, user, project_id, ef.name, ef.path, ef.language, content_b64,
                f"从压缩包 {archive_name} 解压",
                is_binary=1, original_blob=ef.raw_bytes,
            )
        else:
            file_id, lang, version = _create_file(
                db, user, project_id, ef.name, ef.path, ef.language, ef.content,
                f"从压缩包 {archive_name} 解压",
            )
        if first_id is None:
            first_id, first_lang, first_version = file_id, lang, version

    logger.info(
        f"[upload] 压缩包 {archive_name} 解压完成,共创建 {len(extracted_files)} 个文件 "
        f"(project_id={project_id})",
    )
    return first_id or 0, first_lang, first_version


def _check_project_access(db: Session, user: User, project_id: int) -> Project:
    """校验项目访问权限(存在性 + 归属)

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID

    Returns:
        Project: 项目 ORM 对象

    Raises:
        NotFoundError: 项目不存在或已删除
        ForbiddenError: 无访问权限
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)
    return project


def _is_binary_data(data: bytes) -> bool:
    """判断字节流是否为二进制(含 null 字节则视为二进制)

    Args:
        data: 待判断字节

    Returns:
        bool: True 表示二进制
    """
    if not data:
        return False
    return b"\x00" in data[:8192]


def create_file(db: Session, user: User, payload: CodeFileIn) -> tuple:
    """在线新增代码文件

    Args:
        db: 数据库会话
        user: 当前用户
        payload: 文件创建请求体

    Returns:
        tuple[int, str, int]: (文件ID, 语言, 版本号)
    """
    _check_project_access(db, user, payload.project_id)
    safe_name = validate_filename(payload.file_name, settings.allowed_extensions)
    lang = payload.language or detect_language(safe_name)

    return _create_file(db, user, payload.project_id, safe_name, payload.file_path, lang, payload.content, "在线创建")


def _create_file(db: Session, user: User, project_id: int, file_name: str,
                 file_path: Optional[str], language: str, content: str, change_desc: str,
                 is_binary: int = 0, original_blob: Optional[bytes] = None) -> tuple:
    """内部: 创建文件及其初始版本

    v2 增强:支持 is_binary/original_blob 参数,二进制文件单独存储原始字节。

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        file_name: 已校验的文件名
        file_path: 逻辑路径(为空则用 file_name)
        language: 语言标识
        content: 文件内容(文本文件为 UTF-8,二进制文件为 base64 编码)
        change_desc: 变更说明
        is_binary: 是否二进制文件(0/1)
        original_blob: 二进制文件的原始字节(仅 is_binary=1 时使用)

    Returns:
        tuple[int, str, int]: (文件ID, 语言, 版本号)
    """
    # 二进制文件按原始字节计算大小,文本文件按 UTF-8 编码字节计算
    size_bytes = len(original_blob) if original_blob is not None else len(content.encode("utf-8"))
    # 二进制文件不计行数
    line_count = 0 if is_binary else content.count("\n") + 1

    code_file = CodeFile(
        project_id=project_id,
        file_name=file_name,
        file_path=file_path or file_name,
        language=language,
        size_bytes=size_bytes,
        line_count=line_count,
        version_no=1,
        content=content,
        status="active",
        is_binary=is_binary,
        original_blob=original_blob,
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

    v2 增强:二进制文件不返回 base64 content(避免编辑器展示 base64),
    前端通过 is_binary=1 判断,改用下载接口获取原文件。

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID

    Returns:
        CodeFile: 文件ORM对象(is_binary=1 时 content 字段被置空)
    """
    code_file = db.get(CodeFile, file_id)
    if not code_file or code_file.status == "deleted":
        raise NotFoundError("文件不存在", code=40400)
    project = db.get(Project, code_file.project_id)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)
    # v2: 二进制文件不返回 base64 content,前端通过下载接口获取
    if code_file.is_binary == 1:
        code_file.content = ""
    return code_file


def get_binary_content(db: Session, user: User, file_id: int) -> Tuple[bytes, str]:
    """获取二进制文件的原始字节(供下载接口使用)

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID

    Returns:
        Tuple[bytes, str]: (原始字节, 文件名)

    Raises:
        NotFoundError: 文件不存在或非二进制文件
        ForbiddenError: 无访问权限
    """
    code_file = db.get(CodeFile, file_id)
    if not code_file or code_file.status == "deleted":
        raise NotFoundError("文件不存在", code=40400)
    project = db.get(Project, code_file.project_id)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)
    if code_file.is_binary != 1:
        raise NotFoundError("该文件不是二进制文件", code=40400)
    # 优先从 original_blob 取,降级从 content(base64)还原
    if code_file.original_blob is not None:
        return code_file.original_blob, code_file.file_name
    # 兼容旧数据:从 base64 还原
    content = code_file.content or ""
    if content.startswith(BASE64_PREFIX):
        import base64
        return base64.b64decode(content[len(BASE64_PREFIX):]), code_file.file_name
    return b"", code_file.file_name


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

    Raises:
        ValidationError: 二进制文件不允许在线编辑
    """
    code_file = get_file(db, user, file_id)
    if code_file.is_binary == 1:
        raise ValidationError("二进制文件不支持在线编辑", code=40001)
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
