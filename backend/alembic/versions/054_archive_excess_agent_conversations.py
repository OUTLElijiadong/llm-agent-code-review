"""archive legacy active Agent conversations above the account limit

Revision ID: 054_archive_excess_conversations
Revises: 053_role_pentest_authorization

旧版本没有账号级活动会话上限，升级后只靠新建路径收敛会遗留大量
active 索引。本迁移保留运行中/待人工操作会话，再按最近消息补足到 10 条；
其余仅归档索引，不删除 Responses 或消息账本。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "054_archive_excess_conversations"
down_revision: Union[str, None] = "053_role_pentest_authorization"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ARCHIVE_STATE_TABLE = "migration_054_archived_conversation"
RETENTION_LIMIT = 10
OCCUPIED_RUN_STATUSES = (
    "running",
    "approving",
    "rejecting",
    "answering",
    "waiting_approval",
    "waiting_input",
)


def _quoted_statuses() -> str:
    return ",".join(f"'{status}'" for status in OCCUPIED_RUN_STATUSES)


def upgrade() -> None:
    op.create_table(
        ARCHIVE_STATE_TABLE,
        sa.Column("conversation_id", sa.BigInteger(), primary_key=True),
    )

    statuses = _quoted_statuses()
    # 候选会话只有在「受保护会话数 + 比它更新的空闲会话数」已达上限时才归档。
    # 这个相关子查询同时兼容 MySQL 8 和 SQLite，也可用于 Alembic --sql 输出。
    op.execute(sa.text(f"""
        INSERT INTO {ARCHIVE_STATE_TABLE} (conversation_id)
        SELECT candidate.id
        FROM agent_mesh_conversation AS candidate
        WHERE candidate.status = 'active'
          AND NOT EXISTS (
            SELECT 1
            FROM agent_response_run AS candidate_run
            WHERE candidate_run.user_id = candidate.user_id
              AND candidate_run.surface = candidate.surface
              AND candidate_run.session_key = candidate.session_key
              AND candidate_run.status IN ({statuses})
          )
          AND (
            (
              SELECT COUNT(*)
              FROM agent_mesh_conversation AS protected
              WHERE protected.user_id = candidate.user_id
                AND protected.status = 'active'
                AND EXISTS (
                  SELECT 1
                  FROM agent_response_run AS protected_run
                  WHERE protected_run.user_id = protected.user_id
                    AND protected_run.surface = protected.surface
                    AND protected_run.session_key = protected.session_key
                    AND protected_run.status IN ({statuses})
                )
            )
            +
            (
              SELECT COUNT(*)
              FROM agent_mesh_conversation AS newer
              WHERE newer.user_id = candidate.user_id
                AND newer.status = 'active'
                AND NOT EXISTS (
                  SELECT 1
                  FROM agent_response_run AS newer_run
                  WHERE newer_run.user_id = newer.user_id
                    AND newer_run.surface = newer.surface
                    AND newer_run.session_key = newer.session_key
                    AND newer_run.status IN ({statuses})
                )
                AND (
                  COALESCE(newer.last_message_at, newer.last_seen_at)
                    > COALESCE(candidate.last_message_at, candidate.last_seen_at)
                  OR (
                    COALESCE(newer.last_message_at, newer.last_seen_at)
                      = COALESCE(candidate.last_message_at, candidate.last_seen_at)
                    AND newer.id > candidate.id
                  )
                )
            )
          ) >= {RETENTION_LIMIT}
    """))
    op.execute(sa.text(f"""
        UPDATE agent_mesh_conversation
        SET status = 'archived', active_run_id = NULL, active_run_status = NULL
        WHERE id IN (SELECT conversation_id FROM {ARCHIVE_STATE_TABLE})
    """))


def downgrade() -> None:
    op.execute(sa.text(f"""
        UPDATE agent_mesh_conversation
        SET status = 'active'
        WHERE id IN (SELECT conversation_id FROM {ARCHIVE_STATE_TABLE})
    """))
    op.drop_table(ARCHIVE_STATE_TABLE)
