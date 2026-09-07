"""实跑升级与回退，验证历史状态不变和真实数据库外键拒绝孤儿归因。"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_usage_migration_preserves_history_enforces_foreign_keys_and_rolls_back():
    path = Path(__file__).parents[2] / "alembic/versions/048_ai_usage_attribution.py"
    spec = spec_from_file_location("usage_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    tables = set(migration._TABLES) | set(migration._FIELDS.values())
    for name in tables:
        Table(name, metadata, Column("id", Integer, primary_key=True), Column("status", String(30)))
    metadata.create_all(engine)
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            for name in sorted(tables):
                connection.execute(text(f"INSERT INTO {name} (id,status) VALUES (11,'executing')"))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            for name in migration._TABLES:
                values = connection.execute(text(f"SELECT status,{','.join(migration._FIELDS)} FROM {name}")).one()
                assert values == ("executing",) + (None,) * len(migration._FIELDS)
                assert len(inspect(connection).get_foreign_keys(name)) == len(migration._FIELDS)
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("UPDATE ai_call_log SET root_agent_run_id=999 WHERE id=11"))
            connection.execute(text("UPDATE ai_call_log SET root_agent_run_id=11, agent_team_task_id=11 WHERE id=11"))
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            migration.downgrade()
            for name in tables:
                assert connection.execute(text(f"SELECT status FROM {name} WHERE id=11")).scalar() == "executing"
                assert {col["name"] for col in inspect(connection).get_columns(name)} == {"id", "status"}


def test_mysql_downgrade_drops_foreign_key_before_its_supporting_index():
    from io import StringIO

    path = Path(__file__).parents[2] / "alembic/versions/048_ai_usage_attribution.py"
    spec = spec_from_file_location("usage_migration_mysql", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    migration.op = Operations(
        MigrationContext.configure(
            dialect_name="mysql",
            opts={"as_sql": True, "output_buffer": output},
        )
    )
    migration.downgrade()
    sql = output.getvalue()
    for table in migration._TABLES:
        for column in migration._FIELDS:
            constraint = f"ALTER TABLE {table} DROP FOREIGN KEY fk_{table}_{column}"
            index = f"DROP INDEX ix_{table}_{column} ON {table}"
            dropped_column = f"ALTER TABLE {table} DROP COLUMN {column}"
            assert sql.index(constraint) < sql.index(index) < sql.index(dropped_column)
