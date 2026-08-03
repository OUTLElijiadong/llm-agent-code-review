"""
代码文件管理服务模块

v2 增强(2026-06-25):
- 压缩包上传通过 libarchive 自动解压 → 批量创建文件,带路径与解压倍率防护
- 二进制文件支持:is_binary=1 时 original_blob 存原始字节,content 存 base64(向后兼容)
- 编辑器不再展示 base64 字符串:API 层 is_binary=1 时 content 置空,前端走下载接口

T06 增强(2026-06-25):
- 上传流程串行集成:MIME 白名单 → MalwareScanner 双引擎扫描
- 压缩包解压后对每个内部文件递归执行恶意软件扫描
- CodeFile 入库时写入 raw_size 字段(用于项目总大小校验)
"""
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import UploadFile
from loguru import logger
from sqlalchemy.orm import Session

from app.ai.language_detector import detect_language
from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.user import User
from app.schemas.code_file import CodeFileIn
from app.utils.archive_extractor import ExtractedFile, extract_archive, is_archive
from app.utils.encoding_utils import BASE64_PREFIX, to_utf8
from app.utils.file_validator import (
    validate_filename,
    validate_mime,
)
from app.utils.malware_scanner import get_scanner


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
    if project.user_id != user.id and user.role not in {"admin", "super_admin"}:
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
    - 常见源码归档由 libarchive 自动解压 → 批量创建文件
    - 二进制文件(图片/可执行文件等)单独存储 original_blob,content 存 base64(向后兼容)
    - 文本文件按原逻辑处理

    T06 增强:
    - 上传不设固定字节数上限，仍执行 MIME、恶意软件和归档安全校验
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
        ValueError: MIME 校验/恶意软件扫描失败
    """
    try:
        _check_project_access(db, user, project_id)

        raw = upload_file.file.read()
        filename = upload_file.filename or ""

        # 压缩包会在解包后逐成员扫描，避免 ClamAV INSTREAM 对外层大包的传输上限。
        _validate_upload_security(db, project_id, filename, raw)

        # v2: 压缩包自动解压
        if is_archive(filename):
            return _upload_archive(db, user, project_id, raw, filename)

        # 普通文件:校验扩展名
        safe_name = validate_filename(filename, settings.allowed_extensions)
        return _upload_single_file(db, user, project_id, safe_name, raw, file_path, language)
    except Exception:
        db.rollback()
        raise


def _validate_upload_security(
    db: Session, project_id: int, filename: str, raw: bytes,
) -> None:
    """上传前执行类型校验；普通文件立即扫描，归档改为逐成员扫描。

    Args:
        db: 数据库会话(用于查询项目当前总大小)
        project_id: 项目ID
        filename: 上传文件名(含扩展名)
        raw: 文件原始字节

    Returns:
        None: 无返回值,校验通过即静默返回

    Raises:
        ValueError: MIME 不在白名单或检测到恶意软件
    """
    # 1. MIME 白名单校验(同时拦截可执行文件扩展名)
    if not validate_mime(filename):
        raise ValueError(f"不支持的文件类型: {filename}")

    if is_archive(filename):
        return

    # 普通文件继续走 ClamAV + YARA 双引擎；生产默认 fail-closed。
    scan_result = get_scanner().scan(raw, filename)
    _enforce_scan_result(scan_result, filename)


def _enforce_scan_result(
    scan_result, filename: str, archive_member: bool = False,
) -> None:
    """根据扫描结论和环境策略决定是否允许文件继续入库。

    Args:
        scan_result: MalwareScanner 返回的 ScanResult。
        filename: 当前文件名，仅用于可操作的拒绝提示。
        archive_member: 是否为压缩包内部文件。

    Returns:
        None: 允许继续上传时静默返回。

    Raises:
        ValueError: 检测到威胁，或生产 fail-closed 下扫描能力不可信。
    """
    prefix = f"压缩包内文件 {filename} " if archive_member else ""
    if scan_result.result == "infected":
        raise ValueError(
            f"{prefix}检测到恶意软件: {scan_result.threat_name},文件已被拒绝"
        )

    scan_untrusted = (
        scan_result.result in {"degraded", "error", "timeout"}
        or bool(scan_result.degraded)
        or scan_result.result != "clean"
    )
    if settings.malware_scan_fail_closed and scan_untrusted:
        logger.error(
            "[upload] 恶意软件扫描不可用，按 fail-closed 拒绝 "
            f"(file={filename}, engine={scan_result.engine}, result={scan_result.result})"
        )
        raise ValueError(
            f"{prefix}恶意软件扫描服务暂不可用,为保证安全已拒绝上传"
        )
    if scan_untrusted:
        logger.warning(
            "[upload] 恶意软件扫描降级，非生产策略允许继续 "
            f"(file={filename}, engine={scan_result.engine}, result={scan_result.result})"
        )


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
    _enforce_scan_result(scan_result, filename, archive_member=True)


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

    T06 增强:
    - 解压后对每个内部文件执行恶意软件扫描,任一命中即拒绝整批上传。
    - 解压失败(含 zip slip/超限/损坏)统一转为 ValueError 抛出,符合 T06 规范。

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        raw: 压缩包字节流
        archive_name: 压缩包文件名(用于判断格式与变更说明)

    Returns:
        tuple[int, str, int]: (首个解压文件ID, 语言, 版本号)

    Raises:
        ValueError: 解压失败/文件为空/路径不安全/压缩包内文件检测到恶意软件
    """
    try:
        extracted_files: list[ExtractedFile] = extract_archive(raw, archive_name)
    except ValidationError as e:
        # T06: 将 archive_extractor 的 ValidationError 统一转为 ValueError
        raise ValueError(f"压缩包解压失败: {e.message}") from e

    if not extracted_files:
        raise ValueError("压缩包解压失败: 压缩包内没有可用文件")

    # 在任何落库前完成全部成员扫描，确保恶意内容不会造成半包入库。
    for ef in extracted_files:
        scan_bytes = ef.raw_bytes if ef.is_binary else ef.content.encode("utf-8")
        _scan_extracted_file(ef.name, scan_bytes)

    first_id: Optional[int] = None
    first_lang: str = "plaintext"
    first_version: int = 1

    try:
        for ef in extracted_files:
            # 压缩包内文件已通过 archive_extractor 的安全过滤,跳过扩展名校验
            # (压缩包可能包含无扩展名的配置文件、Dockerfile 等)
            if ef.is_binary:
                content_b64 = to_utf8(ef.raw_bytes) if ef.raw_bytes else ""
                file_id, lang, version = _create_file(
                    db, user, project_id, ef.name, ef.path, ef.language, content_b64,
                    f"从压缩包 {archive_name} 解压",
                    is_binary=1, original_blob=ef.raw_bytes, commit=False,
                )
            else:
                file_id, lang, version = _create_file(
                    db, user, project_id, ef.name, ef.path, ef.language, ef.content,
                    f"从压缩包 {archive_name} 解压", commit=False,
                )
            if first_id is None:
                first_id, first_lang, first_version = file_id, lang, version
        db.commit()
    except Exception:
        db.rollback()
        raise

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
    # 与隔离整包上传使用同一 Project 行锁。锁保持到文件提交，跨 worker
    # 阻止两条写路径同时通过互斥检查后形成混合源码项目。
    project = (
        db.query(Project)
        .filter(Project.id == project_id)
        .with_for_update()
        .first()
    )
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role not in {"admin", "super_admin"}:
        raise ForbiddenError("无访问权限", code=40300)
    if db.query(ProjectSourceArchive.id).filter(
        ProjectSourceArchive.project_id == project_id,
        ProjectSourceArchive.storage_status == "active",
    ).first():
        raise ValidationError(
            "隔离整包审计项目不能混入可编辑源码文件",
            code=40901,
        )
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
    try:
        _check_project_access(db, user, payload.project_id)
        safe_name = validate_filename(payload.file_name, settings.allowed_extensions)
        lang = payload.language or detect_language(safe_name)

        return _create_file(
            db,
            user,
            payload.project_id,
            safe_name,
            payload.file_path,
            lang,
            payload.content,
            "在线创建",
        )
    except Exception:
        db.rollback()
        raise


