"""080 为企业小菱保存安全重试边界和可恢复终态输出。"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_migration_adds_attempt_count_and_recoverable_output():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/080_assistant_recovery.py"
    spec = importlib.util.spec_from_file_location("migration_080_assistant_recovery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE enterprise_assistant_run (
                id INTEGER PRIMARY KEY,
                status VARCHAR(24) NOT NULL DEFAULT 'queued',
                update_time DATETIME NOT NULL
            )
        """))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("enterprise_assistant_run")}
        assert columns["attempt_count"]["nullable"] is False
        assert columns["response_text"]["nullable"] is True
        with pytest.raises(RuntimeError, match="迁移|回退|降级"):
            module.downgrade()
    engine.dispose()
