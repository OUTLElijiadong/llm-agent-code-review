"""055 迁移：标题回填、重复空会话归档与降级验证。"""

import json
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, text


def _migration():
    path = Path(__file__).parents[2] / "alembic/versions/055_backfill_agent_conversation_titles.py"
    spec = spec_from_file_location("migration_055", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_backfills_real_titles_and_archives_only_duplicate_empty_sessions() -> None:
    migration = _migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    conversation = Table(
        "agent_mesh_conversation",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("surface", String(24), nullable=False),
        Column("session_key", String(128), nullable=False),
        Column("title", String(200), nullable=False),
        Column("status", String(24), nullable=False),
        Column("active_run_id", String(80)),
        Column("active_run_status", String(32)),
        Column("last_seen_at", DateTime, nullable=False),
        Column("last_message_at", DateTime),
    )
    response_run = Table(
        "agent_response_run",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("surface", String(24), nullable=False),
        Column("session_key", String(128), nullable=False),
        Column("checkpoint_json", String, nullable=False),
    )
    message = Table(
        "agent_mesh_message",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("sent_from", String(200), nullable=False),
        Column("send_to", String(200), nullable=False),
    )
    metadata.create_all(engine)
    base = datetime(2026, 9, 20, 1, 0, 0)
    rows = [
        (1, 1, "user", "with-text", "新对话", "active", base, base),
        (2, 1, "user", "empty-old", "新对话", "active", base, None),
        (3, 1, "user", "empty-new", "默认对话", "active", base + timedelta(minutes=2), None),
        (4, 1, "admin", "admin-empty", "贾维斯运维对话", "active", base, None),
        (5, 1, "user", "mesh-only", "新对话", "active", base, None),
        (6, 1, "user", "named", "已有标题", "active", base, None),
        (7, 2, "user", "archived-text", "用户端小菱对话", "archived", base, base),
        (8, 2, "user", "empty-only", "新对话", "active", base, None),
    ]

    with engine.begin() as connection:
        connection.execute(conversation.insert(), [
            {
                "id": row[0],
                "user_id": row[1],
                "surface": row[2],
                "session_key": row[3],
                "title": row[4],
                "status": row[5],
                "active_run_id": None,
                "active_run_status": None,
                "last_seen_at": row[6],
                "last_message_at": row[7],
            }
            for row in rows
        ])
        connection.execute(response_run.insert(), [
            {
                "id": 1,
                "user_id": 1,
                "surface": "user",
                "session_key": "with-text",
                "checkpoint_json": json.dumps({
                    "transcript": [
                        {"role": "assistant", "content": "请问需要什么"},
                        {"role": "user", "content": "  帮我\n检查\u0000 API 授权问题  "},
                    ]
                }),
            },
            {
                "id": 2,
                "user_id": 2,
                "surface": "user",
                "session_key": "archived-text",
                "checkpoint_json": json.dumps({
                    "transcript": [{
                        "role": "user",
                        "content": [{"type": "input_text", "text": "复核历史对话归档"}],
                    }]
                }),
            },
        ])
        connection.execute(message.insert(), [{
            "id": 1,
            "user_id": 1,
            "sent_from": "session:user:mesh-only",
            "send_to": "agent:reviewer",
        }])
        before_message_count = connection.execute(text(
            "SELECT COUNT(*) FROM agent_mesh_message"
        )).scalar_one()
        before_run_count = connection.execute(text(
            "SELECT COUNT(*) FROM agent_response_run"
        )).scalar_one()

        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        values = {
            row.id: (row.title, row.status)
            for row in connection.execute(text(
                "SELECT id,title,status FROM agent_mesh_conversation ORDER BY id"
            ))
        }
        assert values[1] == ("帮我 检查 API 授权问题", "active")
        assert values[2] == ("新对话", "archived")
        assert values[3] == ("默认对话", "active")
        assert values[4] == ("贾维斯运维对话", "active")
        assert values[5] == ("新对话", "active")
        assert values[6] == ("已有标题", "active")
        assert values[7] == ("复核历史对话归档", "archived")
        assert values[8] == ("新对话", "active")
        assert connection.execute(text(
            "SELECT COUNT(*) FROM migration_055_conversation_state"
        )).scalar_one() == 3
        assert connection.execute(text(
            "SELECT COUNT(*) FROM agent_mesh_message"
        )).scalar_one() == before_message_count
        assert connection.execute(text(
            "SELECT COUNT(*) FROM agent_response_run"
        )).scalar_one() == before_run_count

        migration.downgrade()
        restored = {
            row.id: (row.title, row.status)
            for row in connection.execute(text(
                "SELECT id,title,status FROM agent_mesh_conversation ORDER BY id"
            ))
        }
        assert restored == {row[0]: (row[4], row[5]) for row in rows}
        assert migration.STATE_TABLE not in inspect(connection).get_table_names()

    engine.dispose()


def test_title_extraction_is_bounded_and_rejects_placeholder_content() -> None:
    migration = _migration()
    assert migration._title_from_checkpoint(json.dumps({
        "transcript": [{"role": "user", "content": "新对话"}],
    })) == ""
    title = migration._title_from_checkpoint(json.dumps({
        "transcript": [{"role": "user", "content": "A" * 40}],
    }))
    assert title == "A" * 32 + "…"
