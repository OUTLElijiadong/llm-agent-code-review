"""T10 RBAC API 路由集成测试

覆盖:
1. RBAC 管理接口(admin 专用):admin 可访问,普通用户 403
2. 角色 CRUD:创建/更新/删除(含内置角色不可删)
3. 权限分配:角色权限覆盖式分配
4. 数据范围:更新与查询
5. 菜单:树形查询
6. 按角色查用户
7. 现有路由权限点注入:无权限 403,有权限 200,admin 绕过 200
8. 分配角色后用户权限变化

测试策略:
- 使用 TestClient + 内存 SQLite(StaticPool 共享连接)
- 覆盖 get_db 与 get_current_user 依赖,分别模拟 admin/普通用户
- require_permission / require_admin 闭包内部依赖 get_current_user,覆盖即生效
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.main import app
from app.models.rbac import Menu, Permission, Role
from app.models.user import User

# ============================================================================
# Fixture: 共享内存数据库 + 基础种子数据
# ============================================================================


@pytest.fixture
def db() -> Session:
    """创建共享内存 SQLite 会话(StaticPool 保证 TestClient 与 fixture 共用同一连接)

    Yields:
        Session: 数据库会话
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def seed(db: Session) -> Dict[str, Any]:
    """植入基础种子数据:admin/normal 用户、角色、权限点、菜单

    Args:
        db: 数据库会话

    Returns:
        Dict[str, Any]: 包含 admin/normal/reviewer_role/builtin_role/permissions 等实体引用
    """
    # 用户:admin(旧版字段管理员)与 normal(普通用户无角色)
    admin = User(
        id=1, username="admin", password="x", email="admin@t.com",
        nickname="管理员", role="admin", status=1,
    )
    normal = User(
        id=2, username="normal", password="x", email="normal@t.com",
        nickname="普通用户", role="user", status=1,
    )
    db.add_all([admin, normal])
    db.flush()

    # 角色:reviewer(自定义)与 admin_role(内置,用于测试不可删)
    reviewer_role = Role(
        id=10, name="评审员", code="reviewer", description="代码评审",
        status="active", sort=100, is_builtin=0,
    )
    builtin_role = Role(
        id=20, name="管理员", code="admin", description="系统管理员",
        status="active", sort=50, is_builtin=1,
    )
    db.add_all([reviewer_role, builtin_role])
    db.flush()

    # 权限点:覆盖 project/agent/review 模块关键权限
    permissions = {}
    perm_specs = [
        ("project:view", "查看项目", "project"),
        ("project:create", "创建项目", "project"),
        ("project:update", "更新项目", "project"),
        ("project:delete", "删除项目", "project"),
        ("agent:view", "查看Agent", "agent"),
        ("agent:chat", "Agent对话", "agent"),
        ("review:start", "启动审查", "review"),
        ("review:view", "查看审查", "review"),
        ("review:cancel", "取消审查", "review"),
        ("issue:view", "查看问题", "issue"),
        ("issue:handle", "处理问题", "issue"),
        ("issue:batch", "批量处理问题", "issue"),
        ("file:view", "查看文件", "file"),
    ]
    for idx, (code, name, module) in enumerate(perm_specs, start=100):
        p = Permission(
            id=idx, code=code, name=name, module=module,
            type="api", description=name,
        )
        db.add(p)
        permissions[code] = p
    db.flush()

    # 菜单:顶级 + 子菜单,用于测试树形构建
    m_root = Menu(
        id=1, parent_id=None, name="项目管理", path="/project",
        component="Project", icon="folder", sort=100,
        permission_code=None, visible=1, is_builtin=1,
    )
    m_child = Menu(
        id=2, parent_id=1, name="项目列表", path="/project/list",
        component="ProjectList", icon="list", sort=110,
        permission_code="project:view", visible=1, is_builtin=0,
    )
    m_hidden = Menu(
        id=3, parent_id=None, name="隐藏菜单", path="/hidden",
        component=None, icon=None, sort=200,
        permission_code=None, visible=0, is_builtin=0,
    )
    db.add_all([m_root, m_child, m_hidden])
    db.commit()

    return {
        "admin": admin,
        "normal": normal,
        "reviewer_role": reviewer_role,
        "builtin_role": builtin_role,
        "permissions": permissions,
        "menu_root": m_root,
        "menu_child": m_child,
    }


