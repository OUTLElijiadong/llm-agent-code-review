"""backfill meaningful Agent conversation titles and archive duplicate blanks

Revision ID: 055_backfill_conversation_titles
Revises: 054_archive_excess_conversations

旧数据中部分有真实 Responses 内容的会话仍使用「新对话」等占位标题，
同一账号、同一入口还可能遗留多条无消息、无运行的空会话。本迁移：

1. 只从同账号、同会话的 Responses 检查点提取首条用户文本作为标题；
2. 同账号、同入口的真空白活动会话只保留最新一条，其余只归档；
3. 不删除会话、消息或 Responses 账本，所有变更记入标记表便于降级。

该迁移需读取检查点 JSON，因此数据回填只在在线迁移执行；
``alembic --sql`` 仍会生成 Schema 语句和明确注释，不伪造离线数据已回填。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "055_backfill_conversation_titles"
down_revision: Union[str, None] = "054_archive_excess_conversations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATE_TABLE = "migration_055_conversation_state"
PLACEHOLDER_TITLES = ("新对话", "默认对话", "用户端小菱对话", "贾维斯运维对话")


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_content_text(item) for item in value)
    if isinstance(value, Mapping):
        for key in ("text", "input_text", "content"):
            candidate = value.get(key)
            if candidate is not None:
                return _content_text(candidate)
    return ""


def _title_from_checkpoint(raw: Any) -> str:
    try:
        checkpoint = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(checkpoint, Mapping):
        return ""
    for item in checkpoint.get("transcript") or []:
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        raw_text = _content_text(item.get("content"))
        cleaned = "".join(
            " " if unicodedata.category(char).startswith("C") else char
            for char in raw_text
        )
        normalized = re.sub(r"\s+", " ", cleaned).strip()
        if normalized and normalized not in PLACEHOLDER_TITLES:
            return normalized[:32] + ("…" if len(normalized) > 32 else "")
    return ""


def _order_value(row: Mapping[str, Any]) -> tuple[str, int]:
    value = row.get("last_message_at") or row.get("last_seen_at")
    if isinstance(value, datetime):
        timestamp = value.isoformat()
    else:
        timestamp = str(value or "")
    return timestamp, int(row["id"])


def upgrade() -> None:
    op.create_table(
        STATE_TABLE,
        sa.Column("conversation_id", sa.BigInteger(), primary_key=True),
        sa.Column("previous_title", sa.String(length=200), nullable=False),
        sa.Column("previous_status", sa.String(length=24), nullable=False),
        sa.Column("title_changed", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("status_changed", sa.SmallInteger(), nullable=False, server_default="0"),
    )

    context = op.get_context()
    if bool(getattr(context, "as_sql", False)):
        op.execute(sa.text(
            "-- 055 conversation data backfill requires online Alembic execution; "
            "offline SQL intentionally contains no fabricated data result"
        ))
        op.execute(sa.text(
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
            "'055 conversation data backfill requires online Alembic execution'"
        ))
        return

    connection = op.get_bind()
    placeholders = {str(value) for value in PLACEHOLDER_TITLES}
    conversations = [dict(row._mapping) for row in connection.execute(sa.text(
        "SELECT id,user_id,surface,session_key,title,status,last_seen_at,last_message_at "
        "FROM agent_mesh_conversation ORDER BY id"
    ))]
    runs = [dict(row._mapping) for row in connection.execute(sa.text(
        "SELECT id,user_id,surface,session_key,checkpoint_json "
        "FROM agent_response_run ORDER BY id DESC"
    ))]
    messages = [dict(row._mapping) for row in connection.execute(sa.text(
        "SELECT user_id,sent_from,send_to FROM agent_mesh_message"
    ))]

    latest_run: dict[tuple[int, str, str], Mapping[str, Any]] = {}
    activity_keys: set[tuple[int, str, str]] = set()
    for row in runs:
        key = (int(row["user_id"]), str(row["surface"]), str(row["session_key"]))
        activity_keys.add(key)
        latest_run.setdefault(key, row)

    conversation_by_address = {
        (int(row["user_id"]), f"session:{row['surface']}:{row['session_key']}"):
            (int(row["user_id"]), str(row["surface"]), str(row["session_key"]))
        for row in conversations
    }
    for row in messages:
        user_id = int(row["user_id"])
        for address in (str(row.get("sent_from") or ""), str(row.get("send_to") or "")):
            key = conversation_by_address.get((user_id, address))
            if key is not None:
                activity_keys.add(key)

    title_updates: dict[int, str] = {}
    for row in conversations:
        if str(row.get("title") or "").strip() not in placeholders:
            continue
        key = (int(row["user_id"]), str(row["surface"]), str(row["session_key"]))
        run = latest_run.get(key)
        title = _title_from_checkpoint(run.get("checkpoint_json")) if run else ""
        if title:
            title_updates[int(row["id"])] = title

    empty_groups: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in conversations:
        conversation_id = int(row["id"])
        key = (int(row["user_id"]), str(row["surface"]), str(row["session_key"]))
        if (
            str(row.get("status") or "") == "active"
            and str(row.get("title") or "").strip() in placeholders
            and conversation_id not in title_updates
            and key not in activity_keys
        ):
            empty_groups[(key[0], key[1])].append(row)

    status_updates: set[int] = set()
    for rows in empty_groups.values():
        if len(rows) <= 1:
            continue
        keep_id = int(max(rows, key=_order_value)["id"])
        status_updates.update(int(row["id"]) for row in rows if int(row["id"]) != keep_id)

    by_id = {int(row["id"]): row for row in conversations}
    changed_ids = sorted(set(title_updates) | status_updates)
    for conversation_id in changed_ids:
        row = by_id[conversation_id]
        connection.execute(sa.text(f"""
            INSERT INTO {STATE_TABLE}
              (conversation_id, previous_title, previous_status, title_changed, status_changed)
            VALUES
              (:conversation_id, :previous_title, :previous_status, :title_changed, :status_changed)
        """), {
            "conversation_id": conversation_id,
            "previous_title": str(row.get("title") or ""),
            "previous_status": str(row.get("status") or ""),
            "title_changed": int(conversation_id in title_updates),
            "status_changed": int(conversation_id in status_updates),
        })
        if conversation_id in title_updates:
            connection.execute(sa.text(
                "UPDATE agent_mesh_conversation SET title=:title WHERE id=:conversation_id"
            ), {"title": title_updates[conversation_id], "conversation_id": conversation_id})
        if conversation_id in status_updates:
            connection.execute(sa.text("""
                UPDATE agent_mesh_conversation
                SET status='archived', active_run_id=NULL, active_run_status=NULL
                WHERE id=:conversation_id
            """), {"conversation_id": conversation_id})


def downgrade() -> None:
    op.execute(sa.text(f"""
        UPDATE agent_mesh_conversation
        SET title = (
              SELECT state.previous_title FROM {STATE_TABLE} AS state
              WHERE state.conversation_id = agent_mesh_conversation.id
            ),
            status = (
              SELECT state.previous_status FROM {STATE_TABLE} AS state
              WHERE state.conversation_id = agent_mesh_conversation.id
            )
        WHERE id IN (SELECT conversation_id FROM {STATE_TABLE})
    """))
    op.drop_table(STATE_TABLE)
