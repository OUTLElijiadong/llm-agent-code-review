"""022 unique-super-admin data and guard regression tests."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "022_unique_super_admin.py"
    spec = importlib.util.spec_from_file_location("migration_022", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(engine):
    metadata = sa.MetaData()
    user = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.SmallInteger, nullable=False),
        sa.Column("token_version", sa.Integer, nullable=False, server_default="0"),
    )
    role = sa.Table(
        "role",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
    )
    permission = sa.Table(
        "permission",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
    )
    user_role = sa.Table(
        "user_role",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("role_id", sa.Integer, nullable=False),
        sa.UniqueConstraint("user_id", "role_id"),
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
    return user, role, permission, user_role, role_permission


def _seed(conn, tables):
    user, role, permission, user_role, role_permission = tables
    conn.execute(
        role.insert(),
        [
            {"id": 1, "code": "user", "status": "active"},
            {"id": 2, "code": "reviewer", "status": "active"},
            {"id": 4, "code": "admin", "status": "inactive"},
            {"id": 5, "code": "super_admin", "status": "inactive"},
        ],
    )
    conn.execute(
        permission.insert(),
        [
            {"id": 10, "code": "server_ops:view"},
            {"id": 11, "code": "server_ops:execute"},
            {"id": 12, "code": "server_ops:critical"},
            {"id": 13, "code": "project:view"},
            {"id": 14, "code": "server_ops:future"},
        ],
    )
    conn.execute(
        user.insert(),
        [
            {"id": 1, "username": "admin", "role": "admin", "status": 0, "token_version": 5},
            {"id": 2, "username": "legacy_super", "role": "super_admin", "status": 1, "token_version": 7},
            {"id": 3, "username": "rbac_super", "role": "user", "status": 1, "token_version": 2},
            {"id": 4, "username": "legacy_admin", "role": "admin", "status": 1, "token_version": 4},
            {"id": 5, "username": "rbac_admin", "role": "user", "status": 1, "token_version": 1},
            {"id": 6, "username": "reviewer", "role": "reviewer", "status": 1, "token_version": 9},
            {"id": 7, "username": "unaffected", "role": "user", "status": 1, "token_version": 3},
        ],
    )
    conn.execute(
        user_role.insert(),
        [
            {"user_id": 1, "role_id": 2},
            {"user_id": 1, "role_id": 4},
            {"user_id": 1, "role_id": 5},
            {"user_id": 2, "role_id": 2},
            {"user_id": 2, "role_id": 5},
            {"user_id": 3, "role_id": 5},
            {"user_id": 4, "role_id": 1},
            {"user_id": 5, "role_id": 4},
            {"user_id": 6, "role_id": 2},
            {"user_id": 7, "role_id": 1},
        ],
    )
    conn.execute(
        role_permission.insert(),
        [
            {"role_id": 4, "permission_id": 10},
            {"role_id": 4, "permission_id": 11},
            {"role_id": 4, "permission_id": 12},
            {"role_id": 2, "permission_id": 10},
            {"role_id": 5, "permission_id": 10},
            {"role_id": 5, "permission_id": 11},
            {"role_id": 5, "permission_id": 13},
            {"role_id": 1, "permission_id": 14},
        ],
    )


def test_migration_022_normalizes_accounts_permissions_and_tokens(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    tables = _schema(engine)
    user, role, _permission, user_role, role_permission = tables

    with engine.begin() as conn:
        _seed(conn, tables)
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        migration.upgrade()

        users = {row["username"]: row for row in conn.execute(sa.select(user)).mappings()}
        assert (users["admin"]["role"], users["admin"]["status"]) == ("super_admin", 1)
        assert conn.execute(
            sa.select(role.c.code, role.c.status).where(role.c.code.in_(("admin", "super_admin"))).order_by(role.c.code)
        ).all() == [("admin", "active"), ("super_admin", "active")]
        assert users["legacy_super"]["role"] == "admin"
        assert users["rbac_super"]["role"] == "admin"
        assert users["legacy_admin"]["role"] == "admin"
        assert users["rbac_admin"]["role"] == "admin"
        assert {name: users[name]["token_version"] for name in users} == {
            "admin": 6,
            "legacy_super": 8,
            "rbac_super": 3,
            "legacy_admin": 5,
            "rbac_admin": 2,
            "reviewer": 10,
            "unaffected": 4,
        }


def test_migration_022_adds_missing_token_version_before_normalization(monkeypatch):
    """未经过启动期自动补列的历史库也应能直接执行正式迁移。"""

    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    user = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.SmallInteger, nullable=False),
    )
    role = sa.Table(
        "role",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
    )
    permission = sa.Table(
        "permission",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
    )
    user_role = sa.Table(
        "user_role",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("role_id", sa.Integer, nullable=False),
        sa.UniqueConstraint("user_id", "role_id"),
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
    with engine.begin() as conn:
        conn.execute(role.insert(), [
            {"id": 4, "code": "admin", "status": "active"},
            {"id": 5, "code": "super_admin", "status": "active"},
        ])
        conn.execute(permission.insert(), [
            {"id": 10, "code": "server_ops:view"},
            {"id": 11, "code": "server_ops:execute"},
            {"id": 12, "code": "server_ops:critical"},
        ])
        conn.execute(user.insert(), {"id": 1, "username": "admin", "role": "admin", "status": 1})
        conn.execute(user_role.insert(), {"user_id": 1, "role_id": 4})
        conn.execute(role_permission.insert(), [
            {"role_id": 4, "permission_id": 10},
            {"role_id": 4, "permission_id": 11},
            {"role_id": 4, "permission_id": 12},
        ])
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()

        columns = {column["name"] for column in sa.inspect(conn).get_columns("user")}
        assert "token_version" in columns
        row = conn.execute(sa.text('SELECT role, token_version FROM "user" WHERE id = 1')).one()
        assert row == ("super_admin", 1)

        bindings = conn.execute(sa.select(user_role.c.user_id, user_role.c.role_id)).all()
        assert bindings == [(1, 5)]
        server_links = conn.execute(
            sa.select(role_permission.c.role_id, role_permission.c.permission_id)
            .order_by(role_permission.c.permission_id)
        ).all()
        assert server_links == [(5, 10), (5, 11), (5, 12)]


def test_migration_022_requires_secret_for_seeded_admin(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    tables = _schema(engine)
    with engine.begin() as conn:
        _seed(conn, tables)
        conn.exec_driver_sql('ALTER TABLE "user" ADD COLUMN password VARCHAR(255)')
        conn.exec_driver_sql(
            'UPDATE "user" SET password = ? WHERE id = 1',
            (migration._INITIAL_PASSWORD_PLACEHOLDER,),
        )
        monkeypatch.delenv("INITIAL_ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        with pytest.raises(RuntimeError, match="INITIAL_ADMIN_PASSWORD"):
            migration.upgrade()


def test_migration_022_hashes_initial_admin_secret(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    tables = _schema(engine)
    with engine.begin() as conn:
        _seed(conn, tables)
        conn.exec_driver_sql('ALTER TABLE "user" ADD COLUMN password VARCHAR(255)')
        conn.exec_driver_sql(
            'UPDATE "user" SET password = ? WHERE id = 1',
            (migration._LEGACY_DEFAULT_PASSWORD_HASH,),
        )
        monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "Fresh-Admin-Secret-2026")
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()

        stored = conn.exec_driver_sql('SELECT password FROM "user" WHERE id = 1').scalar_one()
        assert stored not in {
            migration._LEGACY_DEFAULT_PASSWORD_HASH,
            "Fresh-Admin-Secret-2026",
        }
        from app.core.security import verify_password

        assert verify_password("Fresh-Admin-Secret-2026", stored)

    assert migration.revision == "022"
    assert migration.down_revision == "021"
    engine.dispose()


def test_migration_022_sqlite_guards_reject_invariant_breaks(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    tables = _schema(engine)
    user, role, permission, user_role, role_permission = tables

    with engine.begin() as conn:
        _seed(conn, tables)
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        migration.upgrade()

        with pytest.raises(IntegrityError, match="invalid super administrator identity"):
            conn.execute(user.update().where(user.c.id == 1).values(role="admin"))
        with pytest.raises(IntegrityError, match="invalid super administrator identity"):
            conn.execute(user.update().where(user.c.id == 1).values(username="renamed-admin", role="admin"))
        with pytest.raises(IntegrityError, match="invalid super administrator identity"):
            conn.execute(user.update().where(user.c.id == 1).values(id=100))
        with pytest.raises(IntegrityError, match="admin account cannot be deleted"):
            conn.execute(user.delete().where(user.c.id == 1))
        with pytest.raises(IntegrityError, match="invalid super administrator identity"):
            conn.execute(
                user.insert().values(
                    id=8,
                    username="illegal_super",
                    role="super_admin",
                    status=1,
                    token_version=0,
                )
            )
        with pytest.raises(IntegrityError, match="invalid super administrator identity"):
            conn.execute(user.update().where(user.c.id == 1).values(role="SUPER_ADMIN"))
        with pytest.raises(IntegrityError, match="invalid super administrator identity"):
            conn.execute(
                user.insert().values(
                    id=9,
                    username="case_variant_super",
                    role="SUPER_ADMIN",
                    status=1,
                    token_version=0,
                )
            )
        with pytest.raises(IntegrityError, match="invalid super_admin role binding"):
            conn.execute(user_role.insert().values(user_id=7, role_id=5))
        with pytest.raises(IntegrityError, match="invalid super_admin role binding"):
            conn.execute(user_role.insert().values(user_id=1, role_id=4))
        with pytest.raises(IntegrityError, match="cannot be deleted"):
            conn.execute(
                user_role.delete().where(
                    user_role.c.user_id == 1,
                    user_role.c.role_id == 5,
                )
            )
        with pytest.raises(IntegrityError, match="reserved administrator role"):
            conn.execute(role.update().where(role.c.code == "super_admin").values(code="super_admin_temp"))
        with pytest.raises(IntegrityError, match="reserved administrator role"):
            conn.execute(role.update().where(role.c.code == "super_admin").values(id=100))
        with pytest.raises(IntegrityError, match="reserved administrator role"):
            conn.execute(role.update().where(role.c.code == "super_admin").values(status="inactive"))
        with pytest.raises(IntegrityError, match="reserved administrator role"):
            conn.execute(role.update().where(role.c.code == "admin").values(status="inactive"))
        with pytest.raises(IntegrityError, match="reserved administrator role"):
            conn.execute(role.update().where(role.c.code == "super_admin").values(status="ACTIVE"))
        with pytest.raises(IntegrityError, match="reserved administrator role"):
            conn.execute(role.delete().where(role.c.code == "super_admin"))
        with pytest.raises(IntegrityError, match="server_ops permission code"):
            conn.execute(
                permission.update()
                .where(permission.c.code == "project:view")
                .values(code="server_ops:renamed_from_project")
            )
        with pytest.raises(IntegrityError, match="server_ops permission code"):
            conn.execute(
                permission.update()
                .where(permission.c.code == "server_ops:view")
                .values(code="project:renamed_from_server_ops")
            )
        with pytest.raises(IntegrityError, match="server_ops permission code"):
            conn.execute(permission.update().where(permission.c.code == "server_ops:view").values(id=100))
        with pytest.raises(IntegrityError, match="server_ops permission cannot be deleted"):
            conn.execute(permission.delete().where(permission.c.code == "server_ops:view"))
        conn.execute(role_permission.insert().values(role_id=4, permission_id=15))
        with pytest.raises(IntegrityError, match="server_ops permission cannot be created"):
            conn.execute(permission.insert().values(code="server_ops:auto_id_orphaned_binding"))
        assert conn.execute(sa.select(permission.c.code).where(permission.c.id == 15)).first() is None
        conn.execute(role_permission.insert().values(role_id=4, permission_id=999))
        with pytest.raises(IntegrityError, match="server_ops permission cannot be created"):
            conn.execute(permission.insert().values(id=999, code="server_ops:orphaned_binding"))
        assert conn.execute(sa.select(permission.c.id).where(permission.c.id == 999)).first() is None
        conn.execute(role_permission.insert().values(role_id=5, permission_id=998))
        conn.execute(permission.insert().values(id=998, code="server_ops:future_allowed"))
        assert conn.execute(sa.select(permission.c.code).where(permission.c.id == 998)).scalar_one() == (
            "server_ops:future_allowed"
        )
        normal_auto = conn.execute(permission.insert().values(code="project:auto_id_slot"))
        normal_auto_id = int(normal_auto.inserted_primary_key[0])
        super_auto_id = normal_auto_id + 1
        conn.execute(role_permission.insert().values(role_id=5, permission_id=super_auto_id))
        server_auto = conn.execute(permission.insert().values(code="server_ops:auto_id_future_allowed"))
        assert int(server_auto.inserted_primary_key[0]) == super_auto_id
        assert conn.execute(
            sa.select(permission.c.code).where(permission.c.id == super_auto_id)
        ).scalar_one() == "server_ops:auto_id_future_allowed"
        with pytest.raises(IntegrityError, match="reserved for super_admin"):
            conn.execute(role_permission.insert().values(role_id=4, permission_id=10))
        with pytest.raises(IntegrityError, match="cannot be deleted"):
            conn.execute(
                role_permission.delete().where(
                    role_permission.c.role_id == 5,
                    role_permission.c.permission_id == 10,
                )
            )

    engine.dispose()


def test_migration_022_validates_contract_before_any_write(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    tables = _schema(engine)
    user, role, permission, _user_role, _role_permission = tables

    with engine.begin() as conn:
        conn.execute(
            role.insert(),
            [
                {"id": 4, "code": "admin", "status": "active"},
                {"id": 5, "code": "super_admin", "status": "active"},
            ],
        )
        conn.execute(
            permission.insert(),
            [
                {"id": 10, "code": "server_ops:view"},
                {"id": 11, "code": "server_ops:execute"},
            ],
        )
        conn.execute(
            user.insert(),
            [
                {"id": 1, "username": "admin", "role": "admin", "status": 0, "token_version": 5},
                {"id": 2, "username": "admin", "role": "admin", "status": 1, "token_version": 6},
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        with pytest.raises(RuntimeError, match="expected exactly one 'admin' account, found 2"):
            migration.upgrade()
        assert conn.execute(sa.select(user.c.role).order_by(user.c.id)).scalars().all() == [
            "admin",
            "admin",
        ]
        triggers = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).all()
        assert triggers == []

    engine.dispose()


def test_migration_022_fails_on_missing_permission_without_writes(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    tables = _schema(engine)
    user, role, permission, _user_role, _role_permission = tables

    with engine.begin() as conn:
        conn.execute(
            role.insert(),
            [
                {"id": 4, "code": "admin", "status": "active"},
                {"id": 5, "code": "super_admin", "status": "active"},
            ],
        )
        conn.execute(
            permission.insert(),
            [
                {"id": 10, "code": "server_ops:view"},
                {"id": 11, "code": "server_ops:execute"},
            ],
        )
        conn.execute(
            user.insert().values(
                id=1,
                username="admin",
                role="admin",
                status=0,
                token_version=5,
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        with pytest.raises(RuntimeError, match="missing required permissions: server_ops:critical"):
            migration.upgrade()
        assert conn.execute(sa.select(user.c.role, user.c.status)).one() == ("admin", 0)

    engine.dispose()


def test_migration_022_downgrade_restores_admin_server_ops_contract(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    tables = _schema(engine)
    user, _role, _permission, user_role, role_permission = tables

    with engine.begin() as conn:
        _seed(conn, tables)
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        migration.upgrade()
        migration.downgrade()

        admin = conn.execute(sa.select(user.c.role, user.c.status, user.c.token_version).where(user.c.id == 1)).one()
        assert admin == ("admin", 1, 7)
        assert conn.execute(sa.select(user_role.c.role_id).where(user_role.c.user_id == 1)).scalars().all() == [4]
        assert set(
            conn.execute(
                sa.select(role_permission.c.role_id, role_permission.c.permission_id).where(
                    role_permission.c.permission_id.in_((10, 11, 12, 14))
                )
            ).all()
        ) == {
            (4, 10),
            (4, 11),
            (4, 12),
            (4, 14),
            (5, 10),
            (5, 11),
            (5, 12),
            (5, 14),
        }
        assert conn.execute(sa.text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'")).scalar_one() == 0

        # The downgrade deliberately restores the pre-022 permissive database contract.
        conn.execute(user.update().where(user.c.id == 1).values(role="user"))
        conn.execute(user_role.insert().values(user_id=7, role_id=5))
        conn.execute(role_permission.insert().values(role_id=2, permission_id=10))

    engine.dispose()


def test_migration_022_mysql_ddl_contains_all_fail_closed_guards():
    migration = _migration_module()
    mysql_statements = migration._mysql_trigger_statements()
    ddl = "\n".join(mysql_statements)
    insert_guard = next(
        statement
        for statement in migration._mysql_trigger_statements()
        if "CREATE TRIGGER trg_permission_server_ops_insert" in statement
    )

    assert "SIGNAL SQLSTATE '45000'" in ddl
    assert "BINARY NEW.username = BINARY 'admin'" in ddl
    assert "BINARY NEW.role = BINARY 'super_admin'" in ddl
    assert "admin account cannot be deleted" in ddl
    assert "admin account id cannot be changed" in ddl
    assert "admin super_admin binding cannot be deleted" in ddl
    assert "server_ops permissions are reserved for super_admin" in ddl
    assert "super_admin server_ops permission cannot be deleted" in ddl
    assert "reserved administrator role code cannot be changed" in ddl
    assert "reserved administrator role id cannot be changed" in ddl
    assert "server_ops permission code cannot be changed" in ddl
    assert "server_ops permission id cannot be changed" in ddl
    assert "trg_permission_server_ops_insert" in ddl
    assert "server_ops permission cannot be created for a non-super_admin role" in ddl
    assert "AFTER INSERT ON permission" in insert_guard
    assert "EXISTS" in insert_guard
    assert "rp.permission_id = NEW.id" in insert_guard
    assert "COALESCE(r.code, '') <> 'super_admin'" in insert_guard
    expected_names = set(migration._TRIGGER_NAMES)
    mysql_names = {
        match.group(1)
        for statement in mysql_statements
        if (match := re.search(r"CREATE TRIGGER\s+(\w+)", statement))
    }
    sqlite_names = {
        match.group(1)
        for statement in migration._sqlite_trigger_statements()
        if (match := re.search(r"CREATE TRIGGER\s+(\w+)", statement))
    }
    assert mysql_names == expected_names
    assert sqlite_names == expected_names
