"""034 普通成员私有 Agent 草稿权限迁移回归。"""

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
    path = Path(__file__).resolve().parents[2] / "alembic/versions/034_agent_draft_permissions.py"
    spec = importlib.util.spec_from_file_location("migration_034", path)
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


def test_migration_034_grants_only_private_draft_permissions_and_is_idempotent(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, permission, role_permission = _schema(engine)
    target_codes = (
        "agent_asset:create",
        "agent_asset:update_own",
        "agent_asset:test",
        "agent_asset:submit",
    )

    with engine.begin() as conn:
        conn.execute(role.insert(), [
            {"id": 1, "code": "user"},
            {"id": 2, "code": "reviewer"},
            {"id": 3, "code": "admin"},
        ])
        conn.execute(permission.insert(), [
            {"id": index + 10, "code": code} for index, code in enumerate(target_codes)
        ] + [
            {"id": 30, "code": "agent_asset:publish"},
            {"id": 31, "code": "agent_asset:approve"},
        ])
        conn.execute(role_permission.insert(), [
            # 015 已授予 reviewer 的既有权限，034 的 downgrade 不得删除。
            *[
                {"role_id": 2, "permission_id": index + 10}
                for index, _code in enumerate(target_codes)
            ],
            {"role_id": 3, "permission_id": 10},
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
            ("admin", "agent_asset:create"),
            *[("reviewer", code) for code in sorted(target_codes)],
            *[("user", code) for code in sorted(target_codes)],
        ]
        assert not any(code in {"agent_asset:publish", "agent_asset:approve"} for _, code in granted)

        migration.downgrade()
        remaining = conn.execute(
            sa.select(role_permission.c.role_id, role_permission.c.permission_id)
            .order_by(role_permission.c.role_id, role_permission.c.permission_id)
        ).all()
        assert remaining == [
            *[(2, index + 10) for index, _code in enumerate(target_codes)],
            (3, 10),
        ]

    assert migration.revision == "034"
    assert migration.down_revision == "033"
    engine.dispose()


def test_migration_034_fails_before_writing_if_contract_is_incomplete(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, permission, role_permission = _schema(engine)

    with engine.begin() as conn:
        conn.execute(role.insert(), [{"id": 1, "code": "user"}, {"id": 2, "code": "reviewer"}])
        conn.execute(permission.insert().values(id=10, code="agent_asset:create"))
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(as_sql=False))
        with pytest.raises(RuntimeError, match="missing required permissions"):
            migration.upgrade()
        assert conn.execute(sa.select(role_permission.c.id)).all() == []

    engine.dispose()


def test_migration_034_renders_idempotent_offline_mysql_sql():
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
    assert "agent_asset:publish" not in sql
    assert "agent_asset:approve" not in sql
    for code in (
        "agent_asset:create",
        "agent_asset:update_own",
        "agent_asset:test",
        "agent_asset:submit",
    ):
        assert code in sql
