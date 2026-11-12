import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_migration_115_scopes_clarification_evidence_to_run_and_message(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/115_ent_assistant_clarification.py"
    spec = importlib.util.spec_from_file_location("migration_115", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "114_pentest_parent_exit_proof"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.exec_driver_sql(
            "CREATE TABLE enterprise_assistant_run (id INTEGER PRIMARY KEY, enterprise_id INTEGER NOT NULL, "
            "conversation_id INTEGER NOT NULL, initiator_user_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, "
            "UNIQUE(id, enterprise_id, conversation_id, initiator_user_id, source_message_id))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE enterprise_chat_message (id INTEGER PRIMARY KEY, enterprise_id INTEGER NOT NULL, "
            "conversation_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, "
            "UNIQUE(id, enterprise_id, conversation_id, sender_id))"
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        conn.exec_driver_sql("INSERT INTO enterprise_assistant_run VALUES (1, 10, 30, 3, 101)")
        conn.exec_driver_sql("INSERT INTO enterprise_chat_message VALUES (100, 10, 30, 2)")
        conn.exec_driver_sql(
            "INSERT INTO enterprise_assistant_clarification "
            "(id, assistant_run_id, enterprise_id, conversation_id, source_message_id, initiator_user_id, "
            "phase, call_id, action, target_json, arguments_json, arguments_sha256, authority_sha256, version, "
            "expires_at, create_time, update_time) VALUES "
            "(1, 1, 10, 30, 101, 3, 'tool', 'call', 'write', '{}', '{}', 'a', 'b', 1, "
            "'2030-01-01', '2026-01-01', '2026-01-01')"
        )
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.exec_driver_sql(
                    "INSERT INTO enterprise_assistant_clarification_decision "
                    "(request_id, enterprise_id, conversation_id, source_message_id, author_user_id, "
                    "create_time, update_time) VALUES (1, 10, 31, 100, 2, '2026-01-01', '2026-01-01')"
                )
        conn.exec_driver_sql(
            "INSERT INTO enterprise_assistant_clarification_decision "
            "(request_id, enterprise_id, conversation_id, source_message_id, author_user_id, "
            "create_time, update_time) VALUES (1, 10, 30, 100, 2, '2026-01-01', '2026-01-01')"
        )
        with pytest.raises(RuntimeError):
            migration.downgrade()
    engine.dispose()