def _create_file(db: Session, user: User, project_id: int, file_name: str,
                 file_path: Optional[str], language: str, content: str, change_desc: str,
                 is_binary: int = 0, original_blob: Optional[bytes] = None,
                 commit: bool = True) -> tuple:
    """内部: 创建文件及其初始版本

    v2 增强:支持 is_binary/original_blob 参数,二进制文件单独存储原始字节。
    T06 增强:写入 raw_size 字段(用于项目总大小 500MB 校验)。

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
    # T06: raw_size 用于项目总大小校验(二进制文件用原始字节,文本用 UTF-8 编码字节)
    raw_size = len(original_blob) if original_blob is not None else len(content.encode("utf-8"))
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
        raw_size=raw_size,
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
    if commit:
        db.commit()
        db.refresh(code_file)
    else:
        db.flush()
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
    if project.user_id != user.id and user.role not in {"admin", "super_admin"}:
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
    if project.user_id != user.id and user.role not in {"admin", "super_admin"}:
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


def get_file_meta(db: Session, user: User, file_id: int) -> dict:
    """获取代码文件元信息(不含内容,含实时计算的摘要)

    v3 新增:用于二进制文件展示提示卡片。返回文件元数据 + MIME 类型(按扩展名推断)
    + MD5/SHA-256 摘要(实时计算,不入库)。

    Args:
        db: 数据库会话
        user: 当前用户
        file_id: 文件ID

    Returns:
        dict: 元信息字典,字段对齐 CodeFileMetaOut schema

    Raises:
        NotFoundError: 文件不存在
        ForbiddenError: 无访问权限
    """
    import hashlib
    import mimetypes

    code_file = db.get(CodeFile, file_id)
    if not code_file or code_file.status == "deleted":
        raise NotFoundError("文件不存在", code=40400)
    project = db.get(Project, code_file.project_id)
    if project.user_id != user.id and user.role not in {"admin", "super_admin"}:
        raise ForbiddenError("无访问权限", code=40300)

    # MIME 类型按文件名扩展名推断;mimetypes 未知时回退到 application/octet-stream
    mime_type, _ = mimetypes.guess_type(code_file.file_name)
    if not mime_type:
        mime_type = "application/octet-stream" if code_file.is_binary == 1 else "text/plain"

    # 实时计算摘要:二进制文件从 original_blob 计算,文本文件从 content 编码计算
    md5_hash = ""
    sha256_hash = ""
    try:
        if code_file.is_binary == 1:
            if code_file.original_blob is not None:
                raw_bytes = code_file.original_blob
            else:
                # 兼容旧数据:从 base64 还原
                import base64
                content_str = code_file.content or ""
                raw_bytes = (
                    base64.b64decode(content_str[len(BASE64_PREFIX):])
                    if content_str.startswith(BASE64_PREFIX)
                    else b""
                )
        else:
            raw_bytes = (code_file.content or "").encode("utf-8")
        md5_hash = hashlib.md5(raw_bytes).hexdigest()
        sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
    except Exception as e:  # pragma: no cover - 摘要计算失败不应阻塞 meta 接口
        logger.warning(f"计算文件摘要失败 file_id={file_id}: {e}")

    return {
        "id": code_file.id,
        "file_name": code_file.file_name,
        "file_path": code_file.file_path,
        "language": code_file.language,
        "size_bytes": code_file.size_bytes,
        "raw_size": code_file.raw_size or 0,
        "line_count": code_file.line_count,
        "version_no": code_file.version_no,
        "is_binary": code_file.is_binary,
        "mime_type": mime_type,
        "md5_hash": md5_hash or None,
        "sha256_hash": sha256_hash or None,
        "create_time": code_file.create_time,
        "update_time": code_file.update_time,
    }


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
    # T06: 同步更新 raw_size,保持项目总大小校验准确性
    code_file.raw_size = len(content.encode("utf-8"))
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
