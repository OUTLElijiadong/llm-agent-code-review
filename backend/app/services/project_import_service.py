"""可恢复的远程项目异步导入任务状态机。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import httpx
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
)
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.project_import_task import ProjectImportTask
from app.models.project_member import ProjectMember
from app.models.project_source_archive import ProjectSourceArchive
from app.models.project_source_revision import ProjectSourceRevision
from app.models.user import User
from app.schemas.project import ProjectIn
from app.services import audit_service, code_file_service, project_service, project_source_service

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED})
validate_remote_project_url = project_source_service.validate_remote_project_url


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _merge_progress(row: ProjectImportTask, progress: Mapping[str, Any]) -> None:
    result = _load_json(row.result_json)
    result["progress"] = dict(progress)
    row.result_json = _canonical_json(result)


def _request_payload(
    *,
    url: str,
    project_name: str,
    description: str,
    language: str | None,
    audit_mode: bool,
) -> dict[str, Any]:
    return {
        "audit_mode": bool(audit_mode),
        "description": str(description or ""),
        "language": str(language).strip() if language else None,
        "project_name": str(project_name or "").strip(),
        "url": str(url or "").strip(),
    }


def _fingerprint(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _idempotency_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _error_details(error: Exception) -> tuple[str, str]:
    if isinstance(error, AppError):
        return str(error.code), error.message
    message = str(error).strip() or error.__class__.__name__
    return error.__class__.__name__, message[:2000]


def _task_to_dict(row: ProjectImportTask) -> dict[str, Any]:
    error = None
    if row.error_message:
        error = {"code": str(row.error_code or "import_failed"), "message": row.error_message}
    result = _load_json(row.result_json)
    return {
        "id": int(row.id),
        "task_id": row.public_id,
        "status": row.status,
        "attempt_count": int(row.attempt_count or 0),
        "max_attempts": int(row.max_attempts or 0),
        "project_id": int(row.project_id) if row.project_id is not None else None,
        "result": result,
        "error": error,
        "next_attempt_at": row.next_attempt_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "create_time": row.create_time,
        "update_time": row.update_time,
        "lease_token": row.lease_token,
    }


def public_task_dict(row: ProjectImportTask) -> dict[str, Any]:
    """返回不暴露内部主键和租约令牌的任务状态。"""

    data = _task_to_dict(row)
    data.pop("id", None)
    data.pop("lease_token", None)
    return data


def create_import_task(
    db: Session,
    user: User,
    *,
    url: str,
    project_name: str,
    description: str = "",
    language: str | None = None,
    audit_mode: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """创建任务；相同用户的显式幂等键只允许绑定同一请求。"""

    payload = _request_payload(
        url=url,
        project_name=project_name,
        description=description,
        language=language,
        audit_mode=audit_mode,
    )
    # 复用创建项目的字段约束，但不在此阶段产生项目副作用。
    ProjectIn(
        project_name=payload["project_name"],
        description=payload["description"] or None,
        language=payload["language"],
    )
    validate_remote_project_url(payload["url"])
    payload_json = _canonical_json(payload)
    request_fingerprint = _fingerprint(payload_json)
    raw_idempotency = str(idempotency_key or "").strip()
    if len(raw_idempotency) > 512:
        raise ConflictError("幂等键长度不能超过 512 个字符", code=40902)
    # 未提供键时保持旧调用的“每次创建一次”语义；API/Agent 可显式传键获得幂等。
    key_hash = _idempotency_hash(raw_idempotency or uuid.uuid4().hex)

    existing = (
        db.query(ProjectImportTask)
        .filter(
            ProjectImportTask.user_id == user.id,
            ProjectImportTask.idempotency_key_hash == key_hash,
        )
        .first()
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise ConflictError("幂等键已用于另一组远程导入参数", code=40902)
        return public_task_dict(existing)

    duplicate_project = (
        db.query(Project.id)
        .filter(
            Project.user_id == user.id,
            Project.project_name == payload["project_name"],
            Project.status != "deleted",
        )
        .first()
    )
    if duplicate_project is not None:
        raise ConflictError("项目名重复", code=40901)

    row = ProjectImportTask(
        public_id=uuid.uuid4().hex,
        user_id=user.id,
        idempotency_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
        request_json=payload_json,
        status=QUEUED,
        attempt_count=0,
        max_attempts=int(settings.project_import_max_attempts),
        next_attempt_at=_utcnow(),
        result_json="{}",
    )
    try:
        db.add(row)
        db.flush()
        audit_service.log(
            db,
            user,
            "project_remote_import_queued",
            target_type="project_import_task",
            target_id=row.public_id,
            detail=f"project={payload['project_name']}; audit_mode={payload['audit_mode']}",
            commit=False,
        )
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(ProjectImportTask)
            .filter(
                ProjectImportTask.user_id == user.id,
                ProjectImportTask.idempotency_key_hash == key_hash,
            )
            .first()
        )
        if existing is None:
            raise
        if existing.request_fingerprint != request_fingerprint:
            raise ConflictError("幂等键已用于另一组远程导入参数", code=40902)
        return public_task_dict(existing)
    return public_task_dict(row)


def get_import_task(db: Session, user: User, task_id: str) -> dict[str, Any]:
    """仅任务创建者可读取导入状态和失败原因。"""

    row = (
        db.query(ProjectImportTask)
        .filter(
            ProjectImportTask.public_id == str(task_id),
            ProjectImportTask.user_id == user.id,
        )
        .first()
    )
    if row is None:
        raise NotFoundError("远程导入任务不存在", code=40400)
    return public_task_dict(row)


def claim_next_task(
    db: Session,
    *,
    lease_seconds: int | None = None,
) -> dict[str, Any] | None:
    """通过条件更新领取一个到期任务，支持多个调度进程竞争。"""

    now = _utcnow()
    lease_for = max(30, int(lease_seconds or settings.project_import_lease_seconds))
    candidates = (
        db.query(ProjectImportTask.id)
        .filter(
            ProjectImportTask.status == QUEUED,
            or_(
                ProjectImportTask.next_attempt_at.is_(None),
                ProjectImportTask.next_attempt_at <= now,
            ),
        )
        .order_by(ProjectImportTask.next_attempt_at.asc(), ProjectImportTask.id.asc())
        .limit(20)
        .all()
    )
    for (task_db_id,) in candidates:
        lease_token = uuid.uuid4().hex
        updated = db.execute(
            update(ProjectImportTask)
            .where(
                ProjectImportTask.id == task_db_id,
                ProjectImportTask.status == QUEUED,
                or_(
                    ProjectImportTask.next_attempt_at.is_(None),
                    ProjectImportTask.next_attempt_at <= now,
                ),
            )
            .values(
                status=RUNNING,
                attempt_count=ProjectImportTask.attempt_count + 1,
                lease_token=lease_token,
                lease_expires_at=now + timedelta(seconds=lease_for),
                next_attempt_at=None,
                started_at=now,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            db.rollback()
            continue
        db.commit()
        db.expire_all()
        row = db.get(ProjectImportTask, task_db_id)
        return _task_to_dict(row)
    db.rollback()
    return None


def touch_import_task(
    db: Session,
    task_db_id: int,
    *,
    lease_token: str,
    lease_seconds: int | None = None,
    progress: Mapping[str, Any] | None = None,
) -> bool:
    """刷新租约并可选持久化当前下载/入库阶段。"""

    row = (
        db.query(ProjectImportTask)
        .filter(
            ProjectImportTask.id == task_db_id,
            ProjectImportTask.status == RUNNING,
            ProjectImportTask.lease_token == lease_token,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        db.rollback()
        return False
    row.lease_expires_at = _utcnow() + timedelta(
        seconds=max(30, int(lease_seconds or settings.project_import_lease_seconds))
    )
    if progress is not None:
        _merge_progress(row, progress)
    db.commit()
    return True


def recover_expired_leases(db: Session, *, now: datetime | None = None) -> int:
    """把进程退出后遗留的过期运行任务恢复为排队或终态。"""

    current = now or _utcnow()
    rows = (
        db.query(ProjectImportTask)
        .filter(
            ProjectImportTask.status == RUNNING,
            ProjectImportTask.lease_expires_at.is_not(None),
            ProjectImportTask.lease_expires_at < current,
        )
        .order_by(ProjectImportTask.id.asc())
        .all()
    )
    recovered = 0
    for row in rows:
        terminal = int(row.attempt_count or 0) >= int(row.max_attempts or 1)
        values: dict[Any, Any] = {
            ProjectImportTask.status: FAILED if terminal else QUEUED,
            ProjectImportTask.lease_token: None,
            ProjectImportTask.lease_expires_at: None,
            ProjectImportTask.next_attempt_at: None if terminal else current,
            ProjectImportTask.error_code: "lease_expired",
            ProjectImportTask.error_message: (
                "远程导入因服务重启或执行超时而中断，且已达到最大重试次数"
                if terminal
                else "上次远程导入因服务重启或执行超时而中断，系统已自动重试"
            ),
            ProjectImportTask.completed_at: current if terminal else None,
        }
        updated = (
            db.query(ProjectImportTask)
            .filter(
                ProjectImportTask.id == row.id,
                ProjectImportTask.status == RUNNING,
                ProjectImportTask.lease_token == row.lease_token,
                ProjectImportTask.lease_expires_at < current,
            )
            .update(values, synchronize_session=False)
        )
        if updated != 1:
            continue
        _cleanup_partial_import_project(db, row)
        recovered += 1
    if recovered:
        db.commit()
    else:
        db.rollback()
    return recovered


def _ensure_import_project(
    db: Session,
    row: ProjectImportTask,
    user: User,
    payload: Mapping[str, Any],
) -> Project:
    if row.project_id is not None:
        project = db.get(Project, int(row.project_id))
        if project is None or project.status == "deleted":
            raise ConflictError("远程导入关联的项目已被删除", code=40901)
        if project.status == "import_failed":
            project.status = "importing"
            db.commit()
        return project

    project = project_service.create_project(
        db,
        user,
        ProjectIn(
            project_name=str(payload["project_name"]),
            description=str(payload.get("description") or "") or None,
            language=str(payload["language"]) if payload.get("language") else None,
        ),
        initial_status="importing",
        commit=False,
    )
    row.project_id = project.id
    _merge_progress(row, {"phase": "project_created"})
    db.commit()
    db.refresh(project)
    return project


def _cleanup_partial_import_project(db: Session, row: ProjectImportTask) -> bool:
    """清理当前任务创建且尚未激活的整棵半成品项目数据。"""

    if row.project_id is None:
        return False
    project_id = int(row.project_id)
    project = db.get(Project, project_id)
    if project is None:
        row.project_id = None
        return True
    payload = _load_json(row.request_json)
    expected_name = str(payload.get("project_name") or "").strip()
    task_owns_project = (
        project.status in {"importing", "import_failed"}
        and int(project.user_id) == int(row.user_id)
        and bool(expected_name)
        and project.project_name == expected_name
    )
    if not task_owns_project:
        return False

    file_ids = [
        int(file_id)
        for (file_id,) in db.query(CodeFile.id).filter(CodeFile.project_id == project_id).all()
    ]
    if file_ids:
        db.query(CodeVersion).filter(CodeVersion.file_id.in_(file_ids)).delete(
            synchronize_session=False,
        )
    db.query(CodeFile).filter(CodeFile.project_id == project_id).delete(
        synchronize_session=False,
    )
    db.query(ProjectSourceArchive).filter(
        ProjectSourceArchive.project_id == project_id
    ).delete(synchronize_session=False)
    db.query(ProjectSourceRevision).filter(
        ProjectSourceRevision.project_id == project_id
    ).delete(synchronize_session=False)
    db.query(ProjectMember).filter(ProjectMember.project_id == project_id).delete(
        synchronize_session=False,
    )
    db.delete(project)
    row.project_id = None
    db.flush()
    return True


def _existing_result(
    db: Session,
    project: Project,
    *,
    source_url: str,
    audit_mode: bool,
) -> dict[str, Any] | None:
    if audit_mode:
        archive = (
            db.query(ProjectSourceArchive)
            .filter(
                ProjectSourceArchive.project_id == project.id,
                ProjectSourceArchive.storage_status == "active",
            )
            .first()
        )
        if archive is None:
            return None
        return {
            "id": project.id,
            "file_count": int(archive.file_count),
            "first_file_id": None,
            "source_url": source_url,
            "source_mode": "audit_archive",
            "source_archive": project_source_service.source_archive_to_dict(archive),
        }
    rows = (
        db.query(CodeFile.id)
        .filter(CodeFile.project_id == project.id, CodeFile.status == "active")
        .order_by(CodeFile.id.asc())
        .all()
    )
    if not rows:
        return None
    return {
        "id": project.id,
        "file_count": len(rows),
        "first_file_id": int(rows[0][0]),
        "source_url": source_url,
        "source_mode": "files",
    }


def execute_claimed_import(
    db: Session,
    task_db_id: int,
    *,
    lease_token: str,
) -> dict[str, Any]:
    """执行已领取任务；已完成的项目内容会被识别并直接收敛。"""

    row = db.get(ProjectImportTask, int(task_db_id))
    if row is None or row.status != RUNNING or row.lease_token != lease_token:
        raise ConflictError("远程导入任务租约已失效", code=40902)
    user = db.get(User, int(row.user_id))
    if user is None or not int(user.status or 0):
        raise ConflictError("远程导入任务所属账号不存在或已停用", code=40902)
    payload = _load_json(row.request_json)
    project: Project | None = None
    if row.project_id is not None:
        project = _ensure_import_project(db, row, user, payload)
        existing = _existing_result(
            db,
            project,
            source_url=str(payload["url"]),
            audit_mode=bool(payload.get("audit_mode")),
        )
        if existing is not None:
            return existing

    last_heartbeat = 0.0

    def progress(received: int, total: int | None) -> None:
        nonlocal last_heartbeat
        now_monotonic = time.monotonic()
        if received != total and now_monotonic - last_heartbeat < 10:
            return
        if not touch_import_task(
            db,
            row.id,
            lease_token=lease_token,
            progress={
                "phase": "downloading",
                "received_bytes": received,
                "total_bytes": total,
            },
        ):
            raise ConflictError("远程导入任务租约已失效", code=40902)
        last_heartbeat = now_monotonic

    with project_source_service.download_remote_project_archive_to_temp(
        str(payload["url"]),
        progress_callback=progress,
    ) as downloaded:
        if not touch_import_task(
            db,
            row.id,
            lease_token=lease_token,
            progress={
                "phase": "ingesting",
                "received_bytes": downloaded.byte_size,
                "total_bytes": downloaded.byte_size,
                "sha256": downloaded.sha256,
            },
        ):
            raise ConflictError("远程导入任务租约已失效", code=40902)
        raw = downloaded.path.read_bytes()
        # 当前归档解析器接收 bytes；在创建任何项目记录前完成完整路径、文件数和
        # 解压倍率基础校验。下载本身已流式落盘，峰值仍受 MAX_REMOTE_BYTES 约束。
        if bool(payload.get("audit_mode")):
            project_source_service._strict_zip_members(raw, downloaded.filename)
        else:
            project_source_service.read_archive_members(raw, downloaded.filename)
        if project is None:
            project = _ensure_import_project(db, row, user, payload)
        existing = _existing_result(
            db,
            project,
            source_url=str(payload["url"]),
            audit_mode=bool(payload.get("audit_mode")),
        )
        if existing is not None:
            return existing
        if bool(payload.get("audit_mode")):
            archive_data = project_source_service.ingest_source_archive_bytes(
                db,
                user,
                project.id,
                raw=raw,
                filename=downloaded.filename,
            )
            return {
                "id": project.id,
                "file_count": archive_data["file_count"],
                "first_file_id": None,
                "source_url": payload["url"],
                "source_mode": "audit_archive",
                "source_archive": archive_data,
            }
        first_id, _, _ = code_file_service._upload_archive(
            db,
            user,
            project.id,
            raw,
            downloaded.filename,
        )
        count = (
            db.query(CodeFile.id)
            .filter(CodeFile.project_id == project.id, CodeFile.status == "active")
            .count()
        )
        return {
            "id": project.id,
            "file_count": count,
            "first_file_id": first_id,
            "source_url": payload["url"],
            "source_mode": "files",
        }


def complete_import_task(
    db: Session,
    task_db_id: int,
    *,
    lease_token: str,
    result: Mapping[str, Any],
) -> bool:
    """以租约令牌原子提交成功终态，并开放项目列表可见性。"""

    row = db.get(ProjectImportTask, int(task_db_id))
    if row is None or row.status != RUNNING or row.lease_token != lease_token:
        db.rollback()
        return False
    now = _utcnow()
    if row.project_id is not None:
        db.query(Project).filter(
            Project.id == row.project_id,
            Project.status.in_(("importing", "import_failed")),
        ).update({Project.status: "active"}, synchronize_session=False)
    row.status = SUCCEEDED
    row.result_json = _canonical_json(dict(result))
    row.error_code = None
    row.error_message = None
    row.lease_token = None
    row.lease_expires_at = None
    row.next_attempt_at = None
    row.completed_at = now
    audit_service.log(
        db,
        db.get(User, int(row.user_id)),
        "project_remote_import_succeeded",
        target_type="project_import_task",
        target_id=row.public_id,
        detail=f"project={row.project_id}",
        commit=False,
    )
    db.commit()
    return True


def fail_import_task(
    db: Session,
    task_db_id: int,
    *,
    lease_token: str,
    error: Exception,
    retryable: bool,
) -> bool:
    """记录可操作失败原因；可重试错误按指数退避重新入队。"""

    row = db.get(ProjectImportTask, int(task_db_id))
    if row is None or row.status != RUNNING or row.lease_token != lease_token:
        db.rollback()
        return False
    code, message = _error_details(error)
    now = _utcnow()
    should_retry = bool(retryable) and int(row.attempt_count or 0) < int(row.max_attempts or 1)
    row.status = QUEUED if should_retry else FAILED
    row.next_attempt_at = (
        now + timedelta(seconds=min(300, 5 * (2 ** max(0, int(row.attempt_count or 1) - 1))))
        if should_retry
        else None
    )
    row.lease_token = None
    row.lease_expires_at = None
    row.error_code = code[:80]
    row.error_message = message[:2000]
    row.completed_at = None if should_retry else now
    _cleanup_partial_import_project(db, row)
    audit_service.log(
        db,
        db.get(User, int(row.user_id)),
        "project_remote_import_retry" if should_retry else "project_remote_import_failed",
        target_type="project_import_task",
        target_id=row.public_id,
        detail=f"code={code}; reason={message}",
        status="failed",
        commit=False,
    )
    db.commit()
    return True


def is_retryable_error(error: Exception) -> bool:
    """只重试网络/外部服务和本机临时 I/O 故障。"""

    if isinstance(error, project_source_service.RemoteArchiveNotFoundError):
        return False
    return isinstance(error, (ExternalServiceError, httpx.HTTPError, OSError))
