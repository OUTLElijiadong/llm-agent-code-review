"""Enforce the unique super administrator and server-operations boundary.

Revision ID: 022
Revises: 021
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADMIN_USERNAME = "admin"
_ADMIN_ROLE_CODE = "admin"
_SUPER_ADMIN_ROLE_CODE = "super_admin"
_SERVER_OPS_PERMISSION_CODES = (
    "server_ops:view",
    "server_ops:execute",
    "server_ops:critical",
)
_CHECK_NAME = "ck_user_unique_super_admin"
_INITIAL_PASSWORD_PLACEHOLDER = "!INITIAL_ADMIN_PASSWORD_REQUIRED!"
_LEGACY_DEFAULT_PASSWORD_HASH = "$2b$12$Z6ulrL6Jmnek.a.FALzQleAJ2yYcnI.cj9yEuj5GbYAlZfkrnWD7O"
_TRIGGER_NAMES = (
    "trg_user_super_admin_insert",
    "trg_user_super_admin_update",
    "trg_user_super_admin_delete",
    "trg_user_role_super_insert",
    "trg_user_role_super_update",
    "trg_user_role_super_delete",
    "trg_role_reserved_update",
    "trg_role_reserved_delete",
    "trg_permission_server_ops_insert",
    "trg_permission_server_ops_update",
    "trg_permission_server_ops_delete",
    "trg_role_permission_server_ops_insert",
    "trg_role_permission_server_ops_update",
    "trg_role_permission_server_ops_delete",
)


class _Contract:
    __slots__ = (
        "admin_user_id",
        "admin_role_id",
        "super_admin_role_id",
        "server_ops_permission_ids",
    )

    def __init__(
        self,
        *,
        admin_user_id: int,
        admin_role_id: int,
        super_admin_role_id: int,
        server_ops_permission_ids: tuple[int, ...],
    ) -> None:
        self.admin_user_id = admin_user_id
        self.admin_role_id = admin_role_id
        self.super_admin_role_id = super_admin_role_id
        self.server_ops_permission_ids = server_ops_permission_ids


def _tables() -> tuple[
    sa.TableClause,
    sa.TableClause,
    sa.TableClause,
    sa.TableClause,
    sa.TableClause,
]:
    user = sa.table(
        "user",
        sa.column("id", sa.BigInteger()),
        sa.column("username", sa.String()),
        sa.column("role", sa.String()),
        sa.column("status", sa.SmallInteger()),
        sa.column("token_version", sa.Integer()),
    )
    role = sa.table(
        "role",
        sa.column("id", sa.BigInteger()),
        sa.column("code", sa.String()),
        sa.column("status", sa.String()),
    )
    permission = sa.table(
        "permission",
        sa.column("id", sa.BigInteger()),
        sa.column("code", sa.String()),
    )
    user_role = sa.table(
        "user_role",
        sa.column("id", sa.BigInteger()),
        sa.column("user_id", sa.BigInteger()),
        sa.column("role_id", sa.BigInteger()),
    )
    role_permission = sa.table(
        "role_permission",
        sa.column("id", sa.BigInteger()),
        sa.column("role_id", sa.BigInteger()),
        sa.column("permission_id", sa.BigInteger()),
    )
    return user, role, permission, user_role, role_permission


def _ensure_token_version_column(conn) -> None:
    """在归一化和失效旧 JWT 前补齐历史库缺失的会话版本列。"""

    columns = {str(column["name"]) for column in sa.inspect(conn).get_columns("user")}
    if "token_version" in columns:
        return
    if conn.dialect.name == "mysql":
        conn.exec_driver_sql(
            "ALTER TABLE `user` ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0 "
            "COMMENT '令牌版本: 新设备登录后旧JWT失效'"
        )
    elif conn.dialect.name == "sqlite":
        conn.exec_driver_sql(
            'ALTER TABLE "user" ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0'
        )
    else:
        raise RuntimeError(f"unsupported database dialect for migration 022: {conn.dialect.name}")


def _required_contract(
    conn,
    user: sa.TableClause,
    role: sa.TableClause,
    permission: sa.TableClause,
) -> _Contract:
    # MySQL production collations are commonly case-insensitive.  Filter once
    # in SQL for efficiency, then require the canonical spelling in Python.
    admin_rows = [
        row
        for row in conn.execute(
            sa.select(user.c.id, user.c.username).where(user.c.username == _ADMIN_USERNAME)
        ).mappings()
        if str(row["username"]) == _ADMIN_USERNAME
    ]
    if len(admin_rows) != 1:
        raise RuntimeError(f"expected exactly one {_ADMIN_USERNAME!r} account, found {len(admin_rows)}")

    role_rows = (
        conn.execute(
            sa.select(role.c.id, role.c.code).where(role.c.code.in_((_ADMIN_ROLE_CODE, _SUPER_ADMIN_ROLE_CODE)))
        )
        .mappings()
        .all()
    )
    role_ids = {str(row["code"]): int(row["id"]) for row in role_rows}
    missing_roles = sorted({_ADMIN_ROLE_CODE, _SUPER_ADMIN_ROLE_CODE} - set(role_ids))
    if missing_roles:
        raise RuntimeError(f"missing required roles: {', '.join(missing_roles)}")

    permission_rows = (
        conn.execute(sa.select(permission.c.id, permission.c.code).where(permission.c.code.like("server_ops:%")))
        .mappings()
        .all()
    )
    permission_ids = {str(row["code"]): int(row["id"]) for row in permission_rows}
    missing_permissions = sorted(set(_SERVER_OPS_PERMISSION_CODES) - set(permission_ids))
    if missing_permissions:
        raise RuntimeError(f"missing required permissions: {', '.join(missing_permissions)}")

    return _Contract(
        admin_user_id=int(admin_rows[0]["id"]),
        admin_role_id=role_ids[_ADMIN_ROLE_CODE],
        super_admin_role_id=role_ids[_SUPER_ADMIN_ROLE_CODE],
        server_ops_permission_ids=tuple(permission_ids[code] for code in sorted(permission_ids)),
    )


def _initialize_seed_admin_password(conn, admin_user_id: int) -> None:
    """仅替换无法登录占位值或历史公开默认口令。"""

    columns = {str(column["name"]) for column in sa.inspect(conn).get_columns("user")}
    if "password" not in columns:
        return
    password = conn.execute(
        sa.text("SELECT password FROM `user` WHERE id = :user_id"),
        {"user_id": admin_user_id},
    ).scalar_one()
    if password not in {_INITIAL_PASSWORD_PLACEHOLDER, _LEGACY_DEFAULT_PASSWORD_HASH}:
        return

    initial_password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    encoded = initial_password.encode("utf-8")
    forbidden = {
        "admin123",
        "change_me_to_a_strong_initial_admin_password",
        _INITIAL_PASSWORD_PLACEHOLDER,
    }
    if (
        len(initial_password) < 12
        or len(initial_password) > 32
        or len(encoded) > 72
        or initial_password in forbidden
        or initial_password.lower().startswith("change_me")
    ):
        raise RuntimeError(
            "INITIAL_ADMIN_PASSWORD must be a non-default 12-32 character secret "
            "when initializing the admin account"
        )

    from app.core.security import hash_password

    conn.execute(
        sa.text("UPDATE `user` SET password = :password WHERE id = :user_id"),
        {"password": hash_password(initial_password), "user_id": admin_user_id},
    )


def _drop_triggers(conn) -> None:
    for trigger_name in _TRIGGER_NAMES:
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger_name}")


def _mysql_check_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'user' "
                "AND CONSTRAINT_TYPE = 'CHECK' AND CONSTRAINT_NAME = :name"
            ),
            {"name": _CHECK_NAME},
        ).scalar_one()
    )


def _mysql_trigger_statements() -> tuple[str, ...]:
    return (
        """
        CREATE TRIGGER trg_user_super_admin_insert
        BEFORE INSERT ON `user` FOR EACH ROW
        BEGIN
          IF NOT (
            (BINARY NEW.username = BINARY 'admin' AND BINARY NEW.role = BINARY 'super_admin' AND NEW.status = 1)
            OR (BINARY NEW.username <> BINARY 'admin' AND LOWER(COALESCE(NEW.role, '')) <> 'super_admin')
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid super administrator identity';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_user_super_admin_update
        BEFORE UPDATE ON `user` FOR EACH ROW
        BEGIN
          IF BINARY OLD.username = BINARY 'admin'
             AND NEW.id <> OLD.id THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'admin account id cannot be changed';
          END IF;
          IF BINARY OLD.username = BINARY 'admin'
             AND BINARY NEW.username <> BINARY 'admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'admin account cannot be renamed';
          END IF;
          IF NOT (
            (BINARY NEW.username = BINARY 'admin' AND BINARY NEW.role = BINARY 'super_admin' AND NEW.status = 1)
            OR (BINARY NEW.username <> BINARY 'admin' AND LOWER(COALESCE(NEW.role, '')) <> 'super_admin')
          ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid super administrator identity';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_user_super_admin_delete
        BEFORE DELETE ON `user` FOR EACH ROW
        BEGIN
          IF BINARY OLD.username = BINARY 'admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'admin account cannot be deleted';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_user_role_super_insert
        BEFORE INSERT ON user_role FOR EACH ROW
        BEGIN
          IF BINARY COALESCE((SELECT u.username FROM `user` u WHERE u.id = NEW.user_id), '') = BINARY 'admin'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'admin may only hold super_admin role';
          END IF;
          IF BINARY COALESCE((SELECT u.username FROM `user` u WHERE u.id = NEW.user_id), '') <> BINARY 'admin'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') = 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'super_admin role is reserved for admin';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_user_role_super_update
        BEFORE UPDATE ON user_role FOR EACH ROW
        BEGIN
          IF BINARY COALESCE((SELECT u.username FROM `user` u WHERE u.id = OLD.user_id), '') = BINARY 'admin'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin'
             AND (NEW.user_id <> OLD.user_id OR NEW.role_id <> OLD.role_id) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'admin super_admin binding cannot be changed';
          END IF;
          IF BINARY COALESCE((SELECT u.username FROM `user` u WHERE u.id = NEW.user_id), '') = BINARY 'admin'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'admin may only hold super_admin role';
          END IF;
          IF BINARY COALESCE((SELECT u.username FROM `user` u WHERE u.id = NEW.user_id), '') <> BINARY 'admin'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') = 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'super_admin role is reserved for admin';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_user_role_super_delete
        BEFORE DELETE ON user_role FOR EACH ROW
        BEGIN
          IF BINARY COALESCE((SELECT u.username FROM `user` u WHERE u.id = OLD.user_id), '') = BINARY 'admin'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'admin super_admin binding cannot be deleted';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_role_reserved_update
        BEFORE UPDATE ON role FOR EACH ROW
        BEGIN
          IF LOWER(COALESCE(OLD.code, '')) IN ('admin', 'super_admin')
             AND NEW.id <> OLD.id THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reserved administrator role id cannot be changed';
          END IF;
          IF BINARY OLD.code <> BINARY NEW.code
             AND (
               LOWER(COALESCE(OLD.code, '')) IN ('admin', 'super_admin')
               OR LOWER(COALESCE(NEW.code, '')) IN ('admin', 'super_admin')
             ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reserved administrator role code cannot be changed';
          END IF;
          IF LOWER(COALESCE(NEW.code, '')) IN ('admin', 'super_admin')
             AND BINARY COALESCE(NEW.status, '') <> BINARY 'active' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'administrator roles must remain active';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_role_reserved_delete
        BEFORE DELETE ON role FOR EACH ROW
        BEGIN
          IF LOWER(COALESCE(OLD.code, '')) IN ('admin', 'super_admin') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reserved administrator role cannot be deleted';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_permission_server_ops_insert
        AFTER INSERT ON permission FOR EACH ROW
        BEGIN
          IF LOWER(LEFT(COALESCE(NEW.code, ''), 11)) = 'server_ops:'
             AND EXISTS (
               SELECT 1
               FROM role_permission rp
               LEFT JOIN role r ON r.id = rp.role_id
               WHERE rp.permission_id = NEW.id
                 AND COALESCE(r.code, '') <> 'super_admin'
             ) THEN
            SIGNAL SQLSTATE '45000'
              SET MESSAGE_TEXT = 'server_ops permission cannot be created for a non-super_admin role';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_permission_server_ops_update
        BEFORE UPDATE ON permission FOR EACH ROW
        BEGIN
          IF LOWER(LEFT(COALESCE(OLD.code, ''), 11)) = 'server_ops:'
             AND NEW.id <> OLD.id THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'server_ops permission id cannot be changed';
          END IF;
          IF BINARY OLD.code <> BINARY NEW.code
             AND (
               LOWER(LEFT(COALESCE(OLD.code, ''), 11)) = 'server_ops:'
               OR LOWER(LEFT(COALESCE(NEW.code, ''), 11)) = 'server_ops:'
             ) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'server_ops permission code cannot be changed';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_permission_server_ops_delete
        BEFORE DELETE ON permission FOR EACH ROW
        BEGIN
          IF LOWER(LEFT(COALESCE(OLD.code, ''), 11)) = 'server_ops:' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'server_ops permission cannot be deleted';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_role_permission_server_ops_insert
        BEFORE INSERT ON role_permission FOR EACH ROW
        BEGIN
          IF LEFT(COALESCE((SELECT p.code FROM permission p WHERE p.id = NEW.permission_id), ''), 11) = 'server_ops:'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'server_ops permissions are reserved for super_admin';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_role_permission_server_ops_update
        BEFORE UPDATE ON role_permission FOR EACH ROW
        BEGIN
          IF LEFT(COALESCE((SELECT p.code FROM permission p WHERE p.id = OLD.permission_id), ''), 11) = 'server_ops:'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin'
             AND (NEW.role_id <> OLD.role_id OR NEW.permission_id <> OLD.permission_id) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'super_admin server_ops permission cannot be changed';
          END IF;
          IF LEFT(COALESCE((SELECT p.code FROM permission p WHERE p.id = NEW.permission_id), ''), 11) = 'server_ops:'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'server_ops permissions are reserved for super_admin';
          END IF;
        END
        """,
        """
        CREATE TRIGGER trg_role_permission_server_ops_delete
        BEFORE DELETE ON role_permission FOR EACH ROW
        BEGIN
          IF LEFT(COALESCE((SELECT p.code FROM permission p WHERE p.id = OLD.permission_id), ''), 11) = 'server_ops:'
             AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin' THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'super_admin server_ops permission cannot be deleted';
          END IF;
        END
        """,
    )


def _sqlite_trigger_statements() -> tuple[str, ...]:
    identity_guard = """
      NOT (
        (NEW.username = 'admin' AND NEW.role = 'super_admin' AND NEW.status = 1)
        OR (NEW.username <> 'admin' AND LOWER(NEW.role) <> 'super_admin')
      )
    """
    return (
        f"""
        CREATE TRIGGER trg_user_super_admin_insert
        BEFORE INSERT ON "user" FOR EACH ROW WHEN {identity_guard}
        BEGIN SELECT RAISE(ABORT, 'invalid super administrator identity'); END
        """,
        f"""
        CREATE TRIGGER trg_user_super_admin_update
        BEFORE UPDATE ON "user" FOR EACH ROW WHEN
          (OLD.username = 'admin' AND NEW.id <> OLD.id)
          OR (OLD.username = 'admin' AND NEW.username <> 'admin')
          OR ({identity_guard})
        BEGIN SELECT RAISE(ABORT, 'invalid super administrator identity'); END
        """,
        """
        CREATE TRIGGER trg_user_super_admin_delete
        BEFORE DELETE ON "user" FOR EACH ROW WHEN OLD.username = 'admin'
        BEGIN SELECT RAISE(ABORT, 'admin account cannot be deleted'); END
        """,
        """
        CREATE TRIGGER trg_user_role_super_insert
        BEFORE INSERT ON user_role FOR EACH ROW WHEN
          (COALESCE((SELECT u.username FROM "user" u WHERE u.id = NEW.user_id), '') = 'admin'
           AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin')
          OR
          (COALESCE((SELECT u.username FROM "user" u WHERE u.id = NEW.user_id), '') <> 'admin'
           AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') = 'super_admin')
        BEGIN SELECT RAISE(ABORT, 'invalid super_admin role binding'); END
        """,
        """
        CREATE TRIGGER trg_user_role_super_update
        BEFORE UPDATE ON user_role FOR EACH ROW WHEN
          (COALESCE((SELECT u.username FROM "user" u WHERE u.id = OLD.user_id), '') = 'admin'
           AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin'
           AND (NEW.user_id <> OLD.user_id OR NEW.role_id <> OLD.role_id))
          OR
          (COALESCE((SELECT u.username FROM "user" u WHERE u.id = NEW.user_id), '') = 'admin'
           AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin')
          OR
          (COALESCE((SELECT u.username FROM "user" u WHERE u.id = NEW.user_id), '') <> 'admin'
           AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') = 'super_admin')
        BEGIN SELECT RAISE(ABORT, 'invalid super_admin role binding'); END
        """,
        """
        CREATE TRIGGER trg_user_role_super_delete
        BEFORE DELETE ON user_role FOR EACH ROW WHEN
          COALESCE((SELECT u.username FROM "user" u WHERE u.id = OLD.user_id), '') = 'admin'
          AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin'
        BEGIN SELECT RAISE(ABORT, 'admin super_admin binding cannot be deleted'); END
        """,
        """
        CREATE TRIGGER trg_role_reserved_update
        BEFORE UPDATE ON role FOR EACH ROW WHEN
          (LOWER(OLD.code) IN ('admin', 'super_admin') AND NEW.id <> OLD.id)
          OR (OLD.code <> NEW.code AND (
            LOWER(OLD.code) IN ('admin', 'super_admin')
            OR LOWER(NEW.code) IN ('admin', 'super_admin')
          ))
          OR (LOWER(NEW.code) IN ('admin', 'super_admin') AND NEW.status <> 'active')
        BEGIN SELECT RAISE(ABORT, 'reserved administrator role cannot be changed'); END
        """,
        """
        CREATE TRIGGER trg_role_reserved_delete
        BEFORE DELETE ON role FOR EACH ROW WHEN LOWER(OLD.code) IN ('admin', 'super_admin')
        BEGIN SELECT RAISE(ABORT, 'reserved administrator role cannot be deleted'); END
        """,
        """
        CREATE TRIGGER trg_permission_server_ops_insert
        AFTER INSERT ON permission FOR EACH ROW WHEN
          LOWER(NEW.code) LIKE 'server_ops:%'
          AND EXISTS (
            SELECT 1
            FROM role_permission rp
            LEFT JOIN role r ON r.id = rp.role_id
            WHERE rp.permission_id = NEW.id
              AND COALESCE(r.code, '') <> 'super_admin'
          )
        BEGIN SELECT RAISE(ABORT, 'server_ops permission cannot be created for a non-super_admin role'); END
        """,
        """
        CREATE TRIGGER trg_permission_server_ops_update
        BEFORE UPDATE ON permission FOR EACH ROW WHEN
          (LOWER(OLD.code) LIKE 'server_ops:%' AND NEW.id <> OLD.id)
          OR (OLD.code <> NEW.code AND (
            LOWER(OLD.code) LIKE 'server_ops:%'
            OR LOWER(NEW.code) LIKE 'server_ops:%'
          ))
        BEGIN SELECT RAISE(ABORT, 'server_ops permission code cannot be changed'); END
        """,
        """
        CREATE TRIGGER trg_permission_server_ops_delete
        BEFORE DELETE ON permission FOR EACH ROW WHEN LOWER(OLD.code) LIKE 'server_ops:%'
        BEGIN SELECT RAISE(ABORT, 'server_ops permission cannot be deleted'); END
        """,
        """
        CREATE TRIGGER trg_role_permission_server_ops_insert
        BEFORE INSERT ON role_permission FOR EACH ROW WHEN
          COALESCE((SELECT p.code FROM permission p WHERE p.id = NEW.permission_id), '') LIKE 'server_ops:%'
          AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin'
        BEGIN SELECT RAISE(ABORT, 'server_ops permissions are reserved for super_admin'); END
        """,
        """
        CREATE TRIGGER trg_role_permission_server_ops_update
        BEFORE UPDATE ON role_permission FOR EACH ROW WHEN
          (COALESCE((SELECT p.code FROM permission p WHERE p.id = OLD.permission_id), '') LIKE 'server_ops:%'
           AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin'
           AND (NEW.role_id <> OLD.role_id OR NEW.permission_id <> OLD.permission_id))
          OR
          (COALESCE((SELECT p.code FROM permission p WHERE p.id = NEW.permission_id), '') LIKE 'server_ops:%'
           AND COALESCE((SELECT r.code FROM role r WHERE r.id = NEW.role_id), '') <> 'super_admin')
        BEGIN SELECT RAISE(ABORT, 'invalid server_ops role binding'); END
        """,
        """
        CREATE TRIGGER trg_role_permission_server_ops_delete
        BEFORE DELETE ON role_permission FOR EACH ROW WHEN
          COALESCE((SELECT p.code FROM permission p WHERE p.id = OLD.permission_id), '') LIKE 'server_ops:%'
          AND COALESCE((SELECT r.code FROM role r WHERE r.id = OLD.role_id), '') = 'super_admin'
        BEGIN SELECT RAISE(ABORT, 'super_admin server_ops permission cannot be deleted'); END
        """,
    )


def _install_guards(conn) -> None:
    dialect = conn.dialect.name
    if dialect == "mysql":
        if not _mysql_check_exists(conn):
            conn.exec_driver_sql(
                f"""
                ALTER TABLE `user` ADD CONSTRAINT {_CHECK_NAME} CHECK (
                  (BINARY `username` = BINARY 'admin' AND BINARY `role` = BINARY 'super_admin' AND `status` = 1)
                  OR (BINARY `username` <> BINARY 'admin' AND LOWER(COALESCE(`role`, '')) <> 'super_admin')
                )
                """
            )
        statements = _mysql_trigger_statements()
    elif dialect == "sqlite":
        statements = _sqlite_trigger_statements()
    else:
        raise RuntimeError(f"unsupported database dialect for migration 022: {dialect}")
    for statement in statements:
        conn.exec_driver_sql(statement.strip())


def _drop_guards(conn) -> None:
    _drop_triggers(conn)
    if conn.dialect.name == "mysql" and _mysql_check_exists(conn):
        conn.exec_driver_sql(f"ALTER TABLE `user` DROP CHECK {_CHECK_NAME}")


def _normalize_data(
    conn,
    contract: _Contract,
    user: sa.TableClause,
    role: sa.TableClause,
    user_role: sa.TableClause,
    role_permission: sa.TableClause,
) -> None:
    conn.execute(
        role.update()
        .where(role.c.id.in_((contract.admin_role_id, contract.super_admin_role_id)))
        .values(status="active")
    )
    admin_row = (
        conn.execute(sa.select(user.c.role, user.c.status).where(user.c.id == contract.admin_user_id)).mappings().one()
    )
    admin_role_ids = set(
        int(value)
        for value in conn.execute(
            sa.select(user_role.c.role_id).where(user_role.c.user_id == contract.admin_user_id)
        ).scalars()
    )
    admin_needs_normalization = (
        str(admin_row["role"]) != _SUPER_ADMIN_ROLE_CODE
        or int(admin_row["status"]) != 1
        or admin_role_ids != {contract.super_admin_role_id}
    )

    legacy_super_ids = set(
        int(value)
        for value in conn.execute(
            sa.select(user.c.id).where(
                user.c.role == _SUPER_ADMIN_ROLE_CODE,
                user.c.id != contract.admin_user_id,
            )
        ).scalars()
    )
    linked_super_ids = set(
        int(value)
        for value in conn.execute(
            sa.select(user_role.c.user_id)
            .join(user, user.c.id == user_role.c.user_id)
            .where(
                user_role.c.role_id == contract.super_admin_role_id,
                user_role.c.user_id != contract.admin_user_id,
            )
        ).scalars()
    )
    demoted_user_ids = legacy_super_ids | linked_super_ids
    legacy_admin_ids = set(
        int(value)
        for value in conn.execute(
            sa.select(user.c.id).where(
                user.c.role == _ADMIN_ROLE_CODE,
                user.c.id != contract.admin_user_id,
            )
        ).scalars()
    )
    linked_admin_ids = set(
        int(value)
        for value in conn.execute(
            sa.select(user_role.c.user_id)
            .join(user, user.c.id == user_role.c.user_id)
            .where(
                user_role.c.role_id == contract.admin_role_id,
                user_role.c.user_id != contract.admin_user_id,
            )
        ).scalars()
    )
    ordinary_admin_ids = demoted_user_ids | legacy_admin_ids | linked_admin_ids

    existing_server_links = set(
        (int(row[0]), int(row[1]))
        for row in conn.execute(
            sa.select(
                role_permission.c.role_id,
                role_permission.c.permission_id,
            ).where(role_permission.c.permission_id.in_(contract.server_ops_permission_ids))
        ).all()
    )
    missing_super_permissions = {
        permission_id
        for permission_id in contract.server_ops_permission_ids
        if (contract.super_admin_role_id, permission_id) not in existing_server_links
    }

    if admin_needs_normalization:
        conn.execute(
            user.update().where(user.c.id == contract.admin_user_id).values(role=_SUPER_ADMIN_ROLE_CODE, status=1)
        )
        conn.execute(user_role.delete().where(user_role.c.user_id == contract.admin_user_id))
        conn.execute(
            user_role.insert().values(
                user_id=contract.admin_user_id,
                role_id=contract.super_admin_role_id,
            )
        )

    if ordinary_admin_ids:
        conn.execute(user.update().where(user.c.id.in_(ordinary_admin_ids)).values(role=_ADMIN_ROLE_CODE))
        conn.execute(user_role.delete().where(user_role.c.user_id.in_(ordinary_admin_ids)))
        conn.execute(
            user_role.insert(),
            [{"user_id": user_id, "role_id": contract.admin_role_id} for user_id in sorted(ordinary_admin_ids)],
        )

    # Remove even orphaned or stale super_admin bindings left outside the user set.
    conn.execute(
        user_role.delete().where(
            user_role.c.role_id == contract.super_admin_role_id,
            user_role.c.user_id != contract.admin_user_id,
        )
    )
    conn.execute(
        role_permission.delete().where(
            role_permission.c.permission_id.in_(contract.server_ops_permission_ids),
            role_permission.c.role_id != contract.super_admin_role_id,
        )
    )
    if missing_super_permissions:
        conn.execute(
            role_permission.insert(),
            [
                {
                    "role_id": contract.super_admin_role_id,
                    "permission_id": permission_id,
                }
                for permission_id in sorted(missing_super_permissions)
            ],
        )

    # Invalidate every pre-deployment JWT, including accounts whose role data
    # was already correct, so no legacy multi-device session survives 022.
    conn.execute(user.update().values(token_version=sa.func.coalesce(user.c.token_version, 0) + 1))


def upgrade() -> None:
    conn = op.get_bind()
    user, role, permission, user_role, role_permission = _tables()

    # Validate the complete contract before the first write or DDL statement.
    contract = _required_contract(conn, user, role, permission)
    _initialize_seed_admin_password(conn, contract.admin_user_id)
    _ensure_token_version_column(conn)
    _drop_triggers(conn)
    _normalize_data(conn, contract, user, role, user_role, role_permission)
    _install_guards(conn)


def downgrade() -> None:
    conn = op.get_bind()
    user, role, permission, user_role, role_permission = _tables()
    contract = _required_contract(conn, user, role, permission)
    _drop_guards(conn)

    conn.execute(
        user.update()
        .where(user.c.id == contract.admin_user_id)
        .values(
            role=_ADMIN_ROLE_CODE,
            token_version=sa.func.coalesce(user.c.token_version, 0) + 1,
        )
    )
    conn.execute(user_role.delete().where(user_role.c.user_id == contract.admin_user_id))
    conn.execute(
        user_role.insert().values(
            user_id=contract.admin_user_id,
            role_id=contract.admin_role_id,
        )
    )

    existing_admin_permissions = set(
        int(value)
        for value in conn.execute(
            sa.select(role_permission.c.permission_id).where(
                role_permission.c.role_id == contract.admin_role_id,
                role_permission.c.permission_id.in_(contract.server_ops_permission_ids),
            )
        ).scalars()
    )
    missing_admin_permissions = set(contract.server_ops_permission_ids) - existing_admin_permissions
    if missing_admin_permissions:
        conn.execute(
            role_permission.insert(),
            [
                {
                    "role_id": contract.admin_role_id,
                    "permission_id": permission_id,
                }
                for permission_id in sorted(missing_admin_permissions)
            ],
        )
