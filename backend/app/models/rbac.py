"""
RBAC 权限模型

基于角色的访问控制(Role-Based Access Control),提供角色/权限/菜单/数据范围管理。
- Role: 角色表,定义系统角色(普通用户/评审员/审计员/管理员/超级管理员)
- Permission: 权限点表,定义细粒度操作权限(如 review:start)
- RolePermission: 角色-权限关联表(多对多)
- UserRole: 用户-角色关联表(多对多)
- Menu: 菜单表,自引用树形结构,关联权限点控制可见性
- DataScope: 数据范围表,定义角色可访问的数据边界
"""
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class Role(Base, IdMixin, TimestampMixin):
    """角色表

    定义系统角色,预置角色(user/reviewer/auditor/admin/super_admin)不可删除。

    Attributes:
        name: 角色名称(如"评审员")
        code: 角色编码(唯一,如 reviewer)
        description: 角色描述
        status: 状态(active/disabled)
        sort: 排序值,越小越靠前
        is_builtin: 是否预置角色(1=预置不可删,0=自定义)
    """

    __tablename__ = "role"
    __table_args__ = (
        UniqueConstraint("code", name="uk_role_code"),
        Index("ix_role_status", "status"),
    )

    name = Column(String(64), nullable=False, comment="角色名称")
    code = Column(String(64), nullable=False, comment="角色编码")
    description = Column(String(255), nullable=True, comment="角色描述")
    status = Column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
        comment="状态: active/disabled",
    )
    sort = Column(Integer, nullable=False, default=100, server_default="100", comment="排序值")
    is_builtin = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="是否预置角色: 1=是,0=否",
    )


class Permission(Base, IdMixin, TimestampMixin):
    """权限点表

    定义细粒度操作权限,按模块分组(如 review:start 属于 review 模块)。

    Attributes:
        code: 权限编码(唯一,如 review:start)
        name: 权限名称(如"启动审查")
        module: 所属模块(如 review)
        type: 权限类型(api/menu/button)
        description: 权限描述
    """

    __tablename__ = "permission"
    __table_args__ = (
        UniqueConstraint("code", name="uk_permission_code"),
        Index("ix_permission_module", "module"),
    )

    code = Column(String(64), nullable=False, comment="权限编码")
    name = Column(String(128), nullable=False, comment="权限名称")
    module = Column(String(32), nullable=False, comment="所属模块")
    type = Column(
        String(16),
        nullable=False,
        default="api",
        server_default="api",
        comment="权限类型: api/menu/button",
    )
    description = Column(String(255), nullable=True, comment="权限描述")


class RolePermission(Base, IdMixin, TimestampMixin):
    """角色-权限关联表

    建立角色与权限的多对多关系。

    Attributes:
        role_id: 角色ID
        permission_id: 权限ID
    """

    __tablename__ = "role_permission"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uk_role_permission"),
        Index("ix_role_permission_role", "role_id"),
        Index("ix_role_permission_perm", "permission_id"),
    )

    role_id = Column(BigInteger, nullable=False, comment="角色ID")
    permission_id = Column(BigInteger, nullable=False, comment="权限ID")


class UserRole(Base, IdMixin, TimestampMixin):
    """用户-角色关联表

    建立用户与角色的多对多关系。

    Attributes:
        user_id: 用户ID
        role_id: 角色ID
    """

    __tablename__ = "user_role"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uk_user_role"),
        Index("ix_user_role_user", "user_id"),
        Index("ix_user_role_role", "role_id"),
    )

    user_id = Column(BigInteger, nullable=False, comment="用户ID")
    role_id = Column(BigInteger, nullable=False, comment="角色ID")


class Menu(Base, IdMixin, TimestampMixin):
    """菜单表

    自引用树形结构,通过 parent_id 建立父子关系,顶级菜单 parent_id 为 None。
    通过 permission_code 关联权限点控制菜单可见性。

    Attributes:
        parent_id: 父菜单ID,顶级菜单为 None
        name: 菜单名称
        path: 前端路由路径
        component: 前端组件路径
        icon: 菜单图标
        sort: 排序值
        permission_code: 关联权限编码
        visible: 是否可见(1=可见,0=隐藏)
        is_builtin: 是否预置菜单(1=预置不可删,0=自定义)
    """

    __tablename__ = "menu"
    __table_args__ = (
        Index("ix_menu_parent", "parent_id"),
    )

    parent_id = Column(BigInteger, nullable=True, comment="父菜单ID,顶级为NULL")
    name = Column(String(64), nullable=False, comment="菜单名称")
    path = Column(String(255), nullable=True, comment="前端路由路径")
    component = Column(String(255), nullable=True, comment="前端组件路径")
    icon = Column(String(64), nullable=True, comment="菜单图标")
    sort = Column(Integer, nullable=False, default=100, server_default="100", comment="排序值")
    permission_code = Column(String(64), nullable=True, comment="关联权限编码")
    visible = Column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="是否可见: 1=可见,0=隐藏",
    )
    is_builtin = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="是否预置菜单: 1=是,0=否",
    )


class DataScope(Base, IdMixin, TimestampMixin):
    """数据范围表

    定义角色可访问的数据边界,scope_type 控制:
    - all: 全部数据
    - project_own: 仅自己创建的项目
    - project_member: 参与的项目
    - custom: 自定义项目列表(project_ids 字段)

    Attributes:
        role_id: 角色ID
        scope_type: 范围类型(all/project_own/project_member/custom)
        project_ids: 自定义项目ID列表(custom 类型时使用)
    """

    __tablename__ = "data_scope"
    __table_args__ = (
        Index("ix_data_scope_role", "role_id"),
    )

    role_id = Column(BigInteger, nullable=False, comment="角色ID")
    scope_type = Column(
        String(32),
        nullable=False,
        comment="范围类型: all/project_own/project_member/custom",
    )
    project_ids = Column(JSON, nullable=True, comment="自定义项目ID列表(custom类型时使用)")
