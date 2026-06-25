"""
RBAC 管理 API 路由

提供角色/权限/菜单/数据范围/用户角色分配的管理接口。
全部接口要求管理员身份(拥有 admin/super_admin 角色),通过 require_admin 依赖统一校验。

路由前缀: /api/rbac
路由清单:
- POST   /rbac/users/{user_id}/roles          分配用户角色(覆盖式)
- GET    /rbac/users/{user_id}/roles          查询用户角色列表
- GET    /rbac/users/{user_id}/permissions    查询用户权限点列表
- GET    /rbac/users/{user_id}/menus          查询用户可见菜单(树形)
- GET    /rbac/users/{user_id}/data-scope     查询用户数据范围
- GET    /rbac/roles                          列出全部角色
- POST   /rbac/roles                          创建角色
- PUT    /rbac/roles/{role_id}                更新角色
- DELETE /rbac/roles/{role_id}                删除角色(系统内置角色不可删除)
- GET    /rbac/roles/{role_id}/permissions    查询角色权限
- PUT    /rbac/roles/{role_id}/permissions    分配角色权限(覆盖式)
- PUT    /rbac/roles/{role_id}/data-scope     更新角色数据范围
- GET    /rbac/permissions                    列出全部权限点
- GET    /rbac/menus                          列出全部菜单(树形)
- GET    /rbac/roles/{role_code}/users        按角色编码查询用户列表
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.rbac_dependency import require_admin
from app.models.rbac import DataScope, Menu, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.rbac import (
    DataScopeIn,
    DataScopeOut,
    MenuOut,
    PermissionOut,
    RoleCreateIn,
    RoleOut,
    RoleUpdateIn,
    UserRoleAssignIn,
)
from app.services import rbac_service

router = APIRouter()


# ============================================================================
# 请求体模型(路由内联,避免污染 schemas 模块)
# ============================================================================


class RolePermissionAssignIn(BaseModel):
    """角色权限分配请求体

    用于 PUT /rbac/roles/{role_id}/permissions 接口,覆盖式分配权限。

    Attributes:
        permission_ids: 权限ID列表(将完全替换角色现有权限)
    """

    permission_ids: List[int] = Field(default_factory=list, description="权限ID列表")


# ============================================================================
# 辅助函数
# ============================================================================


def _build_menu_tree(menus: List[Menu]) -> List[MenuOut]:
    """将扁平菜单列表构建为树形结构

    根据 parent_id 建立父子关系,顶级菜单 parent_id 为 None。
    子菜单按 sort 升序排列。

    Args:
        menus: 扁平菜单 ORM 对象列表(已按 sort 升序)

    Returns:
        List[MenuOut]: 顶级菜单列表,children 字段递归包含子菜单
    """
    # 按 id 索引,便于 O(1) 查找父菜单
    nodes: Dict[int, MenuOut] = {}
    for m in menus:
        nodes[m.id] = MenuOut(
            id=m.id,
            parent_id=m.parent_id,
            name=m.name,
            path=m.path,
            component=m.component,
            icon=m.icon,
            sort=m.sort,
            permission_code=m.permission_code,
            visible=m.visible,
            is_builtin=m.is_builtin,
            children=[],
            create_time=m.create_time,
        )

    roots: List[MenuOut] = []
    for m in menus:
        node = nodes[m.id]
        if m.parent_id is None or m.parent_id not in nodes:
            # 顶级菜单或父节点不在当前列表中,作为根节点
            roots.append(node)
        else:
            nodes[m.parent_id].children.append(node)
    return roots


def _role_to_out(role: Role, permission_codes: Optional[List[str]] = None) -> RoleOut:
    """将 Role ORM 对象转换为 RoleOut 响应模型

    Args:
        role: 角色 ORM 对象
        permission_codes: 关联权限编码列表(可选,为 None 时留空)

    Returns:
        RoleOut: 角色响应模型
    """
    return RoleOut(
        id=role.id,
        name=role.name,
        code=role.code,
        description=role.description,
        status=role.status,
        sort=role.sort,
        is_builtin=role.is_builtin,
        permission_codes=permission_codes or [],
        create_time=role.create_time,
        update_time=role.update_time,
    )


# ============================================================================
# 用户角色与权限查询接口
# ============================================================================


@router.post("/users/{user_id}/roles", response_model=Resp[None])
def assign_user_roles(
    user_id: int,
    payload: UserRoleAssignIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """分配用户角色(覆盖式)

    用 payload.role_ids 完全替换用户的角色关联。
    若 role_ids 为空,等价于撤销用户全部角色。

    Args:
        user_id: 目标用户ID
        payload: 角色分配请求体(含 role_ids)
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[None]: 操作结果,data 为 None
    """
    rbac_service.assign_roles_to_user(db, user_id, payload.role_ids)
    return Resp(data=None)


@router.get("/users/{user_id}/roles", response_model=Resp[List[RoleOut]])
def list_user_roles(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询用户角色列表

    仅返回 status='active' 的有效角色,按 sort 升序。

    Args:
        user_id: 目标用户ID
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[RoleOut]]: 用户有效角色列表
    """
    roles = rbac_service.get_user_roles(db, user_id)
    return Resp(data=[_role_to_out(r) for r in roles])


