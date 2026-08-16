"""035 普通成员 security:scan 权限迁移回归。"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/035_security_scan_user.py"
    spec = importlib.util.spec_from_file_location("migration_035", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(engine):
    metadata = sa.MetaData()
    role = sa.Table(
        "role", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
    )
    permission = sa.Table(
        "permission", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(80), nullable=False, unique=True),
    )
    role_permission = sa.Table(
        "role_permission", metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("role_id", sa.Integer, nullable=False),
        sa.Column("permission_id", sa.Integer, nullable=False),
        sa.UniqueConstraint("role_id", "permission_id"),
    )
    metadata.create_all(engine)
    return role, permission, role_permission


def test_migration_035_grants_security_scan_to_user_only_and_is_idempotent(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, permission, role_permission = _schema(engine)

    with engine.begin() as conn:
        conn.execute(role.insert(), [
            {"id": 1, "code": "user"},
            {"id": 2, "code": "reviewer"},
            {"id": 3, "code": "admin"},
        ])
        conn.execute(permission.insert(), [
            {"id": 10, "code": "security:scan"},
            {"id": 11, "code": "security:view"},
            {"id": 12, "code": "project:view"},
        ])
        conn.execute(role_permission.insert(), [
            # 007 已授予 reviewer 的既有 security:scan，035 的 downgrade 不得删除。
            {"role_id": 2, "permission_id": 10},
            {"role_id": 1, "permission_id": 12},
        ])
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(as_sql=False))

        migration.upgrade()
        migration.upgrade()
        granted = conn.execute(
            sa.select(role.c.code, permission.c.code)
            .select_from(
                role_permission
                .join(role, role.c.id == role_permission.c.role_id)
                .join(permission, permission.c.id == role_permission.c.permission_id)
            )
            .order_by(role.c.code, permission.c.code)
        ).all()
        assert granted == [
            ("reviewer", "security:scan"),
            ("user", "project:view"),
            ("user", "security:scan"),
        ]
        assert not any(code == "security:view" for _, code in granted)

        migration.downgrade()
        remaining = conn.execute(
            sa.select(role_permission.c.role_id, role_permission.c.permission_id)
            .order_by(role_permission.c.role_id, role_permission.c.permission_id)
        ).all()
        assert remaining == [(1, 12), (2, 10)]

    assert migration.revision == "035"
    assert migration.down_revision == "034"
    engine.dispose()


def test_migration_035_fails_before_writing_if_permission_missing(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, permission, role_permission = _schema(engine)

    with engine.begin() as conn:
        conn.execute(role.insert(), [{"id": 1, "code": "user"}])
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(as_sql=False))
        with pytest.raises(RuntimeError, match="missing required permissions"):
            migration.upgrade()
        assert conn.execute(sa.select(role_permission.c.id)).all() == []

    engine.dispose()


def test_migration_035_renders_idempotent_offline_mysql_sql():
    migration = _migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
    )
    Operations.context = context
    migration.op = Operations(context)

    migration.upgrade()
    migration.downgrade()
    sql = output.getvalue()

    assert "INSERT INTO role_permission (role_id, permission_id)" in sql
    assert "NOT (EXISTS" in sql
    assert "DELETE FROM role_permission" in sql
    assert "security:scan" in sql
    assert "security:view" not in sql
