"""企业消息和内容迁移从空 SQLite 建表，并禁止破坏性降级。"""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError


def test_enterprise_chat_migration_creates_tables():
    migration_path = Path(__file__).resolve().parents[2] / "alembic/versions/068_enterprise_chat.py"
    spec = importlib.util.spec_from_file_location("migration_enterprise_chat", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "067_billing_ledger"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_group (id INTEGER PRIMARY KEY, enterprise_id INTEGER NOT NULL, "
            "UNIQUE (enterprise_id, id))"
        )
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        inspector = inspect(connection)
        assert {
            "enterprise_conversation", "enterprise_chat_message", "enterprise_forum_post",
            "enterprise_forum_reply", "enterprise_announcement",
        }.issubset(inspector.get_table_names())
        assert any(index["name"] == "ix_enterprise_chat_message_conversation"
                   for index in inspector.get_indexes("enterprise_chat_message"))
        assert any(key["name"] == "uq_ent_conversation_scope"
                   for key in inspector.get_unique_constraints("enterprise_conversation"))
        assert any(key["name"] == "fk_ent_conversation_group_scope"
                   for key in inspector.get_foreign_keys("enterprise_conversation"))
        connection.exec_driver_sql(
            "INSERT INTO enterprise_conversation "
            "(id, enterprise_id, kind, conversation_key, group_id, created_by, create_time, update_time) "
            "VALUES (1, 70, 'company', 'company-1', NULL, 7, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql("INSERT INTO enterprise_group (id, enterprise_id) VALUES (90, 71)")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO enterprise_conversation "
                "(id, enterprise_id, kind, conversation_key, group_id, created_by, create_time, update_time) "
                "VALUES (2, 70, 'group', 'wrong-tenant-group', 90, 7, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        for name in ("enterprise_forum_post", "enterprise_forum_reply", "enterprise_announcement"):
            assert {"request_key", "request_digest"}.issubset({col["name"] for col in inspector.get_columns(name)})
            assert any(set(key["column_names"]) == {"enterprise_id", "author_id", "request_key"}
                       for key in inspector.get_unique_constraints(name))
        with pytest.raises(RuntimeError):
            module.downgrade()
    engine.dispose()
