"""079 应保留旧人类消息并允许企业小菱独立身份。"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_migration_adds_tenant_runs_and_assistant_message_identity():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/079_enterprise_assistant_runs.py"
    spec = importlib.util.spec_from_file_location("migration_079_enterprise_assistant_runs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE enterprise_chat_message (
                id INTEGER PRIMARY KEY,
                enterprise_id BIGINT NOT NULL,
                conversation_id BIGINT NOT NULL,
                sender_id BIGINT NOT NULL,
                client_message_id VARCHAR(80) NOT NULL,
                content TEXT NOT NULL,
                create_time DATETIME NOT NULL,
                update_time DATETIME NOT NULL,
                CONSTRAINT uq_enterprise_chat_message_client
                    UNIQUE (conversation_id, sender_id, client_message_id)
            )
        """))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        inspector = inspect(connection)
        columns = {column["name"]: column for column in inspector.get_columns("enterprise_chat_message")}
        assert columns["sender_id"]["nullable"] is True
        assert columns["sender_type"]["nullable"] is False
        assert {"assistant_run_id"}.issubset(columns)
        assert "prompt_sha256" in {column["name"] for column in inspector.get_columns("enterprise_assistant_run")}
        assert any(key["name"] == "uq_ent_assistant_run_team_group"
                   for key in inspector.get_unique_constraints("enterprise_assistant_run"))
        assert any(key["name"] == "uq_enterprise_chat_assistant_run"
                   for key in inspector.get_unique_constraints("enterprise_chat_message"))
        assert any(fk["referred_table"] == "enterprise_assistant_run"
                   for fk in inspector.get_foreign_keys("enterprise_chat_message"))
        with pytest.raises(RuntimeError, match="迁移|回退|降级"):
            module.downgrade()
    engine.dispose()