@pytest.fixture
def client_factory(db: Session):
    """创建 TestClient 的工厂,覆盖 get_db 与 get_current_user 依赖

    用法:
        client = client_factory(seed["admin"])
        response = client.get("/api/rbac/roles")

    Args:
        db: 数据库会话

    Yields:
        Callable[[User], TestClient]: 接收用户对象返回配置好的 TestClient
    """

    def _make(user: User) -> TestClient:
        """构造绑定指定用户的 TestClient

        Args:
            user: 当前模拟登录的用户

        Returns:
            TestClient: 已覆盖鉴权依赖的测试客户端
        """
        def override_db():
            """覆盖数据库依赖,返回共享会话"""
            yield db

        def override_user() -> User:
            """覆盖当前用户依赖,返回指定用户"""
            return user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        return TestClient(app)

    yield _make
    # 清理依赖覆盖,避免影响后续测试
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _ok(response):
    """断言响应成功(HTTP 200 + 业务 code=0)

    Args:
        response: TestClient 响应对象

    Returns:
        object: 响应 body 中的 data 字段
    """
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]


def _forbidden(response):
    """断言响应为权限拒绝(HTTP 403)

    Args:
        response: TestClient 响应对象
    """
    assert response.status_code == 403, response.text


# ============================================================================
# RBAC 管理接口: admin 用户可访问
# ============================================================================


class TestRbacAdminApi:
    """admin 用户访问 RBAC 管理接口应全部成功"""

    def test_admin_list_roles(self, db, seed, client_factory):
        """admin 列出全部角色应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/roles"))
        assert len(data) == 2
        codes = {r["code"] for r in data}
        assert codes == {"reviewer", "admin"}

    def test_admin_list_permissions(self, db, seed, client_factory):
        """admin 列出全部权限点应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/permissions"))
        assert len(data) == len(seed["permissions"])

    def test_admin_list_menus_tree(self, db, seed, client_factory):
        """admin 列出菜单树应返回 200 且结构正确"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/menus"))
        # 可见菜单 2 个(隐藏菜单不返回),顶级菜单 1 个(项目管理)
        assert len(data) == 1
        root = data[0]
        assert root["name"] == "项目管理"
        assert len(root["children"]) == 1
        assert root["children"][0]["name"] == "项目列表"

    def test_admin_list_user_roles(self, db, seed, client_factory):
        """admin 查询用户角色列表应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/users/2/roles"))
        # normal 用户初始无角色
        assert data == []

    def test_admin_list_user_permissions(self, db, seed, client_factory):
        """admin 查询用户权限点应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/users/2/permissions"))
        assert data == []

    def test_admin_list_user_menus(self, db, seed, client_factory):
        """admin 查询用户可见菜单应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/users/1/menus"))
        # admin 看到所有可见菜单
        assert len(data) == 1

    def test_admin_get_user_data_scope(self, db, seed, client_factory):
        """admin 查询用户数据范围应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/users/2/data-scope"))
        # 无数据范围记录,默认 project_own
        assert data["scope_type"] == "project_own"

    def test_admin_list_users_by_role(self, db, seed, client_factory):
        """admin 按角色编码查用户列表应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/roles/reviewer/users"))
        assert data == []


# ============================================================================
# RBAC 管理接口: 普通用户被拒(403)
# ============================================================================


