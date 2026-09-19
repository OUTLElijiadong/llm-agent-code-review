"""
RBAC 业务服务层

提供角色/权限/菜单/数据范围管理的核心业务逻辑,以及用户权限校验能力。
所有函数均为纯函数式(接收 db Session),便于在路由与命令行中复用。

设计要点:
1. 每个账号只有一个固定有效角色，旧字段与 RBAC 绑定冲突时失败关闭
2. admin/super_admin 仅在身份一致时按各自边界执行权限绕过
3. 数据范围绑定唯一有效角色，缺失或冲突时回落到仅本人
4. 角色分配为覆盖式，并同步旧版 ``User.role`` 兼容字段
"""

from __future__ import annotations

from typing import List, Optional, Set

from loguru import logger
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.core.super_admin import (
    SERVER_OPS_PERMISSION_PREFIX,
    SUPER_ADMIN_ROLE,
    SUPER_ADMIN_USERNAME,
    is_unique_super_admin,
)
from app.models.rbac import (
    DataScope,
    Menu,
    Permission,
    Role,
    RolePermission,
    UserRole,
)
from app.models.user import User
from app.schemas.rbac import DataScopeIn, RoleCreateIn, RoleUpdateIn

# 数据范围优先级映射,数值越大优先级越高
# all(全部) > project_member(参与项目) > custom(自定义) > project_own(仅自己)
_DATA_SCOPE_PRIORITY: dict[str, int] = {
    "project_own": 1,
    "custom": 2,
    "project_member": 3,
    "all": 4,
}

# 默认数据范围(用户无任何角色数据范围时使用,最严格)
_DEFAULT_SCOPE_TYPE = "project_own"

# 享受权限绕过的角色编码集合
_ADMIN_ROLE_CODES: Set[str] = {"admin", "super_admin"}

# 系统只允许为普通账号分配一个基础角色；超级管理员由唯一账号约束维护。
ASSIGNABLE_ROLE_CODES: Set[str] = {"user", "reviewer", "admin"}
_EFFECTIVE_ROLE_CODES: Set[str] = {*ASSIGNABLE_ROLE_CODES, "super_admin"}


# ============================================================================
# 内部辅助函数
# ============================================================================


def _require_configurable_role(db: Session, role_id: int) -> Role:
    """只允许管理三个可分配的固定角色。

    历史 auditor/自定义角色可保留供审计，但不得通过旧 API 重新启用、
    获得权限或扩大数据范围。
    """
    role = db.get(Role, role_id)
    if role is None:
        raise NotFoundError("角色不存在", code=40400)
    if role.code not in ASSIGNABLE_ROLE_CODES:
        raise ForbiddenError("仅允许管理普通用户、评审员和管理员三个固定角色", code=40324)
    return role


def resolve_effective_role_code(db: Session, user_id: int) -> str:
    """解析账号唯一有效角色；旧字段与 RBAC 冲突时失败关闭。

    迁移期仍允许“仅有旧字段、尚无任何 RBAC 绑定”的四个固定角色。一旦账号
    存在有效 RBAC 绑定，就必须恰好只有一个角色且与 ``User.role`` 一致。

    Args:
        db: 数据库会话
        user_id: 用户ID

    Returns:
        str: 唯一有效角色编码；账号禁用、未知角色或数据漂移时返回空字符串。
    """
    # 权限判定不得在缺失真实数据会话时回退信任请求对象中的旧 role。
    # 测试替身、已关闭会话或被污染的会话均必须稳定失败关闭。
    if not callable(getattr(db, "get", None)) or not callable(getattr(db, "query", None)):
        return ""
    user = db.get(User, user_id)
    # 某些服务会使用代理 Session；若 ORM 身份映射被污染或返回了
    # 其他实体，必须失败关闭，而不是继续解析其同名属性。
    if not isinstance(user, User):
        return ""
    try:
        is_active = int(user.status or 0) == 1
    except (TypeError, ValueError):
        return ""
    if not is_active:
        return ""
    rows = (
        db.query(Role.code, Role.status)
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .filter(UserRole.user_id == user_id)
        .all()
    )
    active_role_codes = {str(code) for code, status in rows if str(status) == "active"}
    legacy_role = str(user.role or "")
    if not rows:
        return legacy_role if legacy_role in _EFFECTIVE_ROLE_CODES else ""
    if active_role_codes == {legacy_role} and legacy_role in _EFFECTIVE_ROLE_CODES:
        return legacy_role
    logger.warning(
        "RBAC 角色身份漂移或失效，已拒绝授权 user_id={} legacy_role={} rbac_roles={}",
        user_id,
        legacy_role,
        sorted((str(code), str(status)) for code, status in rows),
    )
    return ""


