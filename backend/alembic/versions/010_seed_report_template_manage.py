"""seed report:template_manage permission + fix report-template menu path

Revision ID: 010
Revises: 009
Create Date: 2026-07-28

本次迁移修复 007 种子在存量库上的两处契约漂移(就地改 007 对已应用库无效,需数据迁移):

1. 补种子权限点 report:template_manage:
   前端路由 /report/templates 的 meta.permissions 与后端 reports.py 四个模板 CRUD
   端点均 require_permission('report:template_manage'),但 007 的 _PERMISSIONS 从未
   包含该权限点 → permission 表无此行,RBAC 管理界面无法授予任何角色,模板管理
   只能依赖 admin/super_admin 的角色级绕过。此处补插该行,并绑定 admin/super_admin
   角色(幂等:已存在则跳过)。

2. 修正"报告模板"菜单路径:
   007 预置 menu id=16 path 为 /admin/report-templates,但前端真实路由是
   /report/templates。前端当前虽硬编码导航未消费 menu 表,但一旦启用菜单驱动即
   命中 NotFound。此处按 name='报告模板' 校正存量行。

兼容 SQLite(测试)与 MySQL(生产):统一用 sa.text 参数化 SQL,幂等可重入。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PERM_CODE = "report:template_manage"
_PERM_NAME = "管理报告模板"
_ADMIN_ROLE_CODES = ("admin", "super_admin")


def upgrade() -> None:
    """升级:补 report:template_manage 权限点并绑定管理角色,修正菜单路径。"""
    conn = op.get_bind()

    # === 1. 补种子权限点(幂等) ===
    perm_id = conn.execute(
        sa.text("SELECT id FROM permission WHERE code = :code"), {"code": _PERM_CODE}
    ).scalar()
    if perm_id is None:
        conn.execute(
            sa.text(
                "INSERT INTO permission (code, name, module, type, description, create_time, update_time)"
                " VALUES (:code, :name, 'report', 'api', :desc, NOW(), NOW())"
                if conn.dialect.name != "sqlite"
                else
                "INSERT INTO permission (code, name, module, type, description, create_time, update_time)"
                " VALUES (:code, :name, 'report', 'api', :desc, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"code": _PERM_CODE, "name": _PERM_NAME, "desc": "管理报告模板"},
        )
        perm_id = conn.execute(
            sa.text("SELECT id FROM permission WHERE code = :code"), {"code": _PERM_CODE}
        ).scalar()

    # === 2. 绑定 admin / super_admin 角色(幂等) ===
    if perm_id is not None:
        for role_code in _ADMIN_ROLE_CODES:
            role_id = conn.execute(
                sa.text("SELECT id FROM role WHERE code = :code"), {"code": role_code}
            ).scalar()
            if role_id is None:
                continue
            exists = conn.execute(
                sa.text(
                    "SELECT 1 FROM role_permission WHERE role_id = :rid AND permission_id = :pid"
                ),
                {"rid": role_id, "pid": perm_id},
            ).scalar()
            if not exists:
                conn.execute(
                    sa.text(
                        "INSERT INTO role_permission (role_id, permission_id) VALUES (:rid, :pid)"
                    ),
                    {"rid": role_id, "pid": perm_id},
                )

    # === 3. 修正报告模板菜单路径 ===
    conn.execute(
        sa.text("UPDATE menu SET path = '/report/templates' WHERE name = '报告模板'"),
    )


def downgrade() -> None:
    """回滚:移除 report:template_manage 绑定与权限行,恢复菜单旧路径。"""
    conn = op.get_bind()
    perm_id = conn.execute(
        sa.text("SELECT id FROM permission WHERE code = :code"), {"code": _PERM_CODE}
    ).scalar()
    if perm_id is not None:
        conn.execute(
            sa.text("DELETE FROM role_permission WHERE permission_id = :pid"), {"pid": perm_id}
        )
        conn.execute(sa.text("DELETE FROM permission WHERE id = :pid"), {"pid": perm_id})
    conn.execute(
        sa.text("UPDATE menu SET path = '/admin/report-templates' WHERE name = '报告模板'"),
    )
