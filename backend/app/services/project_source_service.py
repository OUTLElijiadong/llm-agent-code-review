"""项目源码归档与远程源码导入。

归档只读取当前用户可访问项目的 active CodeFile，保持数据库中的相对路径，
不会把宿主机路径或敏感运行时文件加入下载包。远程导入只接受公开 HTTPS
归档地址，并在请求和重定向两处阻断内网地址，避免把导入功能变成 SSRF。
"""
from __future__ import annotations

import ipaddress
import base64
import io
import re
import socket
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectIn
from app.services import code_file_service, project_service
from app.services.project_member_service import require_project_access
from app.utils.encoding_utils import BASE64_PREFIX

MAX_REMOTE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 3
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def _safe_archive_path(path: str) -> str | None:
    normalized = (path or "").replace("\\", "/").lstrip("/")
    if not normalized:
        return None
    parts = PurePosixPath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts) or normalized.startswith("/"):
        return None
    return "/".join(parts)


def _file_bytes(row: CodeFile) -> bytes:
    if row.is_binary:
        if row.original_blob is not None:
            return bytes(row.original_blob)
        content = row.content or ""
        prefix = BASE64_PREFIX
        if content.startswith(prefix):
            try:
                return base64.b64decode(content[len(prefix):])
            except (ValueError, TypeError):
                return b""
        return b""
    return (row.content or "").encode("utf-8")


def build_source_archive(db: Session, user: User, project_id: int) -> tuple[bytes, str]:
    """构建项目源码 ZIP，并保持每个文件的原始相对路径。"""
    require_project_access(db, project_id, user, need_write=False)
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)

    rows = (
        db.query(CodeFile)
        .filter(CodeFile.project_id == project_id, CodeFile.status == "active")
        .order_by(CodeFile.file_path.asc(), CodeFile.id.desc())
        .all()
    )
    out = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            path = _safe_archive_path(row.file_path or row.file_name)
            if not path or path in used:
                continue
            used.add(path)
            archive.writestr(path, _file_bytes(row))
    base_name = re.sub(
        r"[\\/\x00-\x1f\x7f]+",
        "_",
        str(project.project_name or f"project_{project_id}"),
    ).strip(" ._")[:180]
    filename = f"{base_name or f'project_{project_id}'}.zip"
    return out.getvalue(), filename


def _assert_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("远程源码仅支持不带凭据的 HTTPS 公共地址", code=40001)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("远程源码地址端口格式无效", code=40001) from exc
    if port not in (None, 443):
        raise ValidationError("远程源码地址只允许 HTTPS 默认端口", code=40001)
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise ValidationError("无法解析远程源码地址", code=40001) from exc
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValidationError("远程源码地址解析到内网或保留地址", code=40001)


def _archive_name(url: str, headers: httpx.Headers) -> str:
    disposition = headers.get("content-disposition", "")
    name = disposition.split("filename=", 1)[-1].strip(" \"'") if "filename=" in disposition else ""
    if not name:
        name = PurePosixPath(urlsplit(url).path).name
    if not name.lower().endswith(ARCHIVE_SUFFIXES):
        raise ValidationError("远程地址必须直接返回 ZIP/TAR 源码归档", code=41500)
    return name


def download_remote_archive(url: str) -> tuple[bytes, str]:
    """下载公开 HTTPS 源码归档，限制大小并逐跳校验重定向。"""
    current = url.strip()
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _assert_public_url(current)
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValidationError("远程源码重定向缺少目标地址", code=40001)
                    from urllib.parse import urljoin
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise ExternalServiceError(
                        f"远程源码下载失败(HTTP {response.status_code})", code=50201,
                    )
                try:
                    declared = int(response.headers.get("content-length") or 0)
                except (TypeError, ValueError) as exc:
                    raise ExternalServiceError("远程源码响应长度无效", code=50201) from exc
                if declared > MAX_REMOTE_BYTES:
                    raise ValidationError("远程源码归档超过 20MB 限制", code=41300)
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > MAX_REMOTE_BYTES:
                        raise ValidationError("远程源码归档超过 20MB 限制", code=41300)
                    chunks.append(chunk)
                return b"".join(chunks), _archive_name(current, response.headers)
    raise ValidationError("远程源码重定向次数过多", code=40001)


def import_remote_project(db: Session, user: User, *, url: str, project_name: str, description: str = "", language: str | None = None) -> dict:
    """下载公开源码归档并复用本地归档入库链路。"""
    raw, archive_name = download_remote_archive(url)
    project = project_service.create_project(
        db,
        user,
        ProjectIn(project_name=project_name, description=description or None, language=language or None),
    )
    try:
        first_id, _, _ = code_file_service._upload_archive(db, user, project.id, raw, archive_name)
    except Exception:
        project.status = "deleted"
        db.commit()
        raise
    count = db.query(CodeFile.id).filter(CodeFile.project_id == project.id, CodeFile.status == "active").count()
    return {"id": project.id, "file_count": count, "first_file_id": first_id, "source_url": url}