def has_effective_role(db: Session, user_id: int, *role_codes: str) -> bool:
    """集中角色判定入口，禁止业务服务自行读取 ``User.role`` 授权。"""

    return resolve_effective_role_code(db, user_id) in set(role_codes)


def is_admin_user(db: Session, user_id: int) -> bool:
    """集中判断用户是否为管理员；两套持久化表示冲突时拒绝授权。"""

    return has_effective_role(db, user_id, *_ADMIN_ROLE_CODES)


def is_super_admin_user(db: Session, user_id: int) -> bool:
    """判断用户是否为唯一且数据一致的 ``admin`` 超级管理员。"""

    return has_effective_role(db, user_id, "super_admin") and is_unique_super_admin(
        db,
        db.get(User, user_id),
    )


# ============================================================================
# 用户角色分配
# ============================================================================


def assign_roles_to_user(
    db: Session,
    user_id: int,
    role_ids: List[int],
    *,
    commit: bool = True,
    actor: User | None = None,
) -> None:
    """给用户分配角色(覆盖式)

    先删除用户的所有旧角色关联,再插入唯一固定角色关联。

    Args:
        db: 数据库会话
        user_id: 用户ID
        role_ids: 角色ID列表(将完全替换用户现有角色)
    """
    target = db.query(User).populate_existing().filter(User.id == user_id).with_for_update().one_or_none()
    if target is None:
        raise NotFoundError("用户不存在", code=40400)
    if len(set(role_ids)) != 1:
        raise BadRequestError("每个用户必须且只能分配一个基础角色", code=40000)
    roles = db.query(Role).filter(Role.id.in_(set(role_ids)), Role.status == "active").all()
    if len({role.id for role in roles}) != len(set(role_ids)):
        raise BadRequestError("包含不存在的角色", code=40000)
    if target.username == SUPER_ADMIN_USERNAME:
        raise ForbiddenError("超级管理员角色固定，不允许修改", code=40322)
    if any(role.code == SUPER_ADMIN_ROLE for role in roles):
        raise ForbiddenError("超级管理员只能是 admin", code=40322)
    if any(role.code not in ASSIGNABLE_ROLE_CODES for role in roles):
        raise BadRequestError("仅允许分配普通用户、评审员或管理员", code=40000)
    if actor is not None and not is_admin_user(db, actor.id):
        raise ForbiddenError("需要管理员权限", code=40300)

    db.query(UserRole).filter(UserRole.user_id == user_id).delete()
    for rid in role_ids:
        db.add(UserRole(user_id=user_id, role_id=rid))
    role_codes = {role.code for role in roles}
    if "admin" in role_codes:
        target.role = "admin"
    elif "reviewer" in role_codes:
        target.role = "reviewer"
    else:
        target.role = "user"
    target.token_version = (target.token_version or 0) + 1
    if commit:
        db.commit()
    else:
        db.flush()


