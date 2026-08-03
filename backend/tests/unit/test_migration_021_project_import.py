"""021 ordinary-user remote project import permission regression tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa


def _migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "021_grant_project_import_to_users.py"
    )
    spec = importlib.util.spec_from_file_location("migration_021", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(engine):
    metadata = sa.MetaData()
    role = sa.Table(
        "role",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
    )
    permission = sa.Table(
        "permission",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
    )
    role_permission = sa.Table(
        "role_permission",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("role_id", sa.Integer, nullable=False),
        sa.Column("permission_id", sa.Integer, nullable=False),
        sa.UniqueConstraint("role_id", "permission_id"),
    )
    metadata.create_all(engine)
    return role, permission, role_permission


def test_migration_021_grants_only_user_and_reviewer_and_is_idempotent(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, permission, role_permission = _schema(engine)

    with engine.begin() as conn:
        conn.execute(
            role.insert(),
            [
                {"id": 1, "code": "user"},
                {"id": 2, "code": "reviewer"},
                {"id": 3, "code": "auditor"},
                {"id": 4, "code": "admin"},
            ],
        )
        conn.execute(permission.insert().values(id=5, code="project:import"))
        conn.execute(role_permission.insert().values(role_id=4, permission_id=5))
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()
        migration.upgrade()
        grants = conn.execute(
            sa.select(role_permission.c.role_id, role_permission.c.permission_id)
            .order_by(role_permission.c.role_id)
        ).all()
        assert grants == [(1, 5), (2, 5), (4, 5)]

        migration.downgrade()
        remaining = conn.execute(
            sa.select(role_permission.c.role_id, role_permission.c.permission_id)
        ).all()
        assert remaining == [(4, 5)]

    assert migration.revision == "021"
    assert migration.down_revision == "020"
    engine.dispose()


def test_migration_021_fails_when_permission_contract_is_missing(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, _permission, _role_permission = _schema(engine)

    with engine.begin() as conn:
        conn.execute(
            role.insert(),
            [{"id": 1, "code": "user"}, {"id": 2, "code": "reviewer"}],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        with pytest.raises(RuntimeError, match="missing required permission"):
            migration.upgrade()

    engine.dispose()


def test_migration_021_fails_before_writing_when_target_role_is_missing(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, permission, role_permission = _schema(engine)

    with engine.begin() as conn:
        conn.execute(role.insert().values(id=1, code="user"))
        conn.execute(permission.insert().values(id=5, code="project:import"))
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        with pytest.raises(RuntimeError, match="missing required roles: reviewer"):
            migration.upgrade()

        assert conn.execute(sa.select(role_permission.c.id)).all() == []

    engine.dispose()


def test_migration_021_downgrade_is_noop_when_permission_is_missing(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    role, _permission, role_permission = _schema(engine)

    with engine.begin() as conn:
        conn.execute(
            role.insert(),
            [{"id": 1, "code": "user"}, {"id": 2, "code": "reviewer"}],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.downgrade()

        assert conn.execute(sa.select(role_permission.c.id)).all() == []

    engine.dispose()
