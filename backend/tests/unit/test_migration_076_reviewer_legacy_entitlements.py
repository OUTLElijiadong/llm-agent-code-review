"""076 将升级前有效的审查员明确记录为永久权益，保留生产既有权限。"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_migration_grandfathers_existing_reviewer_roles():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/076_reviewer_legacy_entitlements.py"
    spec = importlib.util.spec_from_file_location("migration_076_reviewer_legacy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "075_payment_checkout_state"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT, role TEXT, status INTEGER)"))
        connection.execute(text("CREATE TABLE role (id INTEGER PRIMARY KEY, code TEXT, status TEXT)"))
        connection.execute(text("CREATE TABLE user_role (user_id INTEGER, role_id INTEGER)"))
        connection.execute(text("""
            CREATE TABLE billing_permanent_reviewer (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, status TEXT,
                granted_by INTEGER, granted_at DATETIME, revoked_by INTEGER, revoked_at DATETIME
            )
        """))
        connection.execute(text("""
            CREATE TABLE billing_permanent_reviewer_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, actor_id INTEGER,
                action TEXT, reason TEXT, created_at DATETIME
            )
        """))
        connection.execute(text("""
            INSERT INTO user (id, username, role, status) VALUES
                (1, 'admin', 'super_admin', 1), (2, 'legacy-reviewer', 'reviewer', 1),
                (3, 'disabled-reviewer', 'reviewer', 0), (4, 'conflicted-reviewer', 'reviewer', 1),
                (5, 'ordinary', 'user', 1)
        """))
        connection.execute(text("""
            INSERT INTO role (id, code, status) VALUES
            (10, 'reviewer', 'active'), (11, 'user', 'active'), (12, 'super_admin', 'active')
        """))
        connection.execute(text("""
            INSERT INTO user_role (user_id, role_id) VALUES (1, 12), (3, 10), (4, 11)
        """))
        # 其他迁移单测会把 Operations.context 覆盖为 MigrationContext，且未恢复；
        # 直接注入 Operations 实例可避免依赖被污染的 Alembic 全局代理。
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        rows = connection.execute(text(
            "SELECT user_id, status, granted_by FROM billing_permanent_reviewer ORDER BY user_id"
        )).all()
        assert rows == [(2, "active", 1), (3, "active", 1)]
        events = connection.execute(text(
            "SELECT user_id, actor_id, action, reason FROM billing_permanent_reviewer_event ORDER BY user_id"
        )).all()
        assert len(events) == 2
        assert all(row[1] == 1 and row[2] == "grandfather" for row in events)
        assert all("076" in row[3] for row in events)
        assert "billing_permanent_reviewer" in inspect(connection).get_table_names()
    engine.dispose()
