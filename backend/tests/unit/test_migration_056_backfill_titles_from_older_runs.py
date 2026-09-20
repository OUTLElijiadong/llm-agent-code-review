"""056 迁移：从较早 Responses run 回填仍为占位的会话标题。"""

import json
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, text


def _migration():
    path = Path(__file__).parents[2] / "alembic/versions/056_backfill_titles_from_older_runs.py"
    spec = spec_from_file_location("migration_056", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_uses_older_run_when_latest_run_has_no_real_user_text() -> None:
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
    now = datetime(2026, 9, 20, 7, 0, 0)

    with engine.begin() as connection:
        connection.execute(conversation.insert(), [
            {
                "id": 1, "user_id": 7, "surface": "admin", "session_key": "older-real",
                "title": "贾维斯运维对话", "status": "archived",
                "last_seen_at": now, "last_message_at": now,
            },
            {
                "id": 2, "user_id": 7, "surface": "user", "session_key": "older-after-placeholder",
                "title": "新对话", "status": "active",
                "last_seen_at": now, "last_message_at": now,
            },
            {
                "id": 3, "user_id": 7, "surface": "user", "session_key": "already-named",
                "title": "已有标题", "status": "archived",
                "last_seen_at": now, "last_message_at": now,
            },
            {
                "id": 4, "user_id": 8, "surface": "user", "session_key": "no-real-text",
                "title": "默认对话", "status": "archived",
                "last_seen_at": now, "last_message_at": now,
            },
        ])
        connection.execute(response_run.insert(), [
            {
                "id": 10, "user_id": 7, "surface": "admin", "session_key": "older-real",
                "checkpoint_json": json.dumps({"transcript": [{"role": "user", "content": "复核旧版授权历史"}]}),
            },
            {
                "id": 11, "user_id": 7, "surface": "admin", "session_key": "older-real",
                "checkpoint_json": json.dumps({"transcript": [{"role": "assistant", "content": "已完成"}]}),
            },
            {
                "id": 20, "user_id": 7, "surface": "user", "session_key": "older-after-placeholder",
                "checkpoint_json": json.dumps({"transcript": [{"role": "user", "content": "检查跨账号会话"}]}),
            },
            {
                "id": 21, "user_id": 7, "surface": "user", "session_key": "older-after-placeholder",
                "checkpoint_json": json.dumps({"transcript": [{"role": "user", "content": "新对话"}]}),
            },
            {
                "id": 30, "user_id": 7, "surface": "user", "session_key": "already-named",
                "checkpoint_json": json.dumps({"transcript": [{"role": "user", "content": "不应覆盖"}]}),
            },
            {
                "id": 40, "user_id": 8, "surface": "user", "session_key": "no-real-text",
                "checkpoint_json": "{}",
            },
        ])
        connection.execute(message.insert(), [{
            "id": 1, "user_id": 7,
            "sent_from": "session:admin:older-real", "send_to": "agent:operations",
        }])
        before = {
            "conversations": connection.execute(text("SELECT COUNT(*) FROM agent_mesh_conversation")).scalar_one(),
            "messages": connection.execute(text("SELECT COUNT(*) FROM agent_mesh_message")).scalar_one(),
            "runs": connection.execute(text("SELECT COUNT(*) FROM agent_response_run")).scalar_one(),
        }

        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        titles = dict(connection.execute(text(
            "SELECT id,title FROM agent_mesh_conversation ORDER BY id"
        )).all())
        assert titles == {
            1: "复核旧版授权历史",
            2: "检查跨账号会话",
            3: "已有标题",
            4: "默认对话",
        }
        assert connection.execute(text(
            "SELECT COUNT(*) FROM migration_056_conversation_title_state"
        )).scalar_one() == 2
        assert {
            "conversations": connection.execute(text("SELECT COUNT(*) FROM agent_mesh_conversation")).scalar_one(),
            "messages": connection.execute(text("SELECT COUNT(*) FROM agent_mesh_message")).scalar_one(),
            "runs": connection.execute(text("SELECT COUNT(*) FROM agent_response_run")).scalar_one(),
        } == before

        migration.downgrade()
        restored = dict(connection.execute(text(
            "SELECT id,title FROM agent_mesh_conversation ORDER BY id"
        )).all())
        assert restored == {
            1: "贾维斯运维对话",
            2: "新对话",
            3: "已有标题",
            4: "默认对话",
        }
        assert migration.STATE_TABLE not in inspect(connection).get_table_names()

    engine.dispose()
