"""
RBAC 权限 Pydantic Schema

用于角色/权限/菜单/数据范围管理的请求与响应模型。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RoleCreateIn(BaseModel):
    """创建角色请求体

    Attributes:
        name: 角色名称
        code: 角色编码
        description: 角色描述
        status: 状态(active/disabled)
        sort: 排序值
        permission_codes: 权限编码列表
    """

    name: str = Field(..., min_length=1, max_length=64, description="角色名称")
    code: str = Field(..., min_length=1, max_length=64, description="角色编码")
    description: Optional[str] = Field(default=None, max_length=255, description="角色描述")
    status: str = Field(default="active", pattern="^(active|disabled)$", description="状态")
    sort: int = Field(default=100, ge=0, description="排序值")
    permission_codes: list[str] = Field(default_factory=list, description="权限编码列表")


class RoleUpdateIn(BaseModel):
    """更新角色请求体

    所有字段可选,仅更新提供的字段。

    Attributes:
        name: 角色名称
        description: 角色描述
        status: 状态(active/disabled)
        sort: 排序值
        permission_codes: 权限编码列表
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=64, description="角色名称")
    description: Optional[str] = Field(default=None, max_length=255, description="角色描述")
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$", description="状态")
    sort: Optional[int] = Field(default=None, ge=0, description="排序值")
    permission_codes: Optional[list[str]] = Field(default=None, description="权限编码列表")


class RoleOut(BaseModel):
    """角色响应项

    Attributes:
        id: 角色ID
        name: 角色名称
        code: 角色编码
        description: 角色描述
        status: 状态
        sort: 排序值
        is_builtin: 是否预置角色
        permission_codes: 关联的权限编码列表
        create_time: 创建时间
        update_time: 更新时间
    """

    id: int
    name: str
    code: str
    description: Optional[str] = None
    status: str
    sort: int
    is_builtin: int
    permission_codes: list[str] = []
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}


class PermissionOut(BaseModel):
    """权限点响应项

    Attributes:
        id: 权限ID
        code: 权限编码
        name: 权限名称
        module: 所属模块
        type: 权限类型
        description: 权限描述
        create_time: 创建时间
    """

    id: int
    code: str
    name: str
    module: str
    type: str
    description: Optional[str] = None
    create_time: datetime

    model_config = {"from_attributes": True}


class MenuCreateIn(BaseModel):
    """创建菜单请求体

    Attributes:
        parent_id: 父菜单ID,顶级为空
        name: 菜单名称
        path: 前端路由路径
        component: 前端组件路径
        icon: 菜单图标
        sort: 排序值
        permission_code: 关联权限编码
        visible: 是否可见
    """

    parent_id: Optional[int] = Field(default=None, description="父菜单ID,顶级为空")
    name: str = Field(..., min_length=1, max_length=64, description="菜单名称")
    path: Optional[str] = Field(default=None, max_length=255, description="前端路由路径")
    component: Optional[str] = Field(default=None, max_length=255, description="前端组件路径")
    icon: Optional[str] = Field(default=None, max_length=64, description="菜单图标")
    sort: int = Field(default=100, ge=0, description="排序值")
    permission_code: Optional[str] = Field(default=None, max_length=64, description="关联权限编码")
    visible: int = Field(default=1, ge=0, le=1, description="是否可见: 1=可见,0=隐藏")


class MenuUpdateIn(BaseModel):
    """更新菜单请求体

    所有字段可选,仅更新提供的字段。

    Attributes:
        parent_id: 父菜单ID
        name: 菜单名称
        path: 前端路由路径
        component: 前端组件路径
        icon: 菜单图标
        sort: 排序值
        permission_code: 关联权限编码
        visible: 是否可见
    """

    parent_id: Optional[int] = Field(default=None, description="父菜单ID")
    name: Optional[str] = Field(default=None, min_length=1, max_length=64, description="菜单名称")
    path: Optional[str] = Field(default=None, max_length=255, description="前端路由路径")
    component: Optional[str] = Field(default=None, max_length=255, description="前端组件路径")
    icon: Optional[str] = Field(default=None, max_length=64, description="菜单图标")
    sort: Optional[int] = Field(default=None, ge=0, description="排序值")
    permission_code: Optional[str] = Field(default=None, max_length=64, description="关联权限编码")
    visible: Optional[int] = Field(default=None, ge=0, le=1, description="是否可见")


class MenuOut(BaseModel):
    """菜单响应项(含子菜单)

    自引用树形结构,children 字段递归包含子菜单。

    Attributes:
        id: 菜单ID
        parent_id: 父菜单ID
        name: 菜单名称
        path: 前端路由路径
        component: 前端组件路径
        icon: 菜单图标
        sort: 排序值
        permission_code: 关联权限编码
        visible: 是否可见
        is_builtin: 是否预置菜单
        children: 子菜单列表
        create_time: 创建时间
    """

    id: int
    parent_id: Optional[int] = None
    name: str
    path: Optional[str] = None
    component: Optional[str] = None
    icon: Optional[str] = None
    sort: int
    permission_code: Optional[str] = None
    visible: int
    is_builtin: int
    children: list["MenuOut"] = []
    create_time: datetime

    model_config = {"from_attributes": True}


class DataScopeIn(BaseModel):
    """数据范围设置请求体

    Attributes:
        role_id: 角色ID
        scope_type: 范围类型(all/project_own/project_member/custom)
        project_ids: 自定义项目ID列表(custom类型时使用)
    """

    role_id: int = Field(..., description="角色ID")
    scope_type: str = Field(
        ...,
        pattern="^(all|project_own|project_member|custom)$",
        description="范围类型",
    )
    project_ids: Optional[list[int]] = Field(
        default=None,
        description="自定义项目ID列表(custom类型时使用)",
    )


class DataScopeUpdateIn(BaseModel):
    """按角色路径更新数据范围的请求体。"""

    scope_type: str = Field(
        ...,
        pattern="^(all|project_own|project_member|custom)$",
        description="范围类型",
    )
    project_ids: Optional[list[int]] = Field(
        default=None,
        description="自定义项目ID列表(custom类型时使用)",
    )


class DataScopeOut(BaseModel):
    """数据范围响应项

    Attributes:
        id: 数据范围ID
        role_id: 角色ID
        scope_type: 范围类型
        project_ids: 自定义项目ID列表
        create_time: 创建时间
    """

    id: int
    role_id: int
    scope_type: str
    project_ids: Optional[list[int]] = None
    create_time: datetime

    model_config = {"from_attributes": True}


class UserRoleAssignIn(BaseModel):
    """用户角色分配请求体

    Attributes:
        role_ids: 角色ID列表
    """

    role_ids: list[int] = Field(default_factory=list, description="角色ID列表")


# 解析 MenuOut 中的自引用前向引用(children: list["MenuOut"])
MenuOut.model_rebuild()