class TestRbacNormalUserForbidden:
    """普通用户访问 RBAC 管理接口应返回 403"""

    def test_normal_list_roles_forbidden(self, db, seed, client_factory):
        """普通用户列出角色应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/rbac/roles"))

    def test_normal_create_role_forbidden(self, db, seed, client_factory):
        """普通用户创建角色应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.post("/api/rbac/roles", json={
            "name": "x", "code": "x", "permission_codes": [],
        }))

    def test_normal_assign_user_roles_forbidden(self, db, seed, client_factory):
        """普通用户分配用户角色应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.post("/api/rbac/users/1/roles", json={"role_ids": []}))

    def test_normal_list_permissions_forbidden(self, db, seed, client_factory):
        """普通用户列出权限点应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/rbac/permissions"))

    def test_normal_list_menus_forbidden(self, db, seed, client_factory):
        """普通用户列出菜单应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/rbac/menus"))

    def test_normal_list_user_roles_forbidden(self, db, seed, client_factory):
        """普通用户查询用户角色应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/rbac/users/1/roles"))

    def test_normal_delete_role_forbidden(self, db, seed, client_factory):
        """普通用户删除角色应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.delete("/api/rbac/roles/10"))


# ============================================================================
# 角色 CRUD 测试
# ============================================================================


class TestRoleCrud:
    """角色创建/更新/删除测试"""

    def test_admin_create_role(self, db, seed, client_factory):
        """admin 创建角色应成功并返回角色信息"""
        client = client_factory(seed["admin"])
        data = _ok(client.post("/api/rbac/roles", json={
            "name": "测试角色", "code": "test_role", "description": "测试",
            "permission_codes": ["project:view"],
        }))
        assert data["code"] == "test_role"
        assert data["is_builtin"] == 0
        assert "project:view" in data["permission_codes"]

    def test_admin_create_role_duplicate_code(self, db, seed, client_factory):
        """admin 创建角色编码重复应返回业务错误"""
        client = client_factory(seed["admin"])
        response = client.post("/api/rbac/roles", json={
            "name": "重复", "code": "reviewer",
        })
        assert response.status_code == 400

    def test_admin_update_role(self, db, seed, client_factory):
        """admin 更新角色字段应成功"""
        client = client_factory(seed["admin"])
        data = _ok(client.put("/api/rbac/roles/10", json={
            "name": "评审员V2", "sort": 200,
        }))
        assert data["name"] == "评审员V2"
        assert data["sort"] == 200

    def test_admin_update_role_not_found(self, db, seed, client_factory):
        """admin 更新不存在角色应 404"""
        client = client_factory(seed["admin"])
        response = client.put("/api/rbac/roles/999", json={"name": "x"})
        assert response.status_code == 404

    def test_admin_delete_custom_role(self, db, seed, client_factory):
        """admin 删除自定义角色应成功"""
        client = client_factory(seed["admin"])
        _ok(client.delete("/api/rbac/roles/10"))
        # 再次查询角色列表,reviewer 应已删除
        data = _ok(client.get("/api/rbac/roles"))
        assert all(r["code"] != "reviewer" for r in data)

    def test_admin_delete_builtin_role_forbidden(self, db, seed, client_factory):
        """admin 删除系统内置角色应返回 400(业务错误)"""
        client = client_factory(seed["admin"])
        response = client.delete("/api/rbac/roles/20")
        assert response.status_code == 400

    def test_admin_delete_role_not_found(self, db, seed, client_factory):
        """admin 删除不存在角色应 404"""
        client = client_factory(seed["admin"])
        response = client.delete("/api/rbac/roles/999")
        assert response.status_code == 404


# ============================================================================
# 权限分配测试
# ============================================================================


class TestPermissionAssignment:
    """角色权限分配与查询测试"""

    def test_admin_list_role_permissions_empty(self, db, seed, client_factory):
        """admin 查询角色权限(初始为空)应返回 200"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/rbac/roles/10/permissions"))
        assert data == []

    def test_admin_assign_role_permissions(self, db, seed, client_factory):
        """admin 分配角色权限应成功且覆盖式替换"""
        client = client_factory(seed["admin"])
        perm_view = seed["permissions"]["project:view"].id
        perm_create = seed["permissions"]["project:create"].id
        _ok(client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_view, perm_create],
        }))
        # 查询验证
        data = _ok(client.get("/api/rbac/roles/10/permissions"))
        codes = {p["code"] for p in data}
        assert codes == {"project:view", "project:create"}

    def test_admin_assign_role_permissions_overwrite(self, db, seed, client_factory):
        """admin 再次分配权限应覆盖旧权限"""
        client = client_factory(seed["admin"])
        perm_view = seed["permissions"]["project:view"].id
        perm_agent = seed["permissions"]["agent:view"].id
        # 第一次分配 project:view
        _ok(client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_view],
        }))
        # 第二次分配 agent:view(覆盖)
        _ok(client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_agent],
        }))
        data = _ok(client.get("/api/rbac/roles/10/permissions"))
        codes = {p["code"] for p in data}
        assert codes == {"agent:view"}

    def test_admin_assign_empty_clears_permissions(self, db, seed, client_factory):
        """admin 分配空权限列表应清空角色权限"""
        client = client_factory(seed["admin"])
        perm_view = seed["permissions"]["project:view"].id
        _ok(client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_view],
        }))
        _ok(client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [],
        }))
        data = _ok(client.get("/api/rbac/roles/10/permissions"))
        assert data == []


