"""030 Agent Mesh 迁移的真实升级/回滚验证。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/030_agent_mesh.py"
    spec = importlib.util.spec_from_file_location("migration_030_agent_mesh", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_030_upgrade_and_downgrade_on_sqlite() -> None:
    module = _migration_module()
    assert module.revision == "030"
    assert module.down_revision == "029"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE agent_response_run (id INTEGER PRIMARY KEY)"))
        operations = Operations(MigrationContext.configure(connection))
        module.op = operations

        module.upgrade()
        inspector = inspect(connection)
        assert {
            "agent_mesh_conversation",
            "agent_mesh_message",
            "agent_mesh_message_event",
        } <= set(inspector.get_table_names())
        assert "mesh_message_id" in {
            column["name"] for column in inspector.get_columns("agent_response_run")
        }
        assert "ix_agent_response_run_mesh_message" in {
            index["name"] for index in inspector.get_indexes("agent_response_run")
        }

        module.downgrade()
        inspector = inspect(connection)
        assert not {
            "agent_mesh_conversation",
            "agent_mesh_message",
            "agent_mesh_message_event",
        } & set(inspector.get_table_names())
        assert "mesh_message_id" not in {
            column["name"] for column in inspector.get_columns("agent_response_run")
        }

    engine.dispose()