def get_user_roles(db: Session, user_id: int) -> List[Role]:
    """获取用户的唯一有效角色列表。

    持久化表冲突或账号失效时返回空列表，避免上层将多角色并集
    重新解释成有效身份。

    Args:
        db: 数据库会话
        user_id: 用户ID

    Returns:
        List[Role]: 用户的有效角色 ORM 对象列表
    """
    effective_role = resolve_effective_role_code(db, user_id)
    if not effective_role:
        return []
    return (
        db.query(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(
            UserRole.user_id == user_id,
            Role.status == "active",
            Role.code == effective_role,
        )
        .order_by(Role.sort)
        .all()
    )


def get_user_permissions(db: Session, user_id: int) -> Set[str]:
    """获取用户唯一有效角色的权限 code 集合。

    角色表与旧字段冲突时返回空集，禁止继续聚合冲突角色的权限。
    管理员用户不在此处绕过(绕过逻辑在 check_permission 中处理),
    本函数如实反映用户在 RBAC 表中分配的权限。

    Args:
        db: 数据库会话
        user_id: 用户ID

    Returns:
        Set[str]: 权限编码字符串集合(如 {"project:create", "review:view"})
    """
    effective_role = resolve_effective_role_code(db, user_id)
    if not effective_role:
        return set()
    rows = (
        db.query(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .join(Role, Role.id == UserRole.role_id)
        .filter(
            UserRole.user_id == user_id,
            Role.status == "active",
            Role.code == effective_role,
        )
        .all()
    )
    return {r[0] for r in rows}


def get_user_menus(db: Session, user_id: int) -> List[Menu]:
    """获取用户可见菜单(基于角色聚合)

    菜单可见性规则:
    1. 管理员用户:返回所有 visible=1 的菜单
    2. 普通用户:返回 visible=1 且(permission_code 为空 OR permission_code 在用户权限集合内)的菜单

    Args:
        db: 数据库会话
        user_id: 用户ID

    Returns:
        List[Menu]: 可见菜单 ORM 对象列表,按 sort 升序
    """
    effective_role = resolve_effective_role_code(db, user_id)
    if not effective_role:
        return []
    menus = db.query(Menu).filter(Menu.visible == 1).order_by(Menu.sort).all()
    if effective_role in _ADMIN_ROLE_CODES:
        return menus

    perm_codes = get_user_permissions(db, user_id)
    return [m for m in menus if not m.permission_code or m.permission_code in perm_codes]


def get_user_data_scope(db: Session, user_id: int) -> DataScope:
    """获取用户唯一有效角色的数据范围。

    用户可能有多个角色,每个角色对应一条 DataScope 记录。
    按 _DATA_SCOPE_PRIORITY 优先级取最高的 scope_type:
    all > project_member > custom > project_own

    若用户无任何数据范围记录,返回一个仅含 scope_type 的虚拟 DataScope
    (scope_type=_DEFAULT_SCOPE_TYPE, project_ids=None)。

    Args:
        db: 数据库会话
        user_id: 用户ID

    Returns:
        DataScope: 用户最高优先级的数据范围对象(无 id 表示虚拟默认范围)
    """
    effective_role = resolve_effective_role_code(db, user_id)
    if not effective_role:
        return DataScope(scope_type=_DEFAULT_SCOPE_TYPE, project_ids=None)
    scopes = (
        db.query(DataScope)
        .join(Role, Role.id == DataScope.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(
            UserRole.user_id == user_id,
            Role.status == "active",
            Role.code == effective_role,
        )
        .all()
    )
    if not scopes:
        return DataScope(scope_type=_DEFAULT_SCOPE_TYPE, project_ids=None)

    # 按优先级选取最高的数据范围
    best = max(
        scopes,
        key=lambda s: _DATA_SCOPE_PRIORITY.get(s.scope_type, 0),
    )
    return best


# ============================================================================
# 角色与权限管理
# ============================================================================


def list_roles(db: Session) -> List[Role]:
    """列出所有角色

    Args:
        db: 数据库会话

    Returns:
        List[Role]: 全部角色列表,按 sort 升序
    """
    return (
        db.query(Role)
        .filter(Role.status == "active", Role.code.in_((*ASSIGNABLE_ROLE_CODES, SUPER_ADMIN_ROLE)))
        .order_by(Role.sort)
        .all()
    )


def list_permissions(db: Session) -> List[Permission]:
    """列出所有权限点

    Args:
        db: 数据库会话

    Returns:
        List[Permission]: 全部权限点列表,按 id 升序
    """
    return db.query(Permission).order_by(Permission.id).all()


def create_role(db: Session, role_in: RoleCreateIn, *, actor: User | None = None) -> Role:
    """拒绝创建角色。

    角色模型自 v3.9.5 起固定，不再允许通过接口扩展角色集合。

    Args:
        db: 数据库会话
        role_in: 角色创建请求体(含 name/code/description/status/sort/permission_codes)

    Raises:
        ForbiddenError: 角色模型已固定
    """
    raise ForbiddenError("角色模型已固定为普通用户、评审员、管理员和唯一超级管理员", code=40324)


def update_role(db: Session, role_id: int, role_in: RoleUpdateIn, *, actor: User | None = None) -> Role:
    """更新角色

    仅更新提供的字段。若 permission_codes 字段提供(包括空列表),
    将完全替换角色的权限关联(覆盖式)。

    Args:
        db: 数据库会话
        role_id: 角色ID
        role_in: 角色更新请求体

    Returns:
        Role: 更新后的角色 ORM 对象

    Raises:
        NotFoundError: 角色不存在
    """
    role = _require_configurable_role(db, role_id)
    if role_in.status is not None and role_in.status != "active":
        raise ForbiddenError("三个固定基础角色不允许停用", code=40324)
    if role_in.permission_codes is not None and any(
        code.startswith(SERVER_OPS_PERMISSION_PREFIX) for code in role_in.permission_codes
    ):
        raise ForbiddenError("服务器权限仅属于超级管理员", code=40323)

    # 更新基础字段(仅更新提供的字段)
    if role_in.name is not None:
        role.name = role_in.name
    if role_in.description is not None:
        role.description = role_in.description
    if role_in.status is not None:
        role.status = role_in.status
    if role_in.sort is not None:
        role.sort = role_in.sort

    # 权限覆盖式更新(仅当 permission_codes 字段被显式提供时)
    if role_in.permission_codes is not None:
        db.query(RolePermission).filter(RolePermission.role_id == role_id).delete()
        perm_codes = set(role_in.permission_codes)
        if perm_codes:
            perms = db.query(Permission).filter(Permission.code.in_(perm_codes)).all()
            for perm in perms:
                db.add(RolePermission(role_id=role.id, permission_id=perm.id))

    db.commit()
    db.refresh(role)
    return role


def assign_permissions_to_role(
    db: Session,
    role_id: int,
    permission_ids: List[int],
    *,
    actor: User | None = None,
) -> None:
    """给角色分配权限(覆盖式)

    先删除角色的所有旧权限关联,再插入新权限关联。
    若 permission_ids 为空,等价于撤销角色全部权限。

    Args:
        db: 数据库会话
        role_id: 角色ID
        permission_ids: 权限ID列表(将完全替换角色现有权限)
    """
    _require_configurable_role(db, role_id)
    permissions = db.query(Permission).filter(Permission.id.in_(set(permission_ids))).all() if permission_ids else []
    if len({permission.id for permission in permissions}) != len(set(permission_ids)):
        raise BadRequestError("包含不存在的权限", code=40000)
    if any(permission.code.startswith(SERVER_OPS_PERMISSION_PREFIX) for permission in permissions):
        raise ForbiddenError("服务器权限仅属于超级管理员", code=40323)

    db.query(RolePermission).filter(RolePermission.role_id == role_id).delete()
    for pid in permission_ids:
        db.add(RolePermission(role_id=role_id, permission_id=pid))
    db.commit()


def get_role_permissions(db: Session, role_id: int) -> List[Permission]:
    """获取角色的权限列表

    Args:
        db: 数据库会话
        role_id: 角色ID

    Returns:
        List[Permission]: 角色关联的权限 ORM 对象列表,按 id 升序
    """
    return (
        db.query(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .filter(RolePermission.role_id == role_id)
        .order_by(Permission.id)
        .all()
    )


def update_data_scope(
    db: Session,
    role_id: int,
    scope_in: DataScopeIn,
    *,
    actor: User | None = None,
) -> DataScope:
    """更新角色的数据范围

    若角色已有数据范围记录则更新,否则新建。

    Args:
        db: 数据库会话
        role_id: 角色ID
        scope_in: 数据范围设置请求体(含 scope_type/project_ids)

    Returns:
        DataScope: 更新或新建的数据范围 ORM 对象

    Raises:
        NotFoundError: 角色不存在
    """
    _require_configurable_role(db, role_id)

    scope = db.query(DataScope).filter(DataScope.role_id == role_id).first()
    if scope:
        scope.scope_type = scope_in.scope_type
        scope.project_ids = scope_in.project_ids
    else:
        scope = DataScope(
            role_id=role_id,
            scope_type=scope_in.scope_type,
            project_ids=scope_in.project_ids,
        )
        db.add(scope)

    db.commit()
    db.refresh(scope)
    return scope


def get_role_data_scope(db: Session, role_id: int) -> Optional[DataScope]:
    """查询单个角色当前数据范围，不存在时返回 ``None``。"""

    if db.get(Role, role_id) is None:
        raise NotFoundError("角色不存在", code=40400)
    return db.query(DataScope).filter(DataScope.role_id == role_id).first()


def get_users_by_role(db: Session, role_code: str) -> List[User]:
    """按角色 code 查询用户列表

    通过角色编码反查所有拥有该角色的用户。

    Args:
        db: 数据库会话
        role_code: 角色编码(如 "reviewer")

    Returns:
        List[User]: 拥有该角色的用户 ORM 对象列表
    """
    return (
        db.query(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .filter(Role.code == role_code, Role.status == "active", User.role == role_code, User.status == 1)
        .order_by(User.id)
        .all()
    )


# ============================================================================
# 权限与数据范围校验
# ============================================================================


def check_permission(db: Session, user_id: int, permission_code: str) -> bool:
    """检查用户是否拥有某权限

    管理员绕过:拥有 admin/super_admin 角色的用户对所有权限返回 True。

    Args:
        db: 数据库会话
        user_id: 用户ID
        permission_code: 权限编码(如 "project:create")

    Returns:
        bool: 拥有权限返回 True,否则 False
    """
    if permission_code.startswith(SERVER_OPS_PERMISSION_PREFIX):
        return is_super_admin_user(db, user_id)

    # 普通管理员只绕过程序内权限，不绕过服务器权限。
    if is_admin_user(db, user_id):
        return True

    perm_codes = get_user_permissions(db, user_id)
    return permission_code in perm_codes


def check_data_scope(db: Session, user_id: int, target_user_id: int) -> bool:
    """检查数据范围(用户能否访问目标用户的数据)

    根据用户的最高优先级数据范围 scope_type 决定:
    - all: 可访问全部数据 → True
    - project_own: 仅自己的数据 → target_user_id == user_id
    - project_member: 参与项目数据 → 后续实现,目前返回 True
    - custom: 自定义项目列表 → 后续实现,目前返回 True

    Args:
        db: 数据库会话
        user_id: 当前用户ID
        target_user_id: 被访问的目标用户ID

    Returns:
        bool: 允许访问返回 True,否则 False
    """
    # 管理员绕过
    if is_admin_user(db, user_id):
        return True

    scope = get_user_data_scope(db, user_id)
    scope_type = scope.scope_type

    if scope_type == "all":
        # 全部数据:允许访问任何用户的数据
        return True
    if scope_type == "project_own":
        # 仅自己的数据:目标用户必须是自己
        return target_user_id == user_id
    if scope_type in {"project_member", "custom"}:
        # 该依赖项只有目标用户 ID，无法证明项目成员或自定义项目关系。
        # 在增加项目级契约前只允许访问本人，禁止临时全放行。
        return target_user_id == user_id
    # 未知范围类型,默认拒绝
    return False