@router.get("/users/{user_id}/permissions", response_model=Resp[List[str]])
def list_user_permissions(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询用户权限点列表

    聚合用户所有有效角色的权限点,返回去重后的权限编码列表。
    注意:管理员用户的 RBAC 权限集合可能为空(其绕过逻辑在 check_permission 中处理),
    本接口如实反映用户在 RBAC 表中分配的权限。

    Args:
        user_id: 目标用户ID
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[str]]: 权限编码字符串列表
    """
    codes = rbac_service.get_user_permissions(db, user_id)
    return Resp(data=sorted(codes))


@router.get("/users/{user_id}/menus", response_model=Resp[List[MenuOut]])
def list_user_menus(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询用户可见菜单(树形)

    管理员用户返回所有可见菜单;普通用户返回可见且(无权限限制或拥有对应权限)的菜单。
    返回结果为树形结构,顶级菜单 parent_id 为 None。

    Args:
        user_id: 目标用户ID
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[MenuOut]]: 用户可见菜单树
    """
    menus = rbac_service.get_user_menus(db, user_id)
    return Resp(data=_build_menu_tree(menus))


@router.get("/users/{user_id}/data-scope", response_model=Resp[DataScopeOut])
def get_user_data_scope(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询用户数据范围

    返回用户最高优先级的数据范围(all > project_member > custom > project_own)。
    若用户无任何数据范围记录,返回默认 project_own 范围(虚拟记录,id/role_id 为 0)。

    Args:
        user_id: 目标用户ID
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[DataScopeOut]: 用户数据范围信息
    """
    scope = rbac_service.get_user_data_scope(db, user_id)
    # 服务层在用户无数据范围记录时返回虚拟 DataScope(无 id/role_id/create_time),
    # 此处补齐默认值以适配 DataScopeOut 的必填字段约束。
    if scope.id is None:
        return Resp(data=DataScopeOut(
            id=0,
            role_id=0,
            scope_type=scope.scope_type,
            project_ids=scope.project_ids,
            create_time=datetime.now(),
        ))
    return Resp(data=DataScopeOut.model_validate(scope))


# ============================================================================
# 角色管理接口
# ============================================================================


@router.get("/roles", response_model=Resp[List[RoleOut]])
def list_roles(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """列出全部角色

    按 sort 升序返回所有角色(含预置与自定义)。
    每个角色附带其关联的权限编码列表。

    Args:
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[RoleOut]]: 全部角色列表
    """
    roles = rbac_service.list_roles(db)
    result: List[RoleOut] = []
    for r in roles:
        perms = rbac_service.get_role_permissions(db, r.id)
        result.append(_role_to_out(r, [p.code for p in perms]))
    return Resp(data=result)


@router.post("/roles", response_model=Resp[RoleOut])
def create_role(
    payload: RoleCreateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """创建角色

    创建自定义角色(is_builtin=0),若提供 permission_codes 则同时分配权限。
    角色编码必须唯一,重复将抛出 BadRequestError。

    Args:
        payload: 角色创建请求体
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[RoleOut]: 新建的角色信息(含权限编码列表)
    """
    role = rbac_service.create_role(db, payload)
    perms = rbac_service.get_role_permissions(db, role.id)
    return Resp(data=_role_to_out(role, [p.code for p in perms]))


@router.put("/roles/{role_id}", response_model=Resp[RoleOut])
def update_role(
    role_id: int,
    payload: RoleUpdateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """更新角色

    仅更新提供的字段。若 permission_codes 字段被显式提供(包括空列表),
    将覆盖式替换角色权限关联。角色不存在抛出 NotFoundError。

    Args:
        role_id: 角色ID
        payload: 角色更新请求体
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[RoleOut]: 更新后的角色信息(含权限编码列表)
    """
    role = rbac_service.update_role(db, role_id, payload)
    perms = rbac_service.get_role_permissions(db, role.id)
    return Resp(data=_role_to_out(role, [p.code for p in perms]))


@router.delete("/roles/{role_id}", response_model=Resp[None])
def delete_role(
    role_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """删除角色

    系统内置角色(is_builtin=1)不可删除,抛出 BadRequestError。
    删除自定义角色时,同步清理其与权限、用户、数据范围的关联记录。

    Args:
        role_id: 角色ID
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[None]: 操作结果,data 为 None

    Raises:
        NotFoundError: 角色不存在
        BadRequestError: 角色为系统内置角色,不可删除
    """
    role = db.get(Role, role_id)
    if not role:
        raise NotFoundError("角色不存在", code=40400)
    if role.is_builtin == 1:
        raise BadRequestError("系统内置角色不可删除", code=40000)

    # 清理关联记录:角色-权限、用户-角色、数据范围
    db.query(RolePermission).filter(RolePermission.role_id == role_id).delete()
    db.query(UserRole).filter(UserRole.role_id == role_id).delete()
    db.query(DataScope).filter(DataScope.role_id == role_id).delete()
    db.delete(role)
    db.commit()
    return Resp(data=None)


@router.get("/roles/{role_id}/permissions", response_model=Resp[List[PermissionOut]])
def list_role_permissions(
    role_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询角色权限列表

    返回角色关联的全部权限点,按权限ID升序。

    Args:
        role_id: 角色ID
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[PermissionOut]]: 角色权限点列表
    """
    perms = rbac_service.get_role_permissions(db, role_id)
    return Resp(data=[PermissionOut.model_validate(p) for p in perms])


