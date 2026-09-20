"""backfill placeholder titles from older Agent Response runs

Revision ID: 056_older_run_titles
Revises: 055_backfill_conversation_titles

055 只读取每个会话最新一条 Responses run；若最新 run 只有助手输出、
较早 run 才有用户输入，历史标题仍会保留为占位值。本迁移按 run.id
倒序扫描同会话全部 run，取第一条可安全提取的用户文本，仅更新仍为
占位标题的会话。不会删除或归档任何会话、消息和运行账本。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from typing import Any, Mapping, Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "056_older_run_titles"
down_revision: Union[str, None] = "055_backfill_conversation_titles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATE_TABLE = "migration_056_conversation_title_state"
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


def upgrade() -> None:
    op.create_table(
        STATE_TABLE,
        sa.Column("conversation_id", sa.BigInteger(), primary_key=True),
        sa.Column("previous_title", sa.String(length=200), nullable=False),
    )

    context = op.get_context()
    if bool(getattr(context, "as_sql", False)):
        op.execute(sa.text(
            "-- 056 conversation data backfill requires online Alembic execution; "
            "offline SQL intentionally contains no fabricated data result"
        ))
        op.execute(sa.text(
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
            "'056 conversation data backfill requires online Alembic execution'"
        ))
        return

    connection = op.get_bind()
    placeholders = {str(value) for value in PLACEHOLDER_TITLES}
    conversations = [dict(row._mapping) for row in connection.execute(sa.text(
        "SELECT id,user_id,surface,session_key,title "
        "FROM agent_mesh_conversation ORDER BY id"
    ))]
    runs = [dict(row._mapping) for row in connection.execute(sa.text(
        "SELECT id,user_id,surface,session_key,checkpoint_json "
        "FROM agent_response_run ORDER BY id DESC"
    ))]

    runs_by_key: dict[tuple[int, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in runs:
        key = (int(row["user_id"]), str(row["surface"]), str(row["session_key"]))
        runs_by_key[key].append(row)

    for row in conversations:
        previous_title = str(row.get("title") or "").strip()
        if previous_title not in placeholders:
            continue
        key = (int(row["user_id"]), str(row["surface"]), str(row["session_key"]))
        title = next((
            candidate
            for run in runs_by_key.get(key, [])
            if (candidate := _title_from_checkpoint(run.get("checkpoint_json")))
        ), "")
        if not title:
            continue
        conversation_id = int(row["id"])
        connection.execute(sa.text(f"""
            INSERT INTO {STATE_TABLE} (conversation_id, previous_title)
            VALUES (:conversation_id, :previous_title)
        """), {
            "conversation_id": conversation_id,
            "previous_title": previous_title,
        })
        connection.execute(sa.text(
            "UPDATE agent_mesh_conversation SET title=:title WHERE id=:conversation_id"
        ), {"title": title, "conversation_id": conversation_id})


def downgrade() -> None:
    op.execute(sa.text(f"""
        UPDATE agent_mesh_conversation
        SET title = (
          SELECT state.previous_title FROM {STATE_TABLE} AS state
          WHERE state.conversation_id = agent_mesh_conversation.id
        )
        WHERE id IN (SELECT conversation_id FROM {STATE_TABLE})
    """))
    op.drop_table(STATE_TABLE)
