"""057 migration: keep the auditor menu aligned with its reviewer route."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, create_engine, text


def _load_migration():
    path = Path(__file__).parents[2] / "alembic/versions/057_reviewer_audit_menu_route.py"
    spec = spec_from_file_location("reviewer_audit_menu_route_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_upgrade_moves_only_audit_permission_menu_and_downgrade_restores_it():
    migration = _load_migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table(
        "menu",
        metadata,
        Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
        Column("path", String(255), nullable=False),
        Column("permission_code", String(64)),
    )
    metadata.create_all(engine)

    with engine.connect() as connection:
        connection.execute(text(
            "INSERT INTO menu (id,path,permission_code) VALUES "
            "(1,'/admin/audit','audit:view'),(2,'/admin/audit','user:view')"
        ))
        connection.commit()
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        paths = dict(connection.execute(text("SELECT id,path FROM menu")).all())
        assert paths == {1: "/audit", 2: "/admin/audit"}

        migration.downgrade()
        paths = dict(connection.execute(text("SELECT id,path FROM menu")).all())
        assert paths == {1: "/admin/audit", 2: "/admin/audit"}

    engine.dispose()
