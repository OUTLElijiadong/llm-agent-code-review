"""049 迁移:普通用户收回 Agent 工坊资产权限,reviewer 授权保持不变。"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, create_engine, text


def _load_migration():
    path = Path(__file__).parents[2] / "alembic/versions/049_agent_studio_reviewer_only.py"
    spec = spec_from_file_location("studio_reviewer_only_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _build_engine_with_seed():
    migration = _load_migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    role = Table(
        "role", metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String(32)),
    )
    permission = Table(
        "permission", metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String(64)),
    )
    role_permission = Table(
        "role_permission", metadata,
        Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
        Column("role_id", Integer),
        Column("permission_id", Integer),
    )
    metadata.create_all(engine)
    with engine.connect() as connection:
        connection.execute(text("INSERT INTO role (id, code) VALUES (1,'user'), (2,'reviewer'), (3,'admin')"))
        for index, code in enumerate(migration._PERMISSION_CODES, start=1):
            connection.execute(text(f"INSERT INTO permission (id, code) VALUES ({index},'{code}')"))
        # 模拟 034/045 授予普通用户 + 015 授予 reviewer 的历史状态
        for pid in range(1, len(migration._PERMISSION_CODES) + 1):
            connection.execute(text(f"INSERT INTO role_permission (id, role_id, permission_id) VALUES ({pid}, 1, {pid})"))
            connection.execute(text(f"INSERT INTO role_permission (id, role_id, permission_id) VALUES ({100 + pid}, 2, {pid})"))
        connection.commit()
    return migration, engine, role, permission, role_permission


def test_upgrade_revokes_user_grants_and_keeps_reviewer():
    migration, engine, *_ = _build_engine_with_seed()
    with engine.connect() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        user_perms = [row[0] for row in connection.execute(text(
            "SELECT p.code FROM role_permission rp JOIN role r ON r.id=rp.role_id "
            "JOIN permission p ON p.id=rp.permission_id WHERE r.code='user' ORDER BY p.code"))]
        reviewer_perms = [row[0] for row in connection.execute(text(
            "SELECT p.code FROM role_permission rp JOIN role r ON r.id=rp.role_id "
            "JOIN permission p ON p.id=rp.permission_id WHERE r.code='reviewer' ORDER BY p.code"))]
        assert user_perms == []
        assert sorted(reviewer_perms) == sorted(migration._PERMISSION_CODES)


def test_downgrade_regrants_user_and_is_idempotent():
    migration, engine, *_ = _build_engine_with_seed()
    with engine.connect() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()  # 幂等
        migration.downgrade()
        for role_code, expected in (("user", len(migration._PERMISSION_CODES)), ("reviewer", len(migration._PERMISSION_CODES))):
            count = connection.execute(text(
                f"SELECT COUNT(*) FROM role_permission rp JOIN role r ON r.id=rp.role_id "
                f"WHERE r.code='{role_code}'")).scalar()
            assert count == expected


def test_missing_permission_code_fails_loudly():
    migration = _load_migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("role", metadata, Column("id", Integer, primary_key=True), Column("code", String(32)))
    Table("permission", metadata, Column("id", Integer, primary_key=True), Column("code", String(64)))
    Table("role_permission", metadata, Column("id", Integer, primary_key=True),
          Column("role_id", Integer), Column("permission_id", Integer))
    metadata.create_all(engine)
    with engine.connect() as connection:
        connection.execute(text("INSERT INTO role (id, code) VALUES (1,'user')"))
        connection.commit()
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            raise AssertionError("缺权限码应当显式失败")
        except RuntimeError as exc:
            assert "missing required permissions" in str(exc)
