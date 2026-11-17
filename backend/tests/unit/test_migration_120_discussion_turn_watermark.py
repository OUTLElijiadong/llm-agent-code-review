"""The discussion cutoff belongs to the published assistant message."""

import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def _migration():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/120_discussion_turn_watermark.py"
    spec = importlib.util.spec_from_file_location("migration_120_discussion_turn_watermark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return backend, module


def test_120_is_in_the_single_chain_and_follows_119():
    backend, module = _migration()
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["122_platform_model_cost_ledger"]
    assert script.get_revision("122_platform_model_cost_ledger").down_revision == "121_billing_model_evidence"
    assert script.get_revision("121_billing_model_evidence").down_revision == module.revision
    assert module.down_revision == "119_chat_message_authority"


def test_120_adds_nullable_publication_boundary_and_refuses_downgrade():
    _backend, module = _migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE enterprise_chat_message (id INTEGER PRIMARY KEY, enterprise_id INTEGER, "
            "conversation_id INTEGER, content TEXT NOT NULL)"
        ))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        column = next(
            item for item in inspect(connection).get_columns("enterprise_chat_message")
            if item["name"] == "discussion_closed_through_message_id"
        )
        assert column["nullable"] is True
        indexes = {item["name"] for item in inspect(connection).get_indexes("enterprise_chat_message")}
        assert "ix_ent_chat_discussion_cutoff" in indexes
        with pytest.raises(RuntimeError, match="不能自动降级"):
            module.downgrade()
    engine.dispose()
