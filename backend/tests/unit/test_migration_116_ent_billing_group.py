import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_legacy_billing_attribution_remains_unassigned(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/116_ent_billing_group.py"
    spec = importlib.util.spec_from_file_location("migration_116", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "115_ent_assistant_clarification"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE enterprise_member (id INTEGER PRIMARY KEY, default_group_id INTEGER)")
        conn.exec_driver_sql("CREATE TABLE enterprise_assistant_run (id INTEGER PRIMARY KEY, group_id INTEGER)")
        conn.exec_driver_sql("INSERT INTO enterprise_member VALUES (1, 42)")
        conn.exec_driver_sql("INSERT INTO enterprise_assistant_run VALUES (1, NULL), (2, 42)")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        assert conn.exec_driver_sql(
            "SELECT default_group_id, billing_group_version FROM enterprise_member WHERE id=1"
        ).one() == (42, 0)
        assert conn.exec_driver_sql(
            "SELECT group_id, billing_group_id, billing_attribution_version, billing_membership_version, "
            "billing_user_token_version "
            "FROM enterprise_assistant_run ORDER BY id"
        ).all() == [(None, None, 0, None, None), (42, None, 0, None, None)]
        conn.exec_driver_sql("INSERT INTO enterprise_member (id) VALUES (2)")
        assert conn.exec_driver_sql("SELECT billing_group_version FROM enterprise_member WHERE id=2").scalar() == 0
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.exec_driver_sql("INSERT INTO enterprise_member (id, billing_group_version) VALUES (3, NULL)")
        with pytest.raises(RuntimeError):
            migration.downgrade()
    engine.dispose()
