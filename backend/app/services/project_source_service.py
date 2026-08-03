"""项目源码归档与远程源码导入。

归档只读取当前用户可访问项目的 active CodeFile，保持数据库中的相对路径，
不会把宿主机路径或敏感运行时文件加入下载包。远程导入只接受公开 HTTPS
归档地址，并在请求和重定向两处阻断内网地址，避免把导入功能变成 SSRF。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session, undefer

from app.ai.language_detector import detect_language
from app.core.config import settings
from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.agent_capability import SandboxEnvironment
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.user import User
from app.schemas.project import ProjectIn
from app.services import audit_service, code_file_service, project_service
from app.services.project_member_service import require_project_access
from app.utils.encoding_utils import BASE64_PREFIX, MAX_AUDIT_TEXT_LINES_PER_FILE, to_utf8
from app.utils.archive_extractor import (
    ARCHIVE_EXTENSIONS,
    ArchiveMember,
    is_archive,
    read_archive_members,
)
from app.utils.malware_scanner import MalwareScanner, ScanResult
from app.utils.public_http import PinnedPublicUrl, pin_public_http_url
from app.utils.source_archive_gate import source_archive_workload

MAX_REDIRECTS = 3
MAX_QUARANTINE_FILES = 10_000
MAX_RECORDED_THREATS = 100
CLAMAV_INSTREAM_SAFE_BYTES = 24 * 1024 * 1024
AUDIT_RUNNING_STALE_AFTER = timedelta(minutes=30)


def _strict_zip_members(
    raw: bytes,
    filename: str = "source.zip",
) -> tuple[list[ArchiveMember], dict]:
    """兼容旧调用名：严格读取任一受支持归档，不过滤隔离证据成员。"""
    members, envelope = read_archive_members(
        raw,
        filename,
        filter_sensitive=False,
        strict_paths=True,
    )
    if not 1 <= len(members) <= MAX_QUARANTINE_FILES:
        raise ValidationError(
            f"隔离源码包文件数必须在 1-{MAX_QUARANTINE_FILES} 之间",
            code=40001,
        )
    return members, envelope


def _scan_result_payload(result: ScanResult) -> dict:
    return {
        "engine": result.engine,
        "result": result.result,
        "threat_name": result.threat_name,
        "duration_ms": result.duration_ms,
        "degraded": result.degraded,
        "detail": result.detail,
    }


def _scan_quarantined_zip(
    raw: bytes,
    filename: str,
    members: list[ArchiveMember],
) -> tuple[str, int, str]:
    """ClamAV 扫描可传输的外层包，YARA 遍历全部成员。"""
    scanner = MalwareScanner(clamav_timeout=settings.source_archive_clamav_timeout)
    if len(raw) <= CLAMAV_INSTREAM_SAFE_BYTES:
        outer_result = scanner.scan_clamav(raw, filename)
    else:
        outer_result = ScanResult(
            engine="clamav",
            result="degraded",
            degraded=True,
            detail="外层归档超过 clamd INSTREAM 传输窗口，已改为全成员 YARA 扫描",
        )
    result_counts = {"clean": 0, "infected": 0, "degraded": 0, "error": 0, "timeout": 0}
    threats: list[dict] = []
    yara_started_at = time.monotonic()
    yara_deadline = yara_started_at + settings.source_archive_yara_total_timeout
    skipped_due_deadline = 0
    yara_deadline_reached = False
    for member in members:
        path = member.path
        content = member.content
        if not yara_deadline_reached and time.monotonic() >= yara_deadline:
            yara_deadline_reached = True
        if b"\x00" not in content and content.count(b"\n") + 1 > MAX_AUDIT_TEXT_LINES_PER_FILE:
            raise ValidationError(
                f"隔离源码包文本成员超过 {MAX_AUDIT_TEXT_LINES_PER_FILE} 行: {path}",
                code=40001,
            )
        if yara_deadline_reached:
            skipped_due_deadline += 1
            continue
        member_result = scanner.scan_yara(content, path)
        result_counts[member_result.result] = result_counts.get(member_result.result, 0) + 1
        if member_result.result == "infected" and len(threats) < MAX_RECORDED_THREATS:
            threats.append({
                "path": path,
                "engine": member_result.engine,
                "threat_name": member_result.threat_name,
            })

    member_infected = result_counts.get("infected", 0)
    infected = outer_result.result == "infected" or member_infected > 0
    has_error = outer_result.result in {"error", "timeout"} or (
        result_counts.get("error", 0) + result_counts.get("timeout", 0) > 0
    ) or skipped_due_deadline > 0
    has_degraded = outer_result.result == "degraded" or result_counts.get("degraded", 0) > 0
    malware_status = "infected" if infected else "error" if has_error else "degraded" if has_degraded else "clean"
    threat_count = member_infected or (1 if outer_result.result == "infected" else 0)
    summary = {
        "scope": "whole_archive",
        "outer_clamav": _scan_result_payload(outer_result),
        "member_yara": {
            "total": len(members),
            "scanned": sum(result_counts.values()),
            "skipped_due_deadline": skipped_due_deadline,
            "total_timeout_seconds": settings.source_archive_yara_total_timeout,
            "duration_ms": int((time.monotonic() - yara_started_at) * 1000),
            "result_counts": result_counts,
        },
        "threats": threats,
        "threat_sample_truncated": member_infected > len(threats),
    }
    return malware_status, threat_count, json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
                return base64.b64decode(content[len(prefix) :])
            except (ValueError, TypeError):
                return b""
        return b""
    return (row.content or "").encode("utf-8")


def source_archive_to_dict(row: ProjectSourceArchive) -> dict:
    """返回不触发 deferred BLOB 加载的隔离归档摘要。"""
    malware_status = str(row.malware_status)
    return {
        "id": row.id,
        "project_id": row.project_id,
        "original_filename": row.original_filename,
        "archive_sha256": row.archive_sha256,
        "compressed_size": row.compressed_size,
        "expanded_size": row.expanded_size,
        "file_count": row.file_count,
        "max_member_size": row.max_member_size,
        "max_compression_ratio": row.max_compression_ratio,
        "storage_status": row.storage_status,
        "malware_status": malware_status,
        "audit_status": row.audit_status,
        "audit_started_at": row.audit_started_at,
        "audit_heartbeat_at": row.audit_heartbeat_at,
        "audit_completed_at": row.audit_completed_at,
        "quarantined": True,
        "threat_count": row.threat_count,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def _active_source_archive(
    db: Session,
    project_id: int,
    *,
    for_update: bool = False,
) -> ProjectSourceArchive | None:
    query = (
        db.query(ProjectSourceArchive)
        .filter(
            ProjectSourceArchive.project_id == project_id,
            ProjectSourceArchive.storage_status == "active",
        )
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def get_source_archive_metadata(
    db: Session,
    user: User,
    project_id: int,
) -> dict | None:
    require_project_access(db, project_id, user, need_write=False)
    row = _active_source_archive(db, project_id)
    return source_archive_to_dict(row) if row else None


def _utc_value(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def begin_source_archive_audit(db: Session, project_id: int) -> str | None:
    """原子启动隔离归档审计；活动运行未过期时拒绝重复执行。"""
    row = _active_source_archive(db, project_id, for_update=True)
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    heartbeat = _utc_value(row.audit_heartbeat_at or row.audit_started_at or row.update_time)
    if (
        row.audit_status == "running"
        and heartbeat is not None
        and now - heartbeat < AUDIT_RUNNING_STALE_AFTER
    ):
        db.rollback()
        raise ConflictError("该隔离源码包已有白盒审计正在运行", code=40902)
    row.audit_status = "running"
    row.audit_run_id = uuid.uuid4().hex
    row.audit_started_at = now
    row.audit_heartbeat_at = now
    row.audit_completed_at = None
    # 当前运行期间保留上一份已完成报告，避免进程退出后唯一审计证据被清空。
    db.commit()
    return row.audit_run_id


def touch_source_archive_audit(db: Session, project_id: int, audit_run_id: str) -> bool:
    """刷新运行中整包审计的心跳。"""
    updated = (
        db.query(ProjectSourceArchive)
        .filter(
            ProjectSourceArchive.project_id == project_id,
            ProjectSourceArchive.storage_status == "active",
            ProjectSourceArchive.audit_status == "running",
            ProjectSourceArchive.audit_run_id == audit_run_id,
        )
        .update(
            {ProjectSourceArchive.audit_heartbeat_at: datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    )
    if updated:
        db.commit()
        return True
    db.rollback()
    return False


def finish_source_archive_audit(
    db: Session,
    project_id: int,
    status: str,
    result_data: dict | None,
    *,
    audit_run_id: str,
) -> bool:
    """持久化隔离归档审计终态和与原包摘要绑定的完整结果。"""
    if status not in {"succeeded", "failed", "blocked", "cancelled"}:
        raise ValueError("隔离包审计终态不合法")
    row = _active_source_archive(db, project_id, for_update=True)
    if row is None:
        return False
    if not audit_run_id or row.audit_run_id != audit_run_id or row.audit_status != "running":
        db.rollback()
        return False
    now = datetime.now(timezone.utc)
    payload = dict(result_data or {})
    payload.setdefault("source_archive_sha256", row.archive_sha256)
    payload.setdefault("source_archive_filename", row.original_filename)
    payload.setdefault("audit_run_id", audit_run_id)
    row.audit_status = status
    row.audit_heartbeat_at = now
    row.audit_completed_at = now
    row.audit_result_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    db.commit()
    return True


def get_source_archive_audit_result(
    db: Session,
    user: User,
    project_id: int,
) -> dict | None:
    """显式读取已持久化白盒审计结果；普通项目或未执行时返回 None。"""
    require_project_access(db, project_id, user, need_write=False)
    row = (
        db.query(ProjectSourceArchive)
        .options(undefer(ProjectSourceArchive.audit_result_json))
        .filter(
            ProjectSourceArchive.project_id == project_id,
            ProjectSourceArchive.storage_status == "active",
        )
        .first()
    )
    if row is None or not row.audit_result_json:
        return None
    try:
        result = json.loads(row.audit_result_json)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("隔离源码审计结果已损坏") from exc
    if result.get("source_archive_sha256") != row.archive_sha256:
        raise RuntimeError("隔离源码审计结果与当前原包摘要不一致")
    if result.get("audit_run_id") != row.audit_run_id:
        raise RuntimeError("隔离源码审计结果与当前运行代次不一致")
    return {
        "status": row.audit_status,
        "started_at": row.audit_started_at,
        "completed_at": row.audit_completed_at,
        "result": result,
    }


def ingest_source_archive_bytes(
    db: Session,
    user: User,
    project_id: int,
    *,
    raw: bytes,
    filename: str,
) -> dict:
    """串行执行隔离归档解包与恶意扫描，并在异常时释放数据库行锁。"""
    try:
        with source_archive_workload():
            return _ingest_source_archive_bytes_locked(
                db,
                user,
                project_id,
                raw=raw,
                filename=filename,
            )
    except Exception:
        db.rollback()
        raise


def _ingest_source_archive_bytes_locked(
    db: Session,
    user: User,
    project_id: int,
    *,
    raw: bytes,
    filename: str,
) -> dict:
    """将可能含恶意代码的源码归档作为不可编辑整包证据存入隔离表。"""
    require_project_access(db, project_id, user, need_write=True)
    project = (
        db.query(Project)
        .filter(Project.id == project_id)
        .with_for_update()
        .first()
    )
    if project is None or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if not raw:
        raise ValidationError("隔离源码包内容为空", code=40001)
    safe_filename = PurePosixPath((filename or "").replace("\\", "/")).name
    if not safe_filename or not is_archive(safe_filename):
        raise ValidationError("隔离整包审计不支持该归档格式", code=41500)
    existing_files = db.query(CodeFile.id).filter(
        CodeFile.project_id == project_id,
        CodeFile.status == "active",
    ).count()
    if existing_files:
        raise ConflictError("已有可编辑源码文件的项目不能混入隔离归档", code=40901)
    if _active_source_archive(db, project_id) is not None:
        raise ConflictError("项目已有活动的隔离源码归档", code=40901)
    active_deployment = db.query(SandboxEnvironment.id).filter(
        SandboxEnvironment.project_id == project_id,
        SandboxEnvironment.purpose == "deploy",
        SandboxEnvironment.status.in_(("queued", "dispatching", "running", "ready", "stopping")),
    ).first()
    if active_deployment is not None:
        raise ConflictError("项目存在活动的持续部署，请先关闭沙箱再上传隔离审计包", code=40901)

    members, envelope = _strict_zip_members(raw, safe_filename)
    malware_status, threat_count, scan_summary_json = _scan_quarantined_zip(
        raw,
        safe_filename,
        members,
    )
    row = ProjectSourceArchive(
        project_id=project_id,
        owner_id=user.id,
        original_filename=safe_filename[:255],
        media_type="application/octet-stream",
        archive_sha256=hashlib.sha256(raw).hexdigest(),
        compressed_size=len(raw),
        expanded_size=envelope["expanded_size"],
        file_count=envelope["file_count"],
        max_member_size=envelope["max_member_size"],
        max_compression_ratio=envelope["max_compression_ratio"],
        storage_status="active",
        malware_status=malware_status,
        audit_status="not_started",
        threat_count=threat_count,
        scan_summary_json=scan_summary_json,
        archive_blob=raw,
    )
    try:
        db.add(row)
        db.flush()
        audit_service.log(
            db,
            user,
            "project_source_archive_ingest",
            target_type="project_source_archive",
            target_id=row.id,
            detail=(
                f"project={project_id}; sha256={row.archive_sha256}; files={row.file_count}; "
                f"malware={malware_status}; threats={threat_count}"
            ),
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return source_archive_to_dict(row)


def load_project_source_files(db: Session, user: User, project_id: int) -> list[CodeFile]:
    """读取普通 CodeFile，或将隔离原包临时投影为只读文件对象供审计 Agent 使用。"""
    require_project_access(db, project_id, user, need_write=False)
    archive_row = _active_source_archive(db, project_id)
    rows = (
        db.query(CodeFile)
        .filter(CodeFile.project_id == project_id, CodeFile.status == "active")
        .all()
    )
    if archive_row is not None and rows:
        raise RuntimeError("项目同时存在隔离归档与可编辑源码，已拒绝选择审计对象")
    if rows:
        return rows
    if archive_row is None:
        return []
    raw = bytes(archive_row.archive_blob)
    if len(raw) != archive_row.compressed_size or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        archive_row.archive_sha256,
    ):
        raise RuntimeError("隔离源码归档完整性校验失败")
    members, envelope = _strict_zip_members(raw, archive_row.original_filename)
    if envelope["file_count"] != archive_row.file_count or envelope["expanded_size"] != archive_row.expanded_size:
        raise RuntimeError("隔离源码归档清单与入库证据不一致")

    projected: list[CodeFile] = []
    for index, member in enumerate(members, start=1):
        path = member.path
        content_bytes = member.content
        if (
            b"\x00" not in content_bytes
            and content_bytes.count(b"\n") + 1 > MAX_AUDIT_TEXT_LINES_PER_FILE
        ):
            raise RuntimeError(
                f"隔离源码包文本成员超过审计资源上限: {path}"
            )
        text = to_utf8(content_bytes)
        is_binary = text.startswith(BASE64_PREFIX)
        projected.append(CodeFile(
            id=-index,
            project_id=project_id,
            file_name=PurePosixPath(path).name,
            file_path=path,
            language=detect_language(path),
            size_bytes=len(content_bytes),
            raw_size=len(content_bytes),
            line_count=0 if is_binary else text.count("\n") + 1,
            version_no=1,
            content="" if is_binary else text,
            status="active",
            is_binary=1 if is_binary else 0,
            # 原始二进制保存在归档 BLOB 中；投影层不重复保留 bytes + base64。
            original_blob=None,
        ))
    return projected


def build_source_archive(db: Session, user: User, project_id: int) -> tuple[bytes, str]:
    """构建项目源码 ZIP，并保持每个文件的原始相对路径。"""
    require_project_access(db, project_id, user, need_write=False)
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)

    archive_row = _active_source_archive(db, project_id)
    rows = (
        db.query(CodeFile)
        .filter(CodeFile.project_id == project_id, CodeFile.status == "active")
        .order_by(CodeFile.file_path.asc(), CodeFile.id.desc())
        .all()
    )
    if archive_row is not None and rows:
        raise RuntimeError("项目同时存在隔离归档与可编辑源码，已拒绝导出不确定内容")
    if archive_row is not None:
        content = bytes(archive_row.archive_blob)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if len(content) != archive_row.compressed_size or not hmac.compare_digest(
            actual_sha256,
            archive_row.archive_sha256,
        ):
            raise RuntimeError("隔离源码归档完整性校验失败")
        return content, archive_row.original_filename
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


def _pin_remote_url(url: str) -> PinnedPublicUrl:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("远程源码仅支持不带凭据的 HTTPS 公共地址", code=40001)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("远程源码地址端口格式无效", code=40001) from exc
    if port not in (None, 443):
        raise ValidationError("远程源码地址只允许 HTTPS 默认端口", code=40001)
    return pin_public_http_url(url, require_https=True)


def _assert_public_url(url: str) -> None:
    """兼容旧调用方的只校验入口。"""
    _pin_remote_url(url)


def _archive_name(url: str, headers: httpx.Headers) -> str:
    disposition = headers.get("content-disposition", "")
    name = disposition.split("filename=", 1)[-1].strip(" \"'") if "filename=" in disposition else ""
    if not name:
        name = PurePosixPath(urlsplit(url).path).name
    if not name.lower().endswith(ARCHIVE_EXTENSIONS):
        raise ValidationError("远程地址必须直接返回受支持的源码归档", code=41500)
    return name


def download_remote_archive(url: str) -> tuple[bytes, str]:
    """下载公开 HTTPS 源码归档并逐跳校验重定向，不设置业务大小上限。"""
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        target = _pin_remote_url(current)
        # 每一跳独立连接池，保证固定 IP、Host 和 TLS SNI 是同一个目标。
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "GET",
                target.request_url,
                headers={"Host": target.host_header},
                extensions=target.request_extensions,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValidationError("远程源码重定向缺少目标地址", code=40001)
                    from urllib.parse import urljoin

                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise ExternalServiceError(
                        f"远程源码下载失败(HTTP {response.status_code})",
                        code=50201,
                    )
                try:
                    declared = int(response.headers.get("content-length") or 0)
                except (TypeError, ValueError) as exc:
                    raise ExternalServiceError("远程源码响应长度无效", code=50201) from exc
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                return b"".join(chunks), _archive_name(current, response.headers)
    raise ValidationError("远程源码重定向次数过多", code=40001)


def import_remote_project(
    db: Session,
    user: User,
    *,
    url: str,
    project_name: str,
    description: str = "",
    language: str | None = None,
    audit_mode: bool = False,
) -> dict:
    """下载公开源码归档，按用户选择进入普通或隔离审计链路。"""
    raw, archive_name = download_remote_archive(url)
    project = project_service.create_project(
        db,
        user,
        ProjectIn(project_name=project_name, description=description or None, language=language or None),
    )
    try:
        if audit_mode:
            archive_data = ingest_source_archive_bytes(
                db,
                user,
                project.id,
                raw=raw,
                filename=archive_name,
            )
        else:
            first_id, _, _ = code_file_service._upload_archive(
                db,
                user,
                project.id,
                raw,
                archive_name,
            )
    except Exception:
        project.status = "deleted"
        db.commit()
        raise
    if audit_mode:
        return {
            "id": project.id,
            "file_count": archive_data["file_count"],
            "first_file_id": None,
            "source_url": url,
            "source_mode": "audit_archive",
            "source_archive": archive_data,
        }
    count = db.query(CodeFile.id).filter(CodeFile.project_id == project.id, CodeFile.status == "active").count()
    return {
        "id": project.id,
        "file_count": count,
        "first_file_id": first_id,
        "source_url": url,
        "source_mode": "files",
    }
