"""项目源码修复副本:保存/读取/清理。

语法修复 Agent 修复后的源码 zip 作为项目副本持久化,下次审计可选用。
副本只保存"修复后的源码",剥离沙箱内部注入物(_prism_launch.sh 等),
原始项目归档始终不变。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.project_source_revision import ProjectSourceRevision
from app.services.project_member_service import require_project_access

_INTERNAL_PREFIXES = (
    "_prism_launch.sh",
    "_prism_verify.sh",
    "_prism_poc.sh",
    "_agent_tests/",
    "_prism/",
)


def _strip_internal_members(raw: bytes) -> bytes:
    """移除沙箱内部注入物,只保留修复后的项目源码。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            name = info.filename
            if name.startswith(_INTERNAL_PREFIXES):
                continue
            zout.writestr(info, zin.read(name))
    return buf.getvalue()


def list_revisions(db: Session, actor: Any, project_id: int) -> list[dict[str, Any]]:
    """返回项目的源码修复副本摘要(不含 blob)。"""
    require_project_access(db, project_id, actor, need_write=False)
    rows = (
        db.query(ProjectSourceRevision)
        .filter(ProjectSourceRevision.project_id == project_id)
        .order_by(ProjectSourceRevision.revision_no.desc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "id": row.id,
            "revision_no": row.revision_no,
            "source_sha256": row.source_sha256,
            "parent_sha256": row.parent_sha256,
            "repaired_files": json.loads(row.repaired_files_json or "[]"),
            "repair_notes": row.repair_notes,
            "create_time": row.create_time.isoformat() if row.create_time else None,
        })
    return out


def get_revision_archive(
    db: Session,
    actor: Any,
    revision_id: int,
    project_id: int,
) -> bytes:
    """校验副本归属并返回 zip 字节。"""
    row = db.get(ProjectSourceRevision, revision_id)
    if row is None or row.project_id != project_id:
        raise NotFoundError("源码副本不存在", code=40400)
    require_project_access(db, project_id, actor, need_write=False)
    return bytes(row.archive_blob)


def save_revision(
    db: Session,
    *,
    project_id: int,
    owner_id: int,
    repaired_source_base64: str,
    repaired_files: list[str],
    parent_sha256: Optional[str],
    repair_notes: str = "",
) -> Optional[ProjectSourceRevision]:
    """保存修复后源码为项目副本;sha 与最近副本相同则跳过。"""
    raw = base64.b64decode(repaired_source_base64)
    cleaned = _strip_internal_members(raw)
    sha = hashlib.sha256(cleaned).hexdigest()
    latest = (
        db.query(ProjectSourceRevision)
        .filter(ProjectSourceRevision.project_id == project_id)
        .order_by(ProjectSourceRevision.revision_no.desc())
        .first()
    )
    if latest is not None and latest.source_sha256 == sha:
        return None
    revision_no = (latest.revision_no if latest else 0) + 1
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = ProjectSourceRevision(
        project_id=project_id,
        owner_id=owner_id,
        revision_no=revision_no,
        source_sha256=sha,
        parent_sha256=parent_sha256,
        repaired_files_json=json.dumps(repaired_files, ensure_ascii=False),
        repair_notes=repair_notes[:500],
        archive_blob=cleaned,
        create_time=now,
        update_time=now,
    )
    db.add(row)
    db.flush()
    return row


def delete_revision(
    db: Session,
    actor: Any,
    project_id: int,
    revision_id: int,
) -> None:
    """删除项目的一个源码修复副本(原始归档不受影响)。

    仅项目 owner/admin 可删除;副本被沙箱引用不影响(创建时已复制 zip)。
    """
    row = db.get(ProjectSourceRevision, revision_id)
    if row is None or row.project_id != project_id:
        raise NotFoundError("源码副本不存在", code=40400)
    require_project_access(db, project_id, actor, need_write=True)
    db.delete(row)
    db.flush()


def revision_to_dict(row: ProjectSourceRevision) -> dict[str, Any]:
    return {
        "id": row.id,
        "revision_no": row.revision_no,
        "source_sha256": row.source_sha256,
        "parent_sha256": row.parent_sha256,
        "repaired_files": json.loads(row.repaired_files_json or "[]"),
        "repair_notes": row.repair_notes,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    }
