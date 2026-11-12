"""119 stores immutable, service-derived authority for Xiaoling directives."""

import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError


def _migration():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/119_chat_message_authority.py"
    spec = importlib.util.spec_from_file_location("migration_119_chat_message_authority", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return backend, module


def test_119_is_the_single_head_and_follows_118():
    backend, module = _migration()
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["119_chat_message_authority"]
    assert module.down_revision == "118_clarification_authority"


def test_119_rank_is_validated_on_insert_and_immutable():
    _backend, module = _migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE enterprise_chat_message (id INTEGER PRIMARY KEY, "
            "enterprise_id INTEGER, conversation_id INTEGER, sender_id INTEGER, "
            "sender_type TEXT NOT NULL DEFAULT 'human', client_message_id TEXT, content TEXT NOT NULL DEFAULT '')"
        ))
        connection.execute(text("PRAGMA recursive_triggers=ON"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        columns = {item["name"] for item in inspect(connection).get_columns("enterprise_chat_message")}
        assert {"id", "sender_authority_rank"} <= columns
        connection.execute(text(
            "INSERT INTO enterprise_chat_message (id, sender_id, client_message_id, content, sender_authority_rank) "
            "VALUES (1, 7, 'directive-1', '@小菱 执行', 3)"
        ))
        connection.execute(text(
            "INSERT INTO enterprise_chat_message (id, client_message_id, content) VALUES (2, 'legacy-1', '旧消息')"
        ))
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "INSERT INTO enterprise_chat_message (id, client_message_id, content) "
                "VALUES (3, 'missing-rank', '@小菱 不得缺少快照')"
            ))
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "INSERT INTO enterprise_chat_message (id, client_message_id, content, sender_authority_rank) "
                "VALUES (4, 'bad-rank', '@小菱 越界快照', 5)"
            ))
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "UPDATE enterprise_chat_message SET sender_authority_rank=1 WHERE id=1"
            ))
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "UPDATE enterprise_chat_message SET content='@小菱 变更命令' WHERE id=1"
            ))
        with pytest.raises(DBAPIError):
            connection.execute(text("DELETE FROM enterprise_chat_message WHERE id=1"))
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "UPDATE enterprise_chat_message SET content='@小菱 不能从普通消息补造指令' WHERE id=2"
            ))
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "INSERT OR REPLACE INTO enterprise_chat_message "
                "(id, sender_id, client_message_id, content, sender_authority_rank) "
                "VALUES (1, 7, 'directive-1', '@小菱 覆盖命令', 3)"
            ))
        with pytest.raises(RuntimeError, match="自动降级"):
            module.downgrade()
    engine.dispose()
