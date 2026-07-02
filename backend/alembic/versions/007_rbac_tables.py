"""add rbac tables (role/permission/role_permission/user_role/menu/data_scope)

Revision ID: 007
Revises: 006
Create Date: 2026-06-25

本次迁移为 RBAC 权限体系提供数据库支撑:
1. 创建 6 张表: role, permission, role_permission, user_role, menu, data_scope
2. 预置 5 个角色(user/reviewer/auditor/admin/super_admin)
3. 预置 42 个权限点(覆盖 project/file/review/issue/rule/report/agent/security/user/audit 模块)
4. 预置角色-权限关联矩阵(按角色层级递进)
5. 预置默认菜单树(含系统管理子菜单,自引用树形结构)
6. 预置数据范围规则(project_own/project_member/all)
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    """创建兼容 MySQL/SQLite 的自增主键列。

    Returns:
        sa.Column: id 主键列。
    """
    return sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """创建通用创建/更新时间列。

    Returns:
        tuple[sa.Column, sa.Column]: create_time 与 update_time 两列。
    """
    return (
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


# === 预置权限点定义 (按模块分组,顺序即 ID 顺序,从 1 开始) ===
# 元组格式: (code, name, module, description)
_PERMISSIONS: list[tuple[str, str, str, str]] = [
    # project 模块 (6)
    ("project:create", "创建项目", "project", "创建新项目"),
    ("project:view", "查看项目", "project", "查看项目列表与详情"),
    ("project:update", "更新项目", "project", "更新项目信息"),
    ("project:delete", "删除项目", "project", "删除项目"),
    ("project:import", "导入项目", "project", "导入外部项目"),
    ("project:member:manage", "管理项目成员", "project", "管理项目成员关系"),
    # file 模块 (5)
    ("file:upload", "上传文件", "file", "上传代码文件"),
    ("file:view", "查看文件", "file", "查看代码文件内容"),
    ("file:edit", "编辑文件", "file", "编辑代码文件"),
    ("file:delete", "删除文件", "file", "删除代码文件"),
    ("file:download", "下载文件", "file", "下载代码文件"),
    # review 模块 (5)
    ("review:start", "启动审查", "review", "启动代码审查任务"),
    ("review:view", "查看审查", "review", "查看审查任务与结果"),
    ("review:approve", "审批审查", "review", "审批审查任务结果"),
    ("review:cancel", "取消审查", "review", "取消审查任务"),
    ("review:rerun", "重新审查", "review", "重新执行审查任务"),
    # issue 模块 (4)
    ("issue:view", "查看问题", "issue", "查看审查问题"),
    ("issue:handle", "处理问题", "issue", "处理/修复审查问题"),
    ("issue:batch", "批量处理", "issue", "批量处理审查问题"),
    ("issue:export", "导出问题", "issue", "导出审查问题"),
    # rule 模块 (4)
    ("rule:view", "查看规则", "rule", "查看审查规则"),
    ("rule:create", "创建规则", "rule", "创建审查规则"),
    ("rule:update", "更新规则", "rule", "更新审查规则"),
    ("rule:delete", "删除规则", "rule", "删除审查规则"),
    # report 模块 (5)
    ("report:view", "查看报告", "report", "查看审查报告"),
    ("report:export:pdf", "导出PDF", "report", "导出 PDF 格式报告"),
    ("report:export:word", "导出Word", "report", "导出 Word 格式报告"),
    ("report:export:json", "导出JSON", "report", "导出 JSON 格式报告"),
    ("report:export:html", "导出HTML", "report", "导出 HTML 格式报告"),
    # agent 模块 (3)
    ("agent:view", "查看Agent", "agent", "查看 Agent 信息"),
    ("agent:chat", "Agent对话", "agent", "与 Agent 进行对话"),
    ("agent:configure", "配置Agent", "agent", "配置 Agent 参数"),
    # security 模块 (2)
    ("security:scan", "安全扫描", "security", "执行安全扫描"),
    ("security:view", "查看扫描", "security", "查看安全扫描结果"),
    # user 模块 (6)
    ("user:view", "查看用户", "user", "查看用户列表"),
    ("user:create", "创建用户", "user", "创建新用户"),
    ("user:update", "更新用户", "user", "更新用户信息"),
    ("user:delete", "删除用户", "user", "删除用户"),
    ("role:manage", "管理角色", "user", "管理角色与权限分配"),
    ("menu:manage", "管理菜单", "user", "管理菜单配置"),
    # audit 模块 (2)
    ("audit:view", "查看审计", "audit", "查看操作审计日志"),
    ("ai_log:view", "查看AI日志", "audit", "查看 AI 调用日志"),
]


def _build_permission_rows() -> list[dict]:
    """构建权限点预置数据行。

    Returns:
        list[dict]: 权限点字典列表,每个含 code/name/module/type/description。
    """
    return [
        {
            "code": code,
            "name": name,
            "module": module,
            "type": "api",
            "description": desc,
        }
        for code, name, module, desc in _PERMISSIONS
    ]


def _build_role_permission_rows() -> list[dict]:
    """构建角色-权限关联预置数据行。

    角色层级递进:
    - user(1): 基础项目/文件/审查查看/问题导出等
    - reviewer(2): user 基础 + 启动审查/重新审查/问题处理/批量处理/安全扫描
    - auditor(3): 独立只读权限集 + 审计日志
    - admin(4): reviewer 基础 + 审批/规则管理/Agent配置/用户管理
    - super_admin(5): 全部权限点

    Returns:
        list[dict]: 角色权限关联字典列表,每个含 role_id/permission_id。
    """
    # 权限编码到 ID 的映射(ID 从 1 开始)
    code_to_id = {code: idx + 1 for idx, (code, *_rest) in enumerate(_PERMISSIONS)}

    # user (role_id=1): 基础权限集
    user_codes = {
        "project:create", "project:view", "project:update", "project:delete",
        "project:member:manage",
        "file:upload", "file:view", "file:edit", "file:delete", "file:download",
        "review:view", "review:cancel", "review:start", "review:rerun",
        "issue:view", "issue:export",
        "rule:view",
        "report:view", "report:export:pdf", "report:export:word",
        "report:export:json", "report:export:html",
        "agent:view", "agent:chat",
        "security:view",
    }
    # reviewer (role_id=2): user 基础 + 审查启动/重跑/问题处理/批量/安全扫描
    reviewer_codes = user_codes | {
        "review:start", "review:rerun", "issue:handle", "issue:batch", "security:scan",
    }
    # auditor (role_id=3): 独立只读权限集 + 审计日志
    auditor_codes = {
        "project:view", "file:view", "file:download",
        "review:view", "issue:view", "issue:export",
        "rule:view", "report:view",
        "report:export:pdf", "report:export:word", "report:export:json", "report:export:html",
        "agent:view", "security:view", "audit:view",
    }
    # admin (role_id=4): reviewer 基础 + 审批/规则管理/Agent配置/用户管理
    admin_codes = reviewer_codes | {
        "review:approve", "rule:create", "rule:update", "rule:delete",
        "agent:configure", "user:view", "user:update", "ai_log:view",
    }
    # super_admin (role_id=5): 全部权限点
    super_admin_codes = {code for code, *_rest in _PERMISSIONS}

    role_perm_map = {
        1: user_codes,
        2: reviewer_codes,
        3: auditor_codes,
        4: admin_codes,
        5: super_admin_codes,
    }

    rows = []
    for role_id, codes in role_perm_map.items():
        for code in codes:
            rows.append({"role_id": role_id, "permission_id": code_to_id[code]})
    return rows


def _build_menu_rows() -> list[dict]:
    """构建菜单预置数据行(自引用树形结构,父菜单先于子菜单插入)。

    菜单树:
    - 代码审查 (sort=10)
      ├ 启动审查 (sort=11)
      └ 任务列表 (sort=12)
    - 代码文件 (sort=20)
    - 问题管理 (sort=30)
    - 安全扫描 (sort=40)
    - 报告 (sort=50)
    - 规则管理 (sort=60)
    - Agent 中心 (sort=70)
    - 系统管理 (sort=80)
      ├ 用户管理 (sort=81)
      ├ 角色管理 (sort=82)
      ├ 菜单管理 (sort=83)
      ├ 操作审计 (sort=84)
      ├ AI 调用日志 (sort=85)
      ├ 报告模板 (sort=86)
      └ 扫描记录 (sort=87)

    Returns:
        list[dict]: 菜单字典列表,每个含 parent_id/name/path/component/icon/sort/permission_code/visible/is_builtin。
    """
    return [
        # 顶级菜单: 代码审查 (id=1)
        {"parent_id": None, "name": "代码审查", "path": "/review", "component": None,
         "icon": None, "sort": 10, "permission_code": "review:view", "visible": 1, "is_builtin": 1},
        # 子菜单: 启动审查 (id=2, parent=1)
        {"parent_id": 1, "name": "启动审查", "path": "/review/start", "component": None,
         "icon": None, "sort": 11, "permission_code": "review:start", "visible": 1, "is_builtin": 1},
        # 子菜单: 任务列表 (id=3, parent=1)
        {"parent_id": 1, "name": "任务列表", "path": "/review/tasks", "component": None,
         "icon": None, "sort": 12, "permission_code": "review:view", "visible": 1, "is_builtin": 1},
        # 顶级菜单: 代码文件 (id=4)
        {"parent_id": None, "name": "代码文件", "path": "/code", "component": None,
         "icon": None, "sort": 20, "permission_code": "file:view", "visible": 1, "is_builtin": 1},
        # 顶级菜单: 问题管理 (id=5)
        {"parent_id": None, "name": "问题管理", "path": "/issues", "component": None,
         "icon": None, "sort": 30, "permission_code": "issue:view", "visible": 1, "is_builtin": 1},
        # 顶级菜单: 安全扫描 (id=6)
        {"parent_id": None, "name": "安全扫描", "path": "/security", "component": None,
         "icon": None, "sort": 40, "permission_code": "security:view", "visible": 1, "is_builtin": 1},
        # 顶级菜单: 报告 (id=7)
        {"parent_id": None, "name": "报告", "path": "/reports", "component": None,
         "icon": None, "sort": 50, "permission_code": "report:view", "visible": 1, "is_builtin": 1},
        # 顶级菜单: 规则管理 (id=8)
        {"parent_id": None, "name": "规则管理", "path": "/rules", "component": None,
         "icon": None, "sort": 60, "permission_code": "rule:view", "visible": 1, "is_builtin": 1},
        # 顶级菜单: Agent 中心 (id=9)
        {"parent_id": None, "name": "Agent 中心", "path": "/agent", "component": None,
         "icon": None, "sort": 70, "permission_code": "agent:view", "visible": 1, "is_builtin": 1},
        # 顶级菜单: 系统管理 (id=10)
        {"parent_id": None, "name": "系统管理", "path": "/admin", "component": None,
         "icon": None, "sort": 80, "permission_code": "user:view", "visible": 1, "is_builtin": 1},
        # 系统管理子菜单: 用户管理 (id=11, parent=10)
        {"parent_id": 10, "name": "用户管理", "path": "/admin/users", "component": None,
         "icon": None, "sort": 81, "permission_code": "user:view", "visible": 1, "is_builtin": 1},
        # 系统管理子菜单: 角色管理 (id=12, parent=10)
        {"parent_id": 10, "name": "角色管理", "path": "/admin/roles", "component": None,
         "icon": None, "sort": 82, "permission_code": "role:manage", "visible": 1, "is_builtin": 1},
        # 系统管理子菜单: 菜单管理 (id=13, parent=10)
        {"parent_id": 10, "name": "菜单管理", "path": "/admin/menus", "component": None,
         "icon": None, "sort": 83, "permission_code": "menu:manage", "visible": 1, "is_builtin": 1},
        # 系统管理子菜单: 操作审计 (id=14, parent=10)
        {"parent_id": 10, "name": "操作审计", "path": "/admin/audit", "component": None,
         "icon": None, "sort": 84, "permission_code": "audit:view", "visible": 1, "is_builtin": 1},
        # 系统管理子菜单: AI 调用日志 (id=15, parent=10)
        {"parent_id": 10, "name": "AI 调用日志", "path": "/admin/ai-logs", "component": None,
         "icon": None, "sort": 85, "permission_code": "ai_log:view", "visible": 1, "is_builtin": 1},
        # 系统管理子菜单: 报告模板 (id=16, parent=10)
        {"parent_id": 10, "name": "报告模板", "path": "/admin/report-templates", "component": None,
         "icon": None, "sort": 86, "permission_code": "report:view", "visible": 1, "is_builtin": 1},
        # 系统管理子菜单: 扫描记录 (id=17, parent=10)
        {"parent_id": 10, "name": "扫描记录", "path": "/admin/malware-scans", "component": None,
         "icon": None, "sort": 87, "permission_code": "audit:view", "visible": 1, "is_builtin": 1},
    ]


def upgrade() -> None:
    """升级:创建 RBAC 6 张表并预置角色/权限/菜单/数据范围数据。

    Steps:
        1. 创建 role 表(含 code 唯一索引、status 索引)
        2. 创建 permission 表(含 code 唯一索引、module 索引)
        3. 创建 role_permission 关联表(含 role_id+permission_id 唯一约束)
        4. 创建 user_role 关联表(含 user_id+role_id 唯一约束)
        5. 创建 menu 表(含 parent_id 索引,自引用树)
        6. 创建 data_scope 表(含 role_id 索引)
        7. 预置 5 个角色
        8. 预置 42 个权限点
        9. 预置角色-权限关联矩阵
        10. 预置 17 个菜单(树形结构)
        11. 预置 5 条数据范围规则
    """
    # === 1. 创建 role 表 ===
    op.create_table(
        "role",
        _id_column(),
        sa.Column("name", sa.String(64), nullable=False, comment="角色名称"),
        sa.Column("code", sa.String(64), nullable=False, comment="角色编码"),
        sa.Column("description", sa.String(255), nullable=True, comment="角色描述"),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
            comment="状态: active/disabled",
        ),
        sa.Column(
            "sort",
            sa.Integer(),
            nullable=False,
            server_default="100",
            comment="排序值",
        ),
        sa.Column(
            "is_builtin",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="是否预置角色: 1=是,0=否",
        ),
        *_timestamps(),
    )
    op.create_index("ix_role_status", "role", ["status"])
    op.create_unique_constraint("uk_role_code", "role", ["code"])

    # === 2. 创建 permission 表 ===
    op.create_table(
        "permission",
        _id_column(),
        sa.Column("code", sa.String(64), nullable=False, comment="权限编码"),
        sa.Column("name", sa.String(128), nullable=False, comment="权限名称"),
        sa.Column("module", sa.String(32), nullable=False, comment="所属模块"),
        sa.Column(
            "type",
            sa.String(16),
            nullable=False,
            server_default="api",
            comment="权限类型: api/menu/button",
        ),
        sa.Column("description", sa.String(255), nullable=True, comment="权限描述"),
        *_timestamps(),
    )
    op.create_index("ix_permission_module", "permission", ["module"])
    op.create_unique_constraint("uk_permission_code", "permission", ["code"])

    # === 3. 创建 role_permission 关联表 ===
    op.create_table(
        "role_permission",
        _id_column(),
        sa.Column("role_id", sa.BigInteger(), nullable=False, comment="角色ID"),
        sa.Column("permission_id", sa.BigInteger(), nullable=False, comment="权限ID"),
        *_timestamps(),
    )
    op.create_index("ix_role_permission_role", "role_permission", ["role_id"])
    op.create_index("ix_role_permission_perm", "role_permission", ["permission_id"])
    op.create_unique_constraint("uk_role_permission", "role_permission", ["role_id", "permission_id"])

    # === 4. 创建 user_role 关联表 ===
    op.create_table(
        "user_role",
        _id_column(),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户ID"),
        sa.Column("role_id", sa.BigInteger(), nullable=False, comment="角色ID"),
        *_timestamps(),
    )
    op.create_index("ix_user_role_user", "user_role", ["user_id"])
    op.create_index("ix_user_role_role", "user_role", ["role_id"])
    op.create_unique_constraint("uk_user_role", "user_role", ["user_id", "role_id"])

    # === 5. 创建 menu 表(自引用树) ===
    op.create_table(
        "menu",
        _id_column(),
        sa.Column("parent_id", sa.BigInteger(), nullable=True, comment="父菜单ID,顶级为NULL"),
        sa.Column("name", sa.String(64), nullable=False, comment="菜单名称"),
        sa.Column("path", sa.String(255), nullable=True, comment="前端路由路径"),
        sa.Column("component", sa.String(255), nullable=True, comment="前端组件路径"),
        sa.Column("icon", sa.String(64), nullable=True, comment="菜单图标"),
        sa.Column(
            "sort",
            sa.Integer(),
            nullable=False,
            server_default="100",
            comment="排序值",
        ),
        sa.Column("permission_code", sa.String(64), nullable=True, comment="关联权限编码"),
        sa.Column(
            "visible",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="是否可见: 1=可见,0=隐藏",
        ),
        sa.Column(
            "is_builtin",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="是否预置菜单: 1=是,0=否",
        ),
        *_timestamps(),
    )
    op.create_index("ix_menu_parent", "menu", ["parent_id"])

    # === 6. 创建 data_scope 表 ===
    op.create_table(
        "data_scope",
        _id_column(),
        sa.Column("role_id", sa.BigInteger(), nullable=False, comment="角色ID"),
        sa.Column(
            "scope_type",
            sa.String(32),
            nullable=False,
            comment="范围类型: all/project_own/project_member/custom",
        ),
        sa.Column("project_ids", sa.JSON(), nullable=True, comment="自定义项目ID列表(custom类型时使用)"),
        *_timestamps(),
    )
    op.create_index("ix_data_scope_role", "data_scope", ["role_id"])

    # === 7. 预置 5 个角色 ===
    role_table = sa.table(
        "role",
        sa.column("name", sa.String),
        sa.column("code", sa.String),
        sa.column("description", sa.String),
        sa.column("status", sa.String),
        sa.column("sort", sa.Integer),
        sa.column("is_builtin", sa.Integer),
    )
    op.bulk_insert(
        role_table,
        [
            {"name": "普通用户", "code": "user", "description": "基础用户,可管理自己的项目",
             "status": "active", "sort": 100, "is_builtin": 1},
            {"name": "评审员", "code": "reviewer", "description": "审查员,可启动审查并处理问题",
             "status": "active", "sort": 200, "is_builtin": 1},
            {"name": "审计员", "code": "auditor", "description": "审计员,只读访问全部数据与审计日志",
             "status": "active", "sort": 300, "is_builtin": 1},
            {"name": "管理员", "code": "admin", "description": "管理员,可审批/管理规则与用户",
             "status": "active", "sort": 400, "is_builtin": 1},
            {"name": "超级管理员", "code": "super_admin", "description": "超级管理员,拥有全部权限",
             "status": "active", "sort": 500, "is_builtin": 1},
        ],
    )

    # === 8. 预置 42 个权限点 ===
    permission_table = sa.table(
        "permission",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("module", sa.String),
        sa.column("type", sa.String),
        sa.column("description", sa.String),
    )
    op.bulk_insert(permission_table, _build_permission_rows())

    # === 9. 预置角色-权限关联矩阵 ===
    role_permission_table = sa.table(
        "role_permission",
        sa.column("role_id", sa.BigInteger),
        sa.column("permission_id", sa.BigInteger),
    )
    op.bulk_insert(role_permission_table, _build_role_permission_rows())

    # === 10. 预置 17 个菜单(树形结构) ===
    menu_table = sa.table(
        "menu",
        sa.column("parent_id", sa.BigInteger),
        sa.column("name", sa.String),
        sa.column("path", sa.String),
        sa.column("component", sa.String),
        sa.column("icon", sa.String),
        sa.column("sort", sa.Integer),
        sa.column("permission_code", sa.String),
        sa.column("visible", sa.Integer),
        sa.column("is_builtin", sa.Integer),
    )
    op.bulk_insert(menu_table, _build_menu_rows())

    # === 11. 预置 5 条数据范围规则 ===
    data_scope_table = sa.table(
        "data_scope",
        sa.column("role_id", sa.BigInteger),
        sa.column("scope_type", sa.String),
        sa.column("project_ids", sa.JSON),
    )
    op.bulk_insert(
        data_scope_table,
        [
            {"role_id": 1, "scope_type": "project_own", "project_ids": None},
            {"role_id": 2, "scope_type": "project_member", "project_ids": None},
            {"role_id": 3, "scope_type": "all", "project_ids": None},
            {"role_id": 4, "scope_type": "all", "project_ids": None},
            {"role_id": 5, "scope_type": "all", "project_ids": None},
        ],
    )


def downgrade() -> None:
    """回滚:按反序删除 RBAC 6 张表。

    删除顺序(反序): data_scope → menu → user_role → role_permission → permission → role
    """
    for table in (
        "data_scope",
        "menu",
        "user_role",
        "role_permission",
        "permission",
        "role",
    ):
        op.drop_table(table)
