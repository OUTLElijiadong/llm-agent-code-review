"""The personal agent migration leaves legacy users intact."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_personal_agent_migration_preserves_legacy_and_has_unique_keys():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/103_personal_agent.py"
    spec = importlib.util.spec_from_file_location("personal_agent_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "102_billing_tool_recovery"
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE legacy_user (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO legacy_user VALUES (1, 'old')"))
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        schema = inspect(conn)
        assert {"personal_agent", "personal_agent_version"} <= set(schema.get_table_names())
        assert {"owner_id", "create_key", "create_hash", "current_version", "status"} <= {
            c["name"] for c in schema.get_columns("personal_agent")
        }
        assert {"agent_id", "version_number", "request_key", "request_hash"} <= {
            c["name"] for c in schema.get_columns("personal_agent_version")
        }
        assert conn.execute(text("SELECT name FROM legacy_user WHERE id=1")).scalar_one() == "old"
        assert any(
            set(x["column_names"]) == {"owner_id", "create_key"}
            for x in schema.get_unique_constraints("personal_agent")
        )
        assert any(
            set(x["column_names"]) == {"agent_id", "version_number"}
            for x in schema.get_unique_constraints("personal_agent_version")
        )
    engine.dispose()
