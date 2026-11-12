"""Exercise the deploy schema gate against actual MySQL ACTION_STATEMENT rows."""
from __future__ import annotations

import json
import pathlib
import sys

import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.engine import Connection

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/lib"))
from chat_authority_schema_compatibility import classify_schema, expected_triggers  # noqa: E402

MIGRATION = ROOT / "backend/alembic/versions/119_chat_message_authority.py"


def _schema_snapshot(connection: Connection) -> dict:
    raw = connection.execute(text("""
        SELECT JSON_OBJECT(
          'column_count',
          (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'enterprise_chat_message'
             AND column_name = 'sender_authority_rank'),
          'triggers',
          COALESCE((SELECT JSON_ARRAYAGG(JSON_OBJECT(
                      'trigger_name', trigger_name,
                      'action_timing', action_timing,
                      'event_manipulation', event_manipulation,
                      'action_statement', action_statement
                    ))
                    FROM information_schema.triggers
                    WHERE trigger_schema = DATABASE() AND event_object_table = 'enterprise_chat_message'),
                   JSON_ARRAY())
        )
    """)).scalar_one()
    return json.loads(raw) if isinstance(raw, str) else raw


def _create_validate_trigger(connection: Connection, body: str) -> None:
    connection.exec_driver_sql(
        "CREATE TRIGGER trg_ent_chat_authority_validate BEFORE INSERT ON enterprise_chat_message "
        f"FOR EACH ROW {body}"
    )


def test_deploy_schema_probe_matches_mysql_metadata_and_rejects_trigger_drift(fresh_chain):
    expected = expected_triggers(MIGRATION)
    validate_body = expected["trg_ent_chat_authority_validate"]["action_statement"]
    inner = validate_body.strip()[len("BEGIN"):-len("END")].strip()
    connection = fresh_chain.connect()
    try:
        assert classify_schema(_schema_snapshot(connection), expected) == "complete"

        connection.exec_driver_sql("DROP TRIGGER trg_ent_chat_authority_validate")
        _create_validate_trigger(connection, f"BEGIN IF 1 = 0 THEN {inner} END IF; END")
        assert classify_schema(_schema_snapshot(connection), expected) == "partial"

        connection.exec_driver_sql("DROP TRIGGER trg_ent_chat_authority_validate")
        _create_validate_trigger(connection, validate_body)
        connection.exec_driver_sql(
            "CREATE TRIGGER trg_unmanaged_sender_authority BEFORE INSERT ON enterprise_chat_message "
            "FOR EACH ROW SET NEW.sender_authority_rank = 4"
        )
        assert classify_schema(_schema_snapshot(connection), expected) == "partial"
    finally:
        present = set(connection.execute(text(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE trigger_schema = DATABASE() AND event_object_table = 'enterprise_chat_message'"
        )).scalars())
        if "trg_unmanaged_sender_authority" in present:
            connection.exec_driver_sql("DROP TRIGGER trg_unmanaged_sender_authority")
        if "trg_ent_chat_authority_validate" not in present:
            _create_validate_trigger(connection, validate_body)
        connection.close()