# ============================================================================
# 数据范围与用户角色分配测试
# ============================================================================


class TestDataScopeAndUserAssignment:
    """数据范围更新与用户角色分配测试"""

    def test_admin_update_role_data_scope(self, db, seed, client_factory):
        """admin 更新角色数据范围应成功"""
        client = client_factory(seed["admin"])
        data = _ok(client.put("/api/rbac/roles/10/data-scope", json={
            "role_id": 10, "scope_type": "all",
        }))
        assert data["scope_type"] == "all"
        assert data["role_id"] == 10

    def test_admin_update_role_data_scope_custom(self, db, seed, client_factory):
        """admin 更新角色数据范围为 custom 应保存 project_ids"""
        client = client_factory(seed["admin"])
        data = _ok(client.put("/api/rbac/roles/10/data-scope", json={
            "role_id": 10, "scope_type": "custom", "project_ids": [1, 2, 3],
        }))
        assert data["scope_type"] == "custom"
        assert data["project_ids"] == [1, 2, 3]

    def test_admin_assign_user_roles(self, db, seed, client_factory):
        """admin 给用户分配角色应成功"""
        client = client_factory(seed["admin"])
        _ok(client.post("/api/rbac/users/2/roles", json={"role_ids": [10]}))
        # 验证用户角色已分配
        data = _ok(client.get("/api/rbac/users/2/roles"))
        assert len(data) == 1
        assert data[0]["code"] == "reviewer"

    def test_admin_list_users_by_role_after_assign(self, db, seed, client_factory):
        """admin 分配角色后按角色查用户应返回对应用户"""
        client = client_factory(seed["admin"])
        _ok(client.post("/api/rbac/users/2/roles", json={"role_ids": [10]}))
        data = _ok(client.get("/api/rbac/roles/reviewer/users"))
        assert len(data) == 1
        assert data[0]["username"] == "normal"


# ============================================================================
# 分配角色后权限变化测试
# ============================================================================


