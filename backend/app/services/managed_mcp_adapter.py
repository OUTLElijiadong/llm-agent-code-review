"""Prism 内部 managed MCP 能力适配器。

受管工具不经过网络，也不接受模型传入的用户身份。每次调用都使用
Responses 请求注入的当前 ``User``，再复用项目源码与沙箱服务的权限边界。
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, PermissionError, ValidationError
from app.core.permission_codes import PermissionCode
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import project_source_service, sandbox_service
from app.services.project_member_service import require_project_access
from app.services.rbac_service import check_permission

LIVE_MANAGED_KINDS = frozenset({"prism-code", "prism-sandbox", "playwright"})
_MANAGED_TOOL_PERMISSIONS = {
    ("prism-code", "list_project_source"): (
        PermissionCode.PROJECT_VIEW,
        PermissionCode.FILE_VIEW,
    ),
    ("prism-code", "download_project_source"): (
        PermissionCode.PROJECT_VIEW,
        PermissionCode.FILE_DOWNLOAD,
    ),
}


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ProjectArguments(_StrictArguments):
    project_id: int = Field(gt=0)


class _ProjectListArguments(_ProjectArguments):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=200)


class _CreateTestArguments(_ProjectArguments):
    language: Literal["python", "node", "java", "go", "php"]
    test_mode: Literal["whitebox", "blackbox", "combined"]


class _CreateDeploymentArguments(_ProjectArguments):
    language: Literal["python", "node", "java", "go", "php"]
    ttl_hours: int = Field(default=72, ge=1, le=168)


class _SandboxArguments(_StrictArguments):
    public_id: str = Field(min_length=1, max_length=40, pattern=r"^sbx_[A-Za-z0-9_-]+$")


class _ExtendArguments(_SandboxArguments):
    hours: int = Field(ge=1, le=168)


class _BrowserBlackboxArguments(_StrictArguments):
    sandbox_id: str = Field(min_length=1, max_length=40, pattern=r"^sbx_[A-Za-z0-9_-]+$")
    target_url: AnyHttpUrl


def is_live_managed_kind(value: str | None) -> bool:
    """判断 managed_kind 是否已经有本地真实执行器。"""

    return str(value or "") in LIVE_MANAGED_KINDS


def managed_tool_permissions(managed_kind: str, tool_name: str) -> tuple[str, ...]:
    """返回受管 MCP 工具发现与执行所需的全部权限。"""
    return _MANAGED_TOOL_PERMISSIONS.get((managed_kind, tool_name), ())


def managed_kind_ready(db: Session, managed_kind: str) -> bool:
    if managed_kind in {"prism-code", "prism-sandbox"}:
        return True
    if managed_kind == "playwright":
        return sandbox_service.browser_worker_ready(db)
    return False


def _require_permissions(db: Session, user: User, *permissions: str) -> None:
    for permission in permissions:
        if not check_permission(db, user.id, permission):
            raise PermissionError(f"无操作权限: 需要 {permission}", code=40300)


def _project_source_list(
    db: Session,
    user: User,
    project_id: int,
    *,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    # 统一使用项目成员访问规则；不能复用只支持 owner/admin 的旧文件列表查询。
    _require_permissions(
        db,
        user,
        PermissionCode.PROJECT_VIEW,
        PermissionCode.FILE_VIEW,
    )
    require_project_access(db, project_id, user, need_write=False)
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    archive = project_source_service.get_source_archive_metadata(db, user, project_id)
    if archive is not None:
        return {
            "project_id": project_id,
            "project_name": str(project.project_name or ""),
            "source_mode": "audit_archive",
            "total": int(archive["file_count"]),
            "page": page,
            "page_size": page_size,
            "pages": 1,
            "files": [],
            "source_archive": archive,
        }
    query = db.query(CodeFile).filter(
        CodeFile.project_id == project_id,
        CodeFile.status == "active",
    )
    total = query.count()
    rows = (
        query
        .order_by(CodeFile.file_path.asc(), CodeFile.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "project_id": project_id,
        "project_name": str(project.project_name or ""),
        "source_mode": "files",
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
        "files": [
            {
                "id": int(row.id),
                "file_name": str(row.file_name or ""),
                "file_path": str(row.file_path or row.file_name or ""),
                "language": str(row.language or ""),
                "size_bytes": int(row.size_bytes or 0),
                "raw_size": int(row.raw_size or row.size_bytes or 0),
                "line_count": int(row.line_count or 0),
                "is_binary": bool(row.is_binary),
            }
            for row in rows
        ],
    }


def _project_source_download(db: Session, user: User, project_id: int) -> dict[str, Any]:
    # 只做权限与存在性预检；实际 ZIP 由 HTTP 路由按同一用户再次鉴权并生成。
    _require_permissions(
        db,
        user,
        PermissionCode.PROJECT_VIEW,
        PermissionCode.FILE_DOWNLOAD,
    )
    require_project_access(db, project_id, user, need_write=False)
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    archive = project_source_service.get_source_archive_metadata(db, user, project_id)
    path = f"/api/projects/{project_id}/source-archive"
    result = {
        "project_id": project_id,
        "project_name": str(project.project_name or ""),
        "download_path": path,
        "download_url": path,
        "authentication": "current_user",
        "source_mode": "audit_archive" if archive else "files",
    }
    if archive:
        result.update({
            "archive_sha256": archive["archive_sha256"],
            "malware_status": archive["malware_status"],
            "audit_status": archive["audit_status"],
            "file_count": archive["file_count"],
        })
    return result


def call_managed_tool(
    db: Session,
    user: User,
    managed_kind: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> Any:
    """执行一个内部 managed 工具。

    ``sandbox_service`` 的生命周期函数会再次校验项目可见性、所有者与超级
    管理员边界；adapter 不允许参数覆盖这些身份信息。
    """

    if not is_live_managed_kind(managed_kind) or not managed_kind_ready(db, managed_kind):
        raise ValidationError("该受管 MCP 执行器尚未就绪", code=50301)
    if not isinstance(arguments, Mapping):
        raise ValidationError("MCP 工具参数必须是对象", code=40001)

    if managed_kind == "prism-code":
        if tool_name == "list_project_source":
            parsed = _ProjectListArguments.model_validate(dict(arguments))
            return _project_source_list(
                db,
                user,
                parsed.project_id,
                page=parsed.page,
                page_size=parsed.page_size,
            )
        if tool_name == "download_project_source":
            parsed = _ProjectArguments.model_validate(dict(arguments))
            return _project_source_download(db, user, parsed.project_id)
        raise ValidationError(f"不支持的 prism-code 工具: {tool_name}", code=40001)

    if managed_kind == "playwright":
        if tool_name != "browser_blackbox":
            raise ValidationError(f"不支持的 playwright 工具: {tool_name}", code=40001)
        parsed = _BrowserBlackboxArguments.model_validate(dict(arguments))
        return sandbox_service.run_browser_blackbox(
            db,
            user,
            parsed.sandbox_id,
            str(parsed.target_url),
        )

    if tool_name == "create_test":
        parsed = _CreateTestArguments.model_validate(dict(arguments))
        row = sandbox_service.create_environment(
            db,
            user,
            {
                "project_id": parsed.project_id,
                "purpose": "test",
                "language": parsed.language,
                "test_mode": parsed.test_mode,
            },
        )
        return sandbox_service.environment_to_dict(db, row)
    if tool_name == "create_deployment":
        parsed = _CreateDeploymentArguments.model_validate(dict(arguments))
        row = sandbox_service.create_environment(
            db,
            user,
            {
                "project_id": parsed.project_id,
                "purpose": "deploy",
                "language": parsed.language,
                "test_mode": "deploy",
                "ttl_hours": parsed.ttl_hours,
            },
        )
        return sandbox_service.environment_to_dict(db, row)
    if tool_name == "close":
        parsed = _SandboxArguments.model_validate(dict(arguments))
        return sandbox_service.stop_environment(db, user, parsed.public_id)
    if tool_name == "extend":
        parsed = _ExtendArguments.model_validate(dict(arguments))
        return sandbox_service.extend_environment(db, user, parsed.public_id, parsed.hours)
    raise ValidationError(f"不支持的 prism-sandbox 工具: {tool_name}", code=40001)
