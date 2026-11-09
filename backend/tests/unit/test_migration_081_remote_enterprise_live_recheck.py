"""081 保留远测任务必须沿用企业审批的来源标记。"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_migration_adds_enterprise_approval_required_flag():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/081_remote_enterprise_live_recheck.py"
    spec = importlib.util.spec_from_file_location("migration_081_remote_enterprise_live_recheck", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "080_assistant_recovery"
    assert len(module.revision) <= 32

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE remote_pentest_engagement (
                id INTEGER PRIMARY KEY,
                public_id VARCHAR(32) NOT NULL
            )
        """))
        connection.execute(text(
            "INSERT INTO remote_pentest_engagement (id, public_id) VALUES (1, 'legacy-task')"
        ))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        column = next(
            item for item in inspect(connection).get_columns("remote_pentest_engagement")
            if item["name"] == "enterprise_approval_required"
        )
        assert column["nullable"] is False
        assert connection.execute(text(
            "SELECT enterprise_approval_required FROM remote_pentest_engagement"
        )).scalar_one() == 0
        with pytest.raises(RuntimeError, match="恢复|回退|迁移"):
            module.downgrade()
    engine.dispose()
