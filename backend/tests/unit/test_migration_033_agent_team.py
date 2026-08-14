"""033 动态子 Agent 团队迁移契约。"""

import importlib.util
import io
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/033_agent_team.py"
    spec = importlib.util.spec_from_file_location("migration_033_agent_team", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_team_tables_and_indexes():
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        Operations.context = context
        module.op = Operations(context)
        module.upgrade()
    tables = set(inspect(engine).get_table_names())
    assert {"agent_team", "agent_team_member", "agent_team_task", "agent_team_event"} <= tables
    assert "ix_agent_team_task_queue" in {item["name"] for item in inspect(engine).get_indexes("agent_team_task")}


def test_mysql_upgrade_does_not_define_defaults_for_longtext_columns():
    module = _migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output},
    )
    Operations.context = context
    module.op = Operations(context)
    module.upgrade()
    ddl = output.getvalue()

    json_columns = {
        "summary_json",
        "error_json",
        "capabilities_json",
        "dependency_keys_json",
        "input_json",
        "result_json",
        "artifacts_json",
        "errors_json",
        "detail_json",
    }
    for column_name in json_columns:
        definition = f"{column_name} LONGTEXT NOT NULL"
        assert definition in ddl
        assert f"{definition} DEFAULT" not in ddl
