"""add project_member table

Revision ID: 004
Revises: 003
Create Date: 2026-06-25

新增 project_member 表,建立项目-成员关系,支持按项目成员关系做数据隔离。
数据回填:现有项目的 owner(Project.user_id)自动写入 project_member(role_in_project='owner')。
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    """创建兼容 MySQL/SQLite 的自增主键列。

    Returns:
        sa.Column: id 主键列。
    """
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """创建通用创建/更新时间列。

    Returns:
        tuple[sa.Column, sa.Column]: create_time 与 update_time 两列。
    """
    return (
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    """创建 project_member 表并回填现有项目 owner 数据。"""
    op.create_table(
        "project_member",
        _id_column(),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户ID"),
        sa.Column(
            "role_in_project",
            sa.String(20),
            nullable=False,
            server_default="reviewer",
            comment="项目内角色: owner/reviewer",
        ),
        *_timestamps(),
    )
    op.create_unique_constraint("uk_project_user", "project_member", ["project_id", "user_id"])
    op.create_index("ix_pm_user", "project_member", ["user_id"])
    op.create_index("ix_pm_project", "project_member", ["project_id"])

    # 数据回填:把现有项目的 owner(Project.user_id)写入 project_member
    op.execute(
        """
        INSERT INTO project_member (project_id, user_id, role_in_project, create_time, update_time)
        SELECT id, user_id, 'owner', NOW(), NOW()
        FROM project
        WHERE status != 'deleted'
          AND NOT EXISTS (
              SELECT 1 FROM project_member pm
              WHERE pm.project_id = project.id AND pm.user_id = project.user_id
          )
        """
    )


def downgrade() -> None:
    """删除 project_member 表。"""
    op.drop_index("ix_pm_project", table_name="project_member")
    op.drop_index("ix_pm_user", table_name="project_member")
    op.drop_constraint("uk_project_user", "project_member", type_="unique")
    op.drop_table("project_member")
