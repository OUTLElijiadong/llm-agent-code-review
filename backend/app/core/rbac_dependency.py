"""
RBAC FastAPI 依赖注入

提供基于 RBAC 权限体系的 FastAPI 路由依赖:
1. require_permission: 工厂函数,按权限编码校验,返回当前用户对象
2. require_admin: 要求管理员角色(admin/super_admin)
3. get_current_user_data_scope: 返回当前用户数据范围
4. require_data_scope_access: 校验数据范围(能否访问目标用户数据)

用法示例:
    from app.core.rbac_dependency import require_permission
    from app.core.permission_codes import PermissionCode

    @router.post("/projects", dependencies=[Depends(require_permission(PermissionCode.PROJECT_CREATE))])
    async def create_project(...):
        ...
"""
from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import PermissionError
from app.models.rbac import DataScope
from app.models.user import User
from app.services.rbac_service import (
    check_data_scope,
    check_permission,
    get_user_data_scope,
    is_admin_user,
)


def require_permission(permission_code: str):
    """权限校验依赖工厂函数

    生成一个 FastAPI 依赖,校验当前用户是否拥有指定权限编码。
    校验通过返回当前用户对象(便于路由直接使用),否则抛出 PermissionError(403)。

    Args:
        permission_code: 权限编码字符串(如 "project:create")

    Returns:
        Callable: FastAPI 依赖函数,签名为 (user, db) -> User

    Usage:
        @router.get("/projects", dependencies=[Depends(require_permission("project:view"))])
        def list_projects(...): ...

        # 或在路由函数中获取当前用户:
        @router.get("/projects")
        def list_projects(user: User = Depends(require_permission("project:view"))): ...
    """

    def _dependency(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        """权限校验闭包依赖

        Args:
            user: 当前登录用户(由 get_current_user 注入)
            db: 数据库会话

        Returns:
            User: 校验通过后返回当前用户对象

        Raises:
            PermissionError: 用户无指定权限(403)
        """
        if not check_permission(db, user.id, permission_code):
            raise PermissionError(
                f"无操作权限: 需要 {permission_code}",
                detail={"required_permission": permission_code},
            )
        return user

    return _dependency


def require_admin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """管理员角色校验依赖

    要求当前用户拥有 admin 或 super_admin 角色(新版 RBAC 或旧版 User.role 字段)。
    校验通过返回当前用户对象,否则抛出 PermissionError(403)。

    与 require_permission 的区别:
    - require_permission 基于具体权限编码校验(可粒度到单个操作)
    - require_admin 基于角色身份校验(粗粒度,仅区分管理员与普通用户)

    Args:
        user: 当前登录用户(由 get_current_user 注入)
        db: 数据库会话

    Returns:
        User: 校验通过后返回当前用户对象

    Raises:
        PermissionError: 非管理员用户(403)
    """
    if not is_admin_user(db, user.id):
        raise PermissionError(
            "需要管理员权限",
            detail={"required_role": "admin"},
        )
    return user


def get_current_user_data_scope(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DataScope:
    """获取当前用户数据范围依赖

    返回当前用户最高优先级的数据范围对象(scope_type + project_ids)。
    路由可据此进行更细粒度的数据过滤。

    Args:
        user: 当前登录用户(由 get_current_user 注入)
        db: 数据库会话

    Returns:
        DataScope: 当前用户的数据范围对象
            - scope_type: all/project_own/project_member/custom
            - project_ids: 自定义项目ID列表(custom 类型时使用)
    """
    return get_user_data_scope(db, user.id)


def require_data_scope_access(target_user_id_field: str = "user_id"):
    """数据范围校验依赖工厂函数

    生成一个 FastAPI 依赖,校验当前用户能否访问目标用户的数据。
    目标用户 ID 从请求路径参数或查询参数中按字段名提取。

    校验逻辑:
    1. 管理员:始终通过
    2. scope_type=all:始终通过
    3. scope_type=project_own:目标用户 ID 必须等于当前用户 ID
    4. scope_type=project_member/custom:后续实现,目前通过

    Args:
        target_user_id_field: 目标用户 ID 的参数字段名,默认 "user_id"
            优先从路径参数取,其次从查询参数取

    Returns:
        Callable: FastAPI 依赖函数,签名为 (request, user, db) -> User

    Usage:
        @router.get("/users/{user_id}/profile",
                    dependencies=[Depends(require_data_scope_access("user_id"))])
        def get_user_profile(...): ...
    """

    def _dependency(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        """数据范围校验闭包依赖

        Args:
            request: FastAPI 请求对象(用于提取路径/查询参数)
            user: 当前登录用户
            db: 数据库会话

        Returns:
            User: 校验通过后返回当前用户对象

        Raises:
            PermissionError: 数据范围超出权限(403)
        """
        # 优先从路径参数取目标用户 ID,其次从查询参数取
        raw_target = request.path_params.get(target_user_id_field)
        if raw_target is None:
            raw_target = request.query_params.get(target_user_id_field)

        # 未提供目标用户 ID 时不做数据范围校验(交由路由自行处理)
        if raw_target is None:
            return user

        try:
            target_user_id = int(raw_target)
        except (TypeError, ValueError):
            # 参数非整数,跳过校验(交由路由参数解析阶段报 422)
            return user

        if not check_data_scope(db, user.id, target_user_id):
            raise PermissionError(
                "数据范围超出权限",
                detail={
                    "current_user_id": user.id,
                    "target_user_id": target_user_id,
                },
            )
        return user

    return _dependency
