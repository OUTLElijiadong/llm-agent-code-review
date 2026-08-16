"""Add hot-path composite indexes for sandbox, mesh, team, and Responses tables.

Revision ID: 036
Revises: 035

MySQL 与 SQLite 均支持；升级前检查索引是否已存在，已存在则跳过，保证迁移幂等。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index_name, table_name, columns)。agent_mesh_message 的目标地址持久化列为
# send_to，因此 036 不新增列，直接在既有目标地址列上建组合索引。
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_sandbox_env_status_update", "sandbox_environment", ("status", "update_time")),
    ("ix_sandbox_env_project_purpose_status", "sandbox_environment", ("project_id", "purpose", "status")),
    ("ix_mesh_msg_owner_target_status", "agent_mesh_message", ("user_id", "send_to", "status")),
    ("ix_team_task_team_status", "agent_team_task", ("team_id", "status")),
)


def _index_exists(conn, table: str, index_name: str) -> bool:
    """按方言检查索引是否已存在，避免重复 CREATE INDEX。"""
    dialect_name = getattr(getattr(conn, "dialect", None), "name", "")
    if dialect_name == "mysql":
        rows = conn.execute(
            sa.text(f"SHOW INDEX FROM `{table}` WHERE `Key_name` = :index_name"),
            {"index_name": index_name},
        )
        return rows.first() is not None
    rows = conn.execute(sa.text(f'PRAGMA index_list("{table}")'))
    return any(row[1] == index_name for row in rows)


def upgrade() -> None:
    if op.get_context().as_sql:
        for index_name, table_name, columns in _INDEXES:
            op.create_index(index_name, table_name, list(columns))
        return

    conn = op.get_bind()
    for index_name, table_name, columns in _INDEXES:
        if not _index_exists(conn, table_name, index_name):
            op.create_index(index_name, table_name, list(columns))


def downgrade() -> None:
    if op.get_context().as_sql:
        for index_name, table_name, _columns in reversed(_INDEXES):
            op.drop_index(index_name, table_name=table_name)
        return

    conn = op.get_bind()
    for index_name, table_name, _columns in reversed(_INDEXES):
        if _index_exists(conn, table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