@router.put("/roles/{role_id}/permissions", response_model=Resp[None])
def assign_role_permissions(
    role_id: int,
    payload: RolePermissionAssignIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """分配角色权限(覆盖式)

    用 payload.permission_ids 完全替换角色的权限关联。
    若 permission_ids 为空,等价于撤销角色全部权限。

    Args:
        role_id: 角色ID
        payload: 权限分配请求体(含 permission_ids)
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[None]: 操作结果,data 为 None
    """
    rbac_service.assign_permissions_to_role(db, role_id, payload.permission_ids)
    return Resp(data=None)


@router.put("/roles/{role_id}/data-scope", response_model=Resp[DataScopeOut])
def update_role_data_scope(
    role_id: int,
    payload: DataScopeIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """更新角色数据范围

    若角色已有数据范围记录则更新,否则新建。
    请求体中 role_id 字段将被忽略,以路径参数 role_id 为准。

    Args:
        role_id: 角色ID(路径参数)
        payload: 数据范围设置请求体
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[DataScopeOut]: 更新或新建的数据范围信息
    """
    # 以路径参数为准,覆盖请求体中的 role_id
    scope_in = DataScopeIn(
        role_id=role_id,
        scope_type=payload.scope_type,
        project_ids=payload.project_ids,
    )
    scope = rbac_service.update_data_scope(db, role_id, scope_in)
    return Resp(data=DataScopeOut.model_validate(scope))


@router.get("/roles/{role_code}/users", response_model=Resp[List[Dict[str, Any]]])
def list_users_by_role(
    role_code: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """按角色编码查询用户列表

    通过角色编码反查所有拥有该角色的用户,按用户ID升序。
    返回精简用户信息(id/username/nickname/email/role/status)。

    Args:
        role_code: 角色编码(如 reviewer)
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[Dict[str, Any]]]: 用户信息字典列表
    """
    users = rbac_service.get_users_by_role(db, role_code)
    return Resp(data=[
        {
            "id": u.id,
            "username": u.username,
            "nickname": u.nickname,
            "email": u.email,
            "role": u.role,
            "status": u.status,
        }
        for u in users
    ])


# ============================================================================
# 权限点与菜单查询接口
# ============================================================================


@router.get("/permissions", response_model=Resp[List[PermissionOut]])
def list_permissions(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """列出全部权限点

    按权限ID升序返回系统所有权限点(共 42 个,按模块分组)。

    Args:
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[PermissionOut]]: 全部权限点列表
    """
    perms = rbac_service.list_permissions(db)
    return Resp(data=[PermissionOut.model_validate(p) for p in perms])


@router.get("/menus", response_model=Resp[List[MenuOut]])
def list_menus_tree(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """列出全部菜单(树形)

    返回系统所有可见菜单(visible=1),构建为树形结构。
    按 sort 升序排列。

    Args:
        db: 数据库会话
        _: 管理员鉴权(结果不使用)

    Returns:
        Resp[List[MenuOut]]: 全部菜单树
    """
    menus = (
        db.query(Menu)
        .filter(Menu.visible == 1)
        .order_by(Menu.sort)
        .all()
    )
    return Resp(data=_build_menu_tree(menus))