class TestPermissionChange:
    """分配角色后用户权限变化测试"""

    def test_assign_role_grants_permissions(self, db, seed, client_factory):
        """分配角色(含权限)后用户权限点应更新"""
        client = client_factory(seed["admin"])
        # 给 reviewer 角色分配 project:view 权限
        perm_view = seed["permissions"]["project:view"].id
        _ok(client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_view],
        }))
        # 给 normal 用户分配 reviewer 角色
        _ok(client.post("/api/rbac/users/2/roles", json={"role_ids": [10]}))
        # 查询 normal 用户权限应包含 project:view
        data = _ok(client.get("/api/rbac/users/2/permissions"))
        assert "project:view" in data

    def test_revoke_role_removes_permissions(self, db, seed, client_factory):
        """撤销用户角色后权限应清空"""
        client = client_factory(seed["admin"])
        perm_view = seed["permissions"]["project:view"].id
        _ok(client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_view],
        }))
        _ok(client.post("/api/rbac/users/2/roles", json={"role_ids": [10]}))
        # 撤销角色(空列表覆盖)
        _ok(client.post("/api/rbac/users/2/roles", json={"role_ids": []}))
        data = _ok(client.get("/api/rbac/users/2/permissions"))
        assert data == []


# ============================================================================
# 现有路由权限点注入测试
# ============================================================================


class TestBusinessRoutePermission:
    """现有路由(projects/agents/review)权限点注入测试"""

    def test_normal_no_perm_list_projects_forbidden(self, db, seed, client_factory):
        """普通用户无 project:view 权限访问项目列表应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/projects"))

    def test_normal_no_perm_create_project_forbidden(self, db, seed, client_factory):
        """普通用户无 project:create 权限创建项目应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.post("/api/projects", json={}))

    def test_admin_list_projects_ok(self, db, seed, client_factory):
        """admin 用户访问项目列表应 200(权限绕过)"""
        client = client_factory(seed["admin"])
        data = _ok(client.get("/api/projects"))
        # 空数据库,项目列表为空
        assert "items" in data or "list" in data or isinstance(data, dict)

    def test_normal_no_perm_list_agents_forbidden(self, db, seed, client_factory):
        """普通用户无 agent:view 权限访问 Agent 列表应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/agents"))

    def test_normal_no_perm_list_issues_forbidden(self, db, seed, client_factory):
        """普通用户无 issue:view 权限访问问题列表应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/issues"))

    def test_normal_no_perm_start_review_forbidden(self, db, seed, client_factory):
        """普通用户无 review:start 权限启动审查应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.post("/api/review/start", json={}))

    def test_normal_no_perm_list_code_files_forbidden(self, db, seed, client_factory):
        """普通用户无 file:view 权限访问文件列表应 403"""
        client = client_factory(seed["normal"])
        _forbidden(client.get("/api/code-files?project_id=1"))

    def test_admin_list_agents_ok(self, db, seed, client_factory):
        """admin 用户访问 Agent 列表应 200(权限绕过)"""
        client = client_factory(seed["admin"])
        # admin 绕过权限,即使 Agent 服务返回空列表也应 200
        response = client.get("/api/agents")
        assert response.status_code == 200

    def test_normal_with_perm_list_projects_ok(self, db, seed, client_factory):
        """普通用户有 project:view 权限访问项目列表应 200"""
        admin_client = client_factory(seed["admin"])
        # 给 reviewer 角色分配 project:view 权限
        perm_view = seed["permissions"]["project:view"].id
        admin_client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_view],
        })
        # 给 normal 用户分配 reviewer 角色
        admin_client.post("/api/rbac/users/2/roles", json={"role_ids": [10]})
        # 切换为 normal 用户访问
        normal_client = client_factory(seed["normal"])
        response = normal_client.get("/api/projects")
        assert response.status_code == 200

    def test_normal_with_perm_list_agents_ok(self, db, seed, client_factory):
        """普通用户有 agent:view 权限访问 Agent 列表应 200"""
        admin_client = client_factory(seed["admin"])
        perm_agent = seed["permissions"]["agent:view"].id
        admin_client.put("/api/rbac/roles/10/permissions", json={
            "permission_ids": [perm_agent],
        })
        admin_client.post("/api/rbac/users/2/roles", json={"role_ids": [10]})
        normal_client = client_factory(seed["normal"])
        response = normal_client.get("/api/agents")
        assert response.status_code == 200
