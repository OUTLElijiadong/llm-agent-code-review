"""单元测试: RBAC 业务服务层与 FastAPI 依赖注入(T09)

覆盖:
1. 角色分配(assign_roles_to_user): 覆盖式/空列表/多角色
2. 用户角色与权限查询(get_user_roles/get_user_permissions): 多角色并集去重
3. 权限检查(check_permission): 命中/未命中/admin 绕过
4. 数据范围检查(check_data_scope): all/project_own/project_member/custom 四种 scope
5. 角色管理(create_role/update_role/list_roles): 含权限码分配
6. 权限分配(assign_permissions_to_role/get_role_permissions): 覆盖式
7. 菜单查询(get_user_menus): admin 全量/普通用户按权限过滤
8. 数据范围管理(update_data_scope/get_user_data_scope): 优先级选取
9. 用户反查(get_users_by_role)
10. 依赖注入(require_permission/require_admin): 直接调用闭包验证
11. 异常类(PermissionError): 状态码/错误码
"""
from __future__ import annotations

import pytest

from app.core.exceptions import BadRequestError, NotFoundError, PermissionError
from app.core.permission_codes import ALL_PERMISSION_CODES, PermissionCode
from app.core.rbac_dependency import (
    require_admin,
    require_permission,
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
from app.services.rbac_service import (
    assign_permissions_to_role,
    assign_roles_to_user,
    check_data_scope,
    check_permission,
    create_role,
    get_role_permissions,
    get_user_data_scope,
    get_user_menus,
    get_user_permissions,
    get_user_roles,
    get_users_by_role,
    is_admin_user,
    list_permissions,
    list_roles,
    update_data_scope,
    update_role,
)


# ============================================================================
# 辅助工厂函数
# ============================================================================


def _make_user(db, uid, username, role="user"):
    """构造并持久化用户

    Args:
        db: 数据库会话
        uid: 用户ID
        username: 用户名
        role: 旧版 User.role 字段值(默认 "user")

    Returns:
        User: 已持久化的用户
    """
    user = User(
        id=uid, username=username, password="x", role=role, status=1,
        email=f"{username}@t.com",
    )
    db.add(user)
    db.commit()
    return user


def _make_role(db, rid, code, name, status="active", sort=100, is_builtin=0):
    """构造并持久化角色

    Args:
        db: 数据库会话
        rid: 角色ID
        code: 角色编码
        name: 角色名称
        status: 状态(默认 active)
        sort: 排序值
        is_builtin: 是否预置(0=否,1=是)

    Returns:
        Role: 已持久化的角色
    """
    role = Role(
        id=rid, code=code, name=name, description=f"角色-{name}",
        status=status, sort=sort, is_builtin=is_builtin,
    )
    db.add(role)
    db.commit()
    return role


def _make_permission(db, pid, code, module, name=None):
    """构造并持久化权限点

    Args:
        db: 数据库会话
        pid: 权限ID
        code: 权限编码
        module: 所属模块
        name: 权限名称(默认由 code 生成)

    Returns:
        Permission: 已持久化的权限点
    """
    perm = Permission(
        id=pid, code=code, name=name or code, module=module,
        type="api", description=f"权限-{code}",
    )
    db.add(perm)
    db.commit()
    return perm


def _make_menu(db, mid, name, permission_code=None, visible=1, sort=100, parent_id=None):
    """构造并持久化菜单

    Args:
        db: 数据库会话
        mid: 菜单ID
        name: 菜单名称
        permission_code: 关联权限编码(默认 None 表示无权限限制)
        visible: 是否可见(1=可见,0=隐藏)
        sort: 排序值
        parent_id: 父菜单ID

    Returns:
        Menu: 已持久化的菜单
    """
    menu = Menu(
        id=mid, parent_id=parent_id, name=name, path=f"/{name}",
        component=None, icon=None, sort=sort, permission_code=permission_code,
        visible=visible, is_builtin=0,
    )
    db.add(menu)
    db.commit()
    return menu


def _link_role_permission(db, role_id, permission_id):
    """关联角色与权限

    Args:
        db: 数据库会话
        role_id: 角色ID
        permission_id: 权限ID
    """
    db.add(RolePermission(role_id=role_id, permission_id=permission_id))
    db.commit()


def _link_user_role(db, user_id, role_id):
    """关联用户与角色

    Args:
        db: 数据库会话
        user_id: 用户ID
        role_id: 角色ID
    """
    db.add(UserRole(user_id=user_id, role_id=role_id))
    db.commit()


def _make_data_scope(db, role_id, scope_type, project_ids=None):
    """构造并持久化数据范围

    Args:
        db: 数据库会话
        role_id: 角色ID
        scope_type: 范围类型(all/project_own/project_member/custom)
        project_ids: 自定义项目ID列表

    Returns:
        DataScope: 已持久化的数据范围
    """
    scope = DataScope(
        role_id=role_id, scope_type=scope_type, project_ids=project_ids,
    )
    db.add(scope)
    db.commit()
    return scope


# ============================================================================
# 1. assign_roles_to_user 测试
# ============================================================================


class TestAssignRolesToUser:
    """assign_roles_to_user 角色分配测试"""

    def test_assign_single_role(self, db):
        """分配单个角色后用户应拥有该角色"""
        user = _make_user(db, 1, "u1")
        role = _make_role(db, 10, "reviewer", "评审员")
        assign_roles_to_user(db, user.id, [role.id])
        roles = get_user_roles(db, user.id)
        assert len(roles) == 1
        assert roles[0].code == "reviewer"

    def test_assign_multiple_roles(self, db):
        """分配多个角色后用户应拥有全部角色"""
        user = _make_user(db, 1, "u1")
        r1 = _make_role(db, 11, "reviewer", "评审员", sort=200)
        r2 = _make_role(db, 12, "auditor", "审计员", sort=300)
        assign_roles_to_user(db, user.id, [r1.id, r2.id])
        roles = get_user_roles(db, user.id)
        assert {r.code for r in roles} == {"reviewer", "auditor"}

    def test_assign_overwrites_previous(self, db):
        """覆盖式分配: 旧角色应被清除"""
        user = _make_user(db, 1, "u1")
        r1 = _make_role(db, 11, "reviewer", "评审员")
        r2 = _make_role(db, 12, "auditor", "审计员")
        # 先分配 r1
        assign_roles_to_user(db, user.id, [r1.id])
        assert len(get_user_roles(db, user.id)) == 1
        # 再分配 r2(覆盖)
        assign_roles_to_user(db, user.id, [r2.id])
        roles = get_user_roles(db, user.id)
        assert len(roles) == 1
        assert roles[0].code == "auditor"

    def test_assign_empty_clears_roles(self, db):
        """空列表分配应清空用户全部角色"""
        user = _make_user(db, 1, "u1")
        r1 = _make_role(db, 11, "reviewer", "评审员")
        assign_roles_to_user(db, user.id, [r1.id])
        assert len(get_user_roles(db, user.id)) == 1
        # 清空
        assign_roles_to_user(db, user.id, [])
        assert get_user_roles(db, user.id) == []


# ============================================================================
# 2. get_user_permissions 测试
# ============================================================================


class TestGetUserPermissions:
    """get_user_permissions 用户权限查询测试"""

    def test_single_role_permissions(self, db):
        """单角色用户应返回该角色的全部权限"""
        user = _make_user(db, 1, "u1")
        role = _make_role(db, 10, "reviewer", "评审员")
        p1 = _make_permission(db, 101, "review:start", "review")
        p2 = _make_permission(db, 102, "review:view", "review")
        _link_role_permission(db, role.id, p1.id)
        _link_role_permission(db, role.id, p2.id)
        _link_user_role(db, user.id, role.id)

        perms = get_user_permissions(db, user.id)
        assert perms == {"review:start", "review:view"}

    def test_multi_role_permissions_union(self, db):
        """多角色用户权限应为并集"""
        user = _make_user(db, 1, "u1")
        r1 = _make_role(db, 10, "reviewer", "评审员")
        r2 = _make_role(db, 11, "auditor", "审计员")
        p1 = _make_permission(db, 101, "review:start", "review")
        p2 = _make_permission(db, 102, "audit:view", "audit")
        _link_role_permission(db, r1.id, p1.id)
        _link_role_permission(db, r2.id, p2.id)
        _link_user_role(db, user.id, r1.id)
        _link_user_role(db, user.id, r2.id)

        perms = get_user_permissions(db, user.id)
        assert perms == {"review:start", "audit:view"}

    def test_permissions_deduplicated(self, db):
        """多角色共享同一权限时应去重"""
        user = _make_user(db, 1, "u1")
        r1 = _make_role(db, 10, "reviewer", "评审员")
        r2 = _make_role(db, 11, "auditor", "审计员")
        p1 = _make_permission(db, 101, "review:view", "review")
        # 两个角色都关联同一权限
        _link_role_permission(db, r1.id, p1.id)
        _link_role_permission(db, r2.id, p1.id)
        _link_user_role(db, user.id, r1.id)
        _link_user_role(db, user.id, r2.id)

        perms = get_user_permissions(db, user.id)
        assert perms == {"review:view"}

    def test_no_roles_returns_empty(self, db):
        """无角色用户应返回空权限集合"""
        user = _make_user(db, 1, "u1")
        perms = get_user_permissions(db, user.id)
        assert perms == set()

    def test_disabled_role_excluded(self, db):
        """禁用角色的权限不应包含在用户权限中"""
        user = _make_user(db, 1, "u1")
        role = _make_role(db, 10, "reviewer", "评审员", status="disabled")
        p1 = _make_permission(db, 101, "review:start", "review")
        _link_role_permission(db, role.id, p1.id)
        _link_user_role(db, user.id, role.id)

        perms = get_user_permissions(db, user.id)
        assert perms == set()


# ============================================================================
# 3. check_permission 测试
# ============================================================================


class TestCheckPermission:
    """check_permission 权限检查测试"""

    def test_has_permission(self, db):
        """用户拥有权限应返回 True"""
        user = _make_user(db, 1, "u1")
        role = _make_role(db, 10, "reviewer", "评审员")
        p1 = _make_permission(db, 101, "review:start", "review")
        _link_role_permission(db, role.id, p1.id)
        _link_user_role(db, user.id, role.id)

        assert check_permission(db, user.id, "review:start") is True

    def test_missing_permission(self, db):
        """用户无权限应返回 False"""
        user = _make_user(db, 1, "u1")
        role = _make_role(db, 10, "reviewer", "评审员")
        p1 = _make_permission(db, 101, "review:start", "review")
        _link_role_permission(db, role.id, p1.id)
        _link_user_role(db, user.id, role.id)

        assert check_permission(db, user.id, "review:approve") is False

    def test_admin_bypass_via_legacy_role(self, db):
        """admin 用户(旧版 User.role)应绕过权限检查"""
        user = _make_user(db, 1, "admin", role="admin")
        # admin 用户无任何 RBAC 角色与权限分配
        assert check_permission(db, user.id, "any:permission") is True

    def test_admin_bypass_via_rbac_role(self, db):
        """admin 角色(新版 RBAC)应绕过权限检查"""
        user = _make_user(db, 1, "u1", role="user")
        admin_role = _make_role(db, 10, "admin", "管理员")
        _link_user_role(db, user.id, admin_role.id)
        # 无任何权限分配,但 admin 角色应绕过
        assert check_permission(db, user.id, "any:permission") is True

    def test_super_admin_bypass(self, db):
        """super_admin 角色应绕过权限检查"""
        user = _make_user(db, 1, "u1", role="user")
        sa_role = _make_role(db, 10, "super_admin", "超级管理员")
        _link_user_role(db, user.id, sa_role.id)
        assert check_permission(db, user.id, "any:permission") is True


# ============================================================================
# 4. check_data_scope 测试
# ============================================================================


class TestCheckDataScope:
    """check_data_scope 数据范围检查测试(4 种 scope_type)"""

    def test_scope_all_allows_any_user(self, db):
        """scope_type=all 应允许访问任意用户数据"""
        user = _make_user(db, 1, "u1", role="user")
        target = _make_user(db, 2, "u2", role="user")
        role = _make_role(db, 10, "auditor", "审计员")
        _link_user_role(db, user.id, role.id)
        _make_data_scope(db, role.id, "all")

        assert check_data_scope(db, user.id, target.id) is True

    def test_scope_project_own_only_self(self, db):
        """scope_type=project_own 应仅允许访问自己的数据"""
        user = _make_user(db, 1, "u1", role="user")
        target = _make_user(db, 2, "u2", role="user")
        role = _make_role(db, 10, "user", "普通用户")
        _link_user_role(db, user.id, role.id)
        _make_data_scope(db, role.id, "project_own")

        # 访问自己 → True
        assert check_data_scope(db, user.id, user.id) is True
        # 访问他人 → False
        assert check_data_scope(db, user.id, target.id) is False

    def test_scope_project_member_allows_any(self, db):
        """scope_type=project_member 后续实现,目前返回 True"""
        user = _make_user(db, 1, "u1", role="user")
        target = _make_user(db, 2, "u2", role="user")
        role = _make_role(db, 10, "reviewer", "评审员")
        _link_user_role(db, user.id, role.id)
        _make_data_scope(db, role.id, "project_member")

        assert check_data_scope(db, user.id, target.id) is True

    def test_scope_custom_allows_any(self, db):
        """scope_type=custom 后续实现,目前返回 True"""
        user = _make_user(db, 1, "u1", role="user")
        target = _make_user(db, 2, "u2", role="user")
        role = _make_role(db, 10, "custom_role", "自定义角色")
        _link_user_role(db, user.id, role.id)
        _make_data_scope(db, role.id, "custom", project_ids=[1, 2, 3])

        assert check_data_scope(db, user.id, target.id) is True

    def test_admin_bypass_data_scope(self, db):
        """管理员应绕过数据范围检查"""
        user = _make_user(db, 1, "admin", role="admin")
        target = _make_user(db, 2, "u2", role="user")
        # admin 无任何数据范围,但仍应允许访问
        assert check_data_scope(db, user.id, target.id) is True

    def test_no_scope_defaults_project_own(self, db):
        """无数据范围记录时默认 project_own(仅自己)"""
        user = _make_user(db, 1, "u1", role="user")
        target = _make_user(db, 2, "u2", role="user")
        role = _make_role(db, 10, "user", "普通用户")
        _link_user_role(db, user.id, role.id)
        # 不创建 data_scope 记录

        assert check_data_scope(db, user.id, user.id) is True
        assert check_data_scope(db, user.id, target.id) is False


# ============================================================================
# 5. 角色管理测试
# ============================================================================


class TestRoleManagement:
    """create_role / update_role / list_roles 测试"""

    def test_create_role_basic(self, db):
        """创建角色应持久化并返回"""
        role_in = RoleCreateIn(name="自定义角色", code="custom", description="测试")
        role = create_role(db, role_in)
        assert role.id is not None
        assert role.code == "custom"
        assert role.is_builtin == 0

    def test_create_role_with_permissions(self, db):
        """创建角色时同时分配权限码"""
        _make_permission(db, 101, "review:start", "review")
        _make_permission(db, 102, "review:view", "review")
        role_in = RoleCreateIn(
            name="自定义角色", code="custom",
            permission_codes=["review:start", "review:view"],
        )
        role = create_role(db, role_in)
        perms = get_role_permissions(db, role.id)
        assert {p.code for p in perms} == {"review:start", "review:view"}

    def test_create_role_duplicate_code_raises(self, db):
        """角色编码重复应抛出 BadRequestError"""
        _make_role(db, 10, "custom", "自定义角色")
        role_in = RoleCreateIn(name="另一角色", code="custom")
        with pytest.raises(BadRequestError):
            create_role(db, role_in)

    def test_update_role_fields(self, db):
        """更新角色字段应持久化"""
        role = _make_role(db, 10, "custom", "原名称", sort=100)
        role_in = RoleUpdateIn(name="新名称", sort=200)
        updated = update_role(db, role.id, role_in)
        assert updated.name == "新名称"
        assert updated.sort == 200

    def test_update_role_permissions_overwrite(self, db):
        """更新角色权限码应覆盖式替换"""
        role = _make_role(db, 10, "custom", "自定义")
        p1 = _make_permission(db, 101, "review:start", "review")
        p2 = _make_permission(db, 102, "review:view", "review")
        p3 = _make_permission(db, 103, "review:cancel", "review")
        # 初始分配 p1, p2
        _link_role_permission(db, role.id, p1.id)
        _link_role_permission(db, role.id, p2.id)
        # 覆盖为 p2, p3
        role_in = RoleUpdateIn(permission_codes=["review:view", "review:cancel"])
        update_role(db, role.id, role_in)
        perms = get_role_permissions(db, role.id)
        assert {p.code for p in perms} == {"review:view", "review:cancel"}

    def test_update_role_clear_permissions(self, db):
        """传空权限码列表应清空角色权限"""
        role = _make_role(db, 10, "custom", "自定义")
        p1 = _make_permission(db, 101, "review:start", "review")
        _link_role_permission(db, role.id, p1.id)
        role_in = RoleUpdateIn(permission_codes=[])
        update_role(db, role.id, role_in)
        assert get_role_permissions(db, role.id) == []

    def test_update_role_not_found_raises(self, db):
        """更新不存在的角色应抛出 NotFoundError"""
        with pytest.raises(NotFoundError):
            update_role(db, 999, RoleUpdateIn(name="x"))

    def test_list_roles_ordered_by_sort(self, db):
        """列出角色应按 sort 升序"""
        _make_role(db, 10, "b", "B", sort=200)
        _make_role(db, 11, "a", "A", sort=100)
        _make_role(db, 12, "c", "C", sort=300)
        roles = list_roles(db)
        assert [r.code for r in roles] == ["a", "b", "c"]


# ============================================================================
# 6. 权限分配测试
# ============================================================================


class TestPermissionAssignment:
    """assign_permissions_to_role / get_role_permissions 测试"""

    def test_assign_permissions_overwrites(self, db):
        """assign_permissions_to_role 应覆盖式替换"""
        role = _make_role(db, 10, "custom", "自定义")
        p1 = _make_permission(db, 101, "p1", "mod")
        p2 = _make_permission(db, 102, "p2", "mod")
        p3 = _make_permission(db, 103, "p3", "mod")
        # 初始分配 p1
        _link_role_permission(db, role.id, p1.id)
        # 覆盖为 p2, p3
        assign_permissions_to_role(db, role.id, [p2.id, p3.id])
        perms = get_role_permissions(db, role.id)
        assert {p.code for p in perms} == {"p2", "p3"}

    def test_assign_empty_clears_permissions(self, db):
        """空权限列表应清空角色权限"""
        role = _make_role(db, 10, "custom", "自定义")
        p1 = _make_permission(db, 101, "p1", "mod")
        _link_role_permission(db, role.id, p1.id)
        assign_permissions_to_role(db, role.id, [])
        assert get_role_permissions(db, role.id) == []

    def test_list_permissions_returns_all(self, db):
        """list_permissions 应返回全部权限点"""
        _make_permission(db, 101, "p1", "mod")
        _make_permission(db, 102, "p2", "mod")
        perms = list_permissions(db)
        assert len(perms) == 2


# ============================================================================
# 7. 菜单查询测试
# ============================================================================


class TestGetUserMenus:
    """get_user_menus 菜单可见性测试"""

    def test_admin_sees_all_menus(self, db):
        """admin 用户应看到所有可见菜单"""
        user = _make_user(db, 1, "admin", role="admin")
        _make_menu(db, 1, "m1", permission_code="review:start")
        _make_menu(db, 2, "m2", permission_code="audit:view")
        _make_menu(db, 3, "m3", permission_code=None)
        menus = get_user_menus(db, user.id)
        assert {m.name for m in menus} == {"m1", "m2", "m3"}

    def test_normal_user_sees_permitted_menus(self, db):
        """普通用户应仅看到无权限限制或有权限的菜单"""
        user = _make_user(db, 1, "u1", role="user")
        role = _make_role(db, 10, "reviewer", "评审员")
        p1 = _make_permission(db, 101, "review:start", "review")
        _link_role_permission(db, role.id, p1.id)
        _link_user_role(db, user.id, role.id)

        _make_menu(db, 1, "m1", permission_code="review:start")  # 有权限
        _make_menu(db, 2, "m2", permission_code="audit:view")    # 无权限
        _make_menu(db, 3, "m3", permission_code=None)            # 无限制

        menus = get_user_menus(db, user.id)
        names = {m.name for m in menus}
        assert "m1" in names  # 有权限
        assert "m3" in names  # 无限制
        assert "m2" not in names  # 无权限

    def test_hidden_menus_excluded(self, db):
        """visible=0 的菜单不应返回(即使 admin)"""
        user = _make_user(db, 1, "admin", role="admin")
        _make_menu(db, 1, "visible_menu", visible=1)
        _make_menu(db, 2, "hidden_menu", visible=0)
        menus = get_user_menus(db, user.id)
        names = {m.name for m in menus}
        assert "visible_menu" in names
        assert "hidden_menu" not in names

    def test_menus_ordered_by_sort(self, db):
        """菜单应按 sort 升序返回"""
        user = _make_user(db, 1, "admin", role="admin")
        _make_menu(db, 1, "c", sort=300)
        _make_menu(db, 2, "a", sort=100)
        _make_menu(db, 3, "b", sort=200)
        menus = get_user_menus(db, user.id)
        assert [m.name for m in menus] == ["a", "b", "c"]


# ============================================================================
# 8. 数据范围管理测试
# ============================================================================


class TestDataScopeManagement:
    """update_data_scope / get_user_data_scope 测试"""

    def test_update_data_scope_creates_new(self, db):
        """update_data_scope 对无记录角色应新建"""
        role = _make_role(db, 10, "custom", "自定义")
        scope_in = DataScopeIn(role_id=role.id, scope_type="all")
        scope = update_data_scope(db, role.id, scope_in)
        assert scope.scope_type == "all"
        assert scope.role_id == role.id

    def test_update_data_scope_updates_existing(self, db):
        """update_data_scope 对已有记录应更新"""
        role = _make_role(db, 10, "custom", "自定义")
        _make_data_scope(db, role.id, "project_own")
        scope_in = DataScopeIn(role_id=role.id, scope_type="all")
        scope = update_data_scope(db, role.id, scope_in)
        assert scope.scope_type == "all"

    def test_update_data_scope_role_not_found(self, db):
        """更新不存在角色的数据范围应抛出 NotFoundError"""
        scope_in = DataScopeIn(role_id=999, scope_type="all")
        with pytest.raises(NotFoundError):
            update_data_scope(db, 999, scope_in)

    def test_get_user_data_scope_highest_priority(self, db):
        """多角色数据范围应取最高优先级(all > project_member > custom > project_own)"""
        user = _make_user(db, 1, "u1", role="user")
        r1 = _make_role(db, 10, "user", "普通用户")
        r2 = _make_role(db, 11, "auditor", "审计员")
        _link_user_role(db, user.id, r1.id)
        _link_user_role(db, user.id, r2.id)
        _make_data_scope(db, r1.id, "project_own")   # 优先级 1
        _make_data_scope(db, r2.id, "all")            # 优先级 4

        scope = get_user_data_scope(db, user.id)
        assert scope.scope_type == "all"

    def test_get_user_data_scope_default_when_empty(self, db):
        """无数据范围记录时应返回默认 project_own"""
        user = _make_user(db, 1, "u1", role="user")
        role = _make_role(db, 10, "user", "普通用户")
        _link_user_role(db, user.id, role.id)
        scope = get_user_data_scope(db, user.id)
        assert scope.scope_type == "project_own"


# ============================================================================
# 9. get_users_by_role 测试
# ============================================================================


class TestGetUsersByRole:
    """get_users_by_role 按角色反查用户测试"""

    def test_get_users_by_role_code(self, db):
        """按角色编码应返回所有拥有该角色的用户"""
        role = _make_role(db, 10, "reviewer", "评审员")
        u1 = _make_user(db, 1, "u1")
        u2 = _make_user(db, 2, "u2")
        _make_user(db, 3, "u3")  # 无角色
        _link_user_role(db, u1.id, role.id)
        _link_user_role(db, u2.id, role.id)

        users = get_users_by_role(db, "reviewer")
        assert {u.id for u in users} == {1, 2}

    def test_get_users_by_unknown_role_code(self, db):
        """未知角色编码应返回空列表"""
        _make_user(db, 1, "u1")
        users = get_users_by_role(db, "nonexistent")
        assert users == []


# ============================================================================
# 10. is_admin_user 测试
# ============================================================================


class TestIsAdminUser:
    """is_admin_user 管理员判定测试"""

    def test_legacy_admin_field(self, db):
        """User.role='admin' 应判定为管理员"""
        user = _make_user(db, 1, "admin", role="admin")
        assert is_admin_user(db, user.id) is True

    def test_legacy_super_admin_field(self, db):
        """User.role='super_admin' 应判定为管理员"""
        user = _make_user(db, 1, "sa", role="super_admin")
        assert is_admin_user(db, user.id) is True

    def test_rbac_admin_role(self, db):
        """RBAC admin 角色应判定为管理员"""
        user = _make_user(db, 1, "u1", role="user")
        admin_role = _make_role(db, 10, "admin", "管理员")
        _link_user_role(db, user.id, admin_role.id)
        assert is_admin_user(db, user.id) is True

    def test_normal_user_not_admin(self, db):
        """普通用户应判定为非管理员"""
        user = _make_user(db, 1, "u1", role="user")
        role = _make_role(db, 10, "reviewer", "评审员")
        _link_user_role(db, user.id, role.id)
        assert is_admin_user(db, user.id) is False


# ============================================================================
# 11. PermissionError 异常类测试
# ============================================================================


class TestPermissionError:
    """PermissionError 异常类测试"""

    def test_permission_error_default_message(self):
        """PermissionError 默认消息应为'无操作权限'"""
        err = PermissionError()
        assert err.message == "无操作权限"
        assert err.http_status == 403
        assert err.code == 40303
        assert err.error_code == "PERMISSION_DENIED"

    def test_permission_error_custom_message(self):
        """PermissionError 应支持自定义消息"""
        err = PermissionError("需要 review:start 权限")
        assert err.message == "需要 review:start 权限"
        assert err.http_status == 403

    def test_permission_error_is_forbidden_subclass(self):
        """PermissionError 应为 ForbiddenError 子类"""
        err = PermissionError()
        from app.core.exceptions import ForbiddenError
        assert isinstance(err, ForbiddenError)


# ============================================================================
# 12. 依赖注入测试(直接调用闭包)
# ============================================================================


class TestRbacDependency:
    """FastAPI 依赖注入闭包直接调用测试"""

    def test_require_permission_passes(self, db):
        """require_permission 闭包: 有权限应返回用户对象"""
        user = _make_user(db, 1, "u1", role="user")
        role = _make_role(db, 10, "reviewer", "评审员")
        p1 = _make_permission(db, 101, "review:start", "review")
        _link_role_permission(db, role.id, p1.id)
        _link_user_role(db, user.id, role.id)

        dep = require_permission("review:start")
        result = dep(user=user, db=db)
        assert result.id == user.id

    def test_require_permission_denied(self, db):
        """require_permission 闭包: 无权限应抛出 PermissionError"""
        user = _make_user(db, 1, "u1", role="user")
        role = _make_role(db, 10, "reviewer", "评审员")
        p1 = _make_permission(db, 101, "review:start", "review")
        _link_role_permission(db, role.id, p1.id)
        _link_user_role(db, user.id, role.id)

        dep = require_permission("review:approve")
        with pytest.raises(PermissionError):
            dep(user=user, db=db)

    def test_require_admin_passes(self, db):
        """require_admin 闭包: 管理员应通过"""
        user = _make_user(db, 1, "admin", role="admin")
        result = require_admin(user=user, db=db)
        assert result.id == user.id

    def test_require_admin_denied(self, db):
        """require_admin 闭包: 普通用户应抛出 PermissionError"""
        user = _make_user(db, 1, "u1", role="user")
        with pytest.raises(PermissionError):
            require_admin(user=user, db=db)


# ============================================================================
# 13. permission_codes 常量测试
# ============================================================================


class TestPermissionCodes:
    """权限点常量定义测试"""

    def test_all_permission_codes_count(self):
        """ALL_PERMISSION_CODES 应包含 42 个权限点"""
        assert len(ALL_PERMISSION_CODES) == 42

    def test_permission_code_values_unique(self):
        """所有权限编码应唯一"""
        codes = ALL_PERMISSION_CODES
        assert len(codes) == len(set(codes))

    def test_known_permission_codes(self):
        """关键权限编码应符合预期值"""
        assert PermissionCode.PROJECT_CREATE == "project:create"
        assert PermissionCode.REVIEW_START == "review:start"
        assert PermissionCode.AGENT_VIEW == "agent:view"
        assert PermissionCode.AUDIT_VIEW == "audit:view"
